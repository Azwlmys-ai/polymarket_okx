"""
paper_anchor_sim.py — real-time paper execution validator.

Strategy:
  anchor_est = Binance_T_open - ANCHOR_CORRECTION
  If abs(BTC_live - anchor_est) > SIGNAL_THRESHOLD at T+90s/T+120s/T+180s:
    → paper signal, direction = UP if BTC_live > anchor_est else DOWN

No trading. No wallet. No keys. Read-only data only.

Usage:
  python3 research/paper_anchor_sim.py            # run live simulation
  python3 research/paper_anchor_sim.py --report   # report from existing JSONL

Output:
  research/paper_anchor_signals.jsonl
  research/paper_anchor_report.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import signal
import ssl
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any

import aiohttp

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANCHOR_CORRECTION = 76.75          # Binance T_open - Chainlink priceToBeat (measured)
SIGNAL_THRESHOLD_USD = 40.0        # min |BTC_live - anchor_est| to trigger paper signal
CHECK_OFFSETS_S = [90, 120, 180]   # seconds into window to record checkpoints
WINDOW_S = 300                     # 5 minutes

TAKER_FEE_RATE = 0.07              # 7% of (1 - price)
TRADEABLE_SPREAD_MAX = 0.03        # max spread for "tradeable"

GAMMA_BASE = "https://gamma-api.polymarket.com"
BINANCE_BASE = "https://api.binance.com"
SLUG_PREFIX = "btc-updown-5m-"

SIGNALS_PATH = Path("research/paper_anchor_signals.jsonl")
REPORT_PATH = Path("research/paper_anchor_report.md")

# Resolution polling
RESOLUTION_POLL_INTERVAL_S = 10
RESOLUTION_TIMEOUT_S = 900        # give up after 15 minutes past endDate (API cache lag observed up to 8min)

# ---------------------------------------------------------------------------
# SSL / HTTP
# ---------------------------------------------------------------------------

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _make_connector() -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(ssl=_SSL_CTX)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Checkpoint:
    offset_s: int
    ts_utc: str
    btc_live: float
    distance: float
    direction: str                   # "UP" | "DOWN" | "NONE"
    triggered: bool                  # distance > SIGNAL_THRESHOLD_USD
    poly_bid: float | None = None
    poly_ask: float | None = None
    poly_spread: float | None = None
    poly_liquidity: float | None = None
    poly_last_trade: float | None = None
    tradeable: bool = False          # spread <= TRADEABLE_SPREAD_MAX and has liquidity
    error: str | None = None


@dataclass
class WindowRecord:
    slug: str
    event_start_ts: int
    end_ts: int

    # T+0 anchor capture
    binance_t_open: float | None = None
    anchor_est: float | None = None
    t_open_error: str | None = None

    # Checkpoints
    checkpoints: list[Checkpoint] = field(default_factory=list)

    # Resolution
    resolved: bool = False
    outcome: str | None = None        # "UP" | "DOWN"
    price_to_beat: float | None = None
    final_price: float | None = None
    resolved_ts: int | None = None
    resolution_error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["checkpoints"] = [asdict(c) for c in self.checkpoints]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "WindowRecord":
        r = cls(**{k: v for k, v in d.items() if k != "checkpoints"})
        r.checkpoints = [Checkpoint(**c) for c in d.get("checkpoints", [])]
        return r

    def triggered_checkpoints(self) -> list[Checkpoint]:
        return [c for c in self.checkpoints if c.triggered and not c.error]

    def paper_pnl(self, cp: Checkpoint) -> float | None:
        """Fee-adjusted PnL for a paper bet at this checkpoint. Returns None if not resolvable."""
        if not self.resolved or not cp.triggered or self.outcome is None:
            return None
        # Bet at poly ask (YES) if direction == UP, else at poly ask of NO (= 1 - poly_bid)
        if cp.direction == "UP":
            bet_price = cp.poly_ask if cp.poly_ask else 0.50
        else:
            bet_price = (1.0 - cp.poly_bid) if cp.poly_bid else 0.50  # NO at implied ask
        fee = TAKER_FEE_RATE * (1.0 - bet_price)
        total_cost = bet_price + fee
        payout = 1.0 if cp.direction == self.outcome else 0.0
        return payout - total_cost


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _get_json(session: aiohttp.ClientSession, url: str, params: dict | None = None) -> Any:
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            return await r.json(content_type=None)
    except Exception as e:
        return {"_error": str(e)}


async def fetch_binance_t_open(session: aiohttp.ClientSession, event_start_ts: int) -> tuple[float | None, str | None]:
    """Fetch the 1m candle open at exactly event_start_ts. Retries up to 5s."""
    url = f"{BINANCE_BASE}/api/v3/klines"
    params = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "startTime": event_start_ts * 1000,
        "limit": 1,
    }
    for attempt in range(5):
        data = await _get_json(session, url, params)
        if isinstance(data, list) and data and int(data[0][0]) // 1000 == event_start_ts:
            return float(data[0][1]), None   # open price
        if isinstance(data, dict) and "_error" in data:
            return None, data["_error"]
        await asyncio.sleep(1.0)  # wait for candle to be available
    return None, "candle not found after retries"


async def fetch_btc_live(session: aiohttp.ClientSession) -> tuple[float | None, str | None]:
    """Fetch current BTC spot price from Binance."""
    url = f"{BINANCE_BASE}/api/v3/ticker/price"
    data = await _get_json(session, url, {"symbol": "BTCUSDT"})
    if isinstance(data, dict) and "price" in data:
        return float(data["price"]), None
    return None, data.get("_error", "unknown error")


async def fetch_poly_state(session: aiohttp.ClientSession, slug: str) -> dict:
    """Fetch Polymarket CLOB state for the given market slug."""
    url = f"{GAMMA_BASE}/events/slug/{slug}"
    data = await _get_json(session, url)
    if not isinstance(data, dict) or "markets" not in data:
        return {"_error": data.get("_error", "no markets")}
    markets = data.get("markets") or []
    if not markets:
        return {"_error": "empty markets"}
    m = markets[0]
    return {
        "bid": _float(m.get("bestBid")),
        "ask": _float(m.get("bestAsk")),
        "spread": _float(m.get("spread")),
        "liquidity": _float(m.get("liquidity")),
        "last_trade": _float(m.get("lastTradePrice")),
        "accepting_orders": m.get("acceptingOrders"),
    }


async def fetch_resolution(session: aiohttp.ClientSession, slug: str) -> dict:
    """
    Fetch resolution data. Returns non-empty dict when the market is confirmed settled.

    Resolution is available ~17-21s after endDate via outcomePrices (["1","0"] or ["0","1"]).
    eventMetadata.finalPrice takes ~5 extra minutes (next window must also close).
    We use outcomePrices + umaResolutionStatus as the primary resolution signal.
    """
    url = f"{GAMMA_BASE}/events/slug/{slug}"
    data = await _get_json(session, url)
    if not isinstance(data, dict):
        return {}
    markets = data.get("markets") or []
    if not markets:
        return {}
    m = markets[0]

    # Primary: umaResolutionStatus == "resolved"
    uma_status = m.get("umaResolutionStatus", "")
    try:
        op = json.loads(m.get("outcomePrices", "[]"))
        op_0 = float(op[0]) if op else 0.5
    except Exception:
        op_0 = 0.5

    resolved = (uma_status == "resolved") or (op_0 in (0.0, 1.0))
    if not resolved:
        return {}

    outcome = "UP" if op_0 > 0.5 else "DOWN"

    meta = data.get("eventMetadata") or {}
    ptb = meta.get("priceToBeat")
    final = meta.get("finalPrice")

    return {
        "price_to_beat": float(ptb) if ptb else None,
        "final_price": float(final) if final else None,
        "outcome": outcome,
    }


def _float(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f != 0.0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Window worker
# ---------------------------------------------------------------------------

async def window_worker(
    event_start_ts: int,
    session: aiohttp.ClientSession,
    print_lock: asyncio.Lock,
) -> WindowRecord:
    slug = f"{SLUG_PREFIX}{event_start_ts}"
    rec = WindowRecord(slug=slug, event_start_ts=event_start_ts, end_ts=event_start_ts + WINDOW_S)
    now = int(time.time())

    async def _log(msg: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        async with print_lock:
            print(f"[{ts}] [{slug[-8:]}] {msg}")

    # --- Step 1: Capture T_open (wait until T+3s) ---
    wait_s = max(0, event_start_ts + 3 - int(time.time()))
    if wait_s > 0:
        await asyncio.sleep(wait_s)

    t_open, err = await fetch_binance_t_open(session, event_start_ts)
    if t_open is None:
        rec.t_open_error = err or "fetch failed"
        await _log(f"T_open fetch FAILED: {rec.t_open_error}")
    else:
        rec.binance_t_open = t_open
        rec.anchor_est = round(t_open - ANCHOR_CORRECTION, 2)
        await _log(f"T_open={t_open:.2f}  anchor_est={rec.anchor_est:.2f}")

    # --- Step 2: Checkpoints at T+90, T+120, T+180 ---
    for offset in CHECK_OFFSETS_S:
        target_ts = event_start_ts + offset
        sleep_s = max(0, target_ts - int(time.time()))
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)
        else:
            # checkpoint already passed (started mid-window)
            if int(time.time()) - target_ts > 30:
                await _log(f"T+{offset}s already passed, skipping")
                continue

        # Fetch live BTC
        btc, btc_err = await fetch_btc_live(session)
        ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if btc is None or rec.anchor_est is None:
            cp = Checkpoint(
                offset_s=offset, ts_utc=ts_utc,
                btc_live=0, distance=0,
                direction="NONE", triggered=False,
                error=btc_err or "no anchor",
            )
            rec.checkpoints.append(cp)
            await _log(f"T+{offset}s BTC fetch error: {btc_err}")
            continue

        distance = abs(btc - rec.anchor_est)
        direction = "UP" if btc > rec.anchor_est else "DOWN"
        triggered = distance > SIGNAL_THRESHOLD_USD

        # Fetch Polymarket state
        poly = await fetch_poly_state(session, slug)
        poly_err = poly.pop("_error", None) if isinstance(poly, dict) else "bad response"

        bid = poly.get("bid")
        ask = poly.get("ask")
        spread = poly.get("spread")
        liquidity = poly.get("liquidity")
        last_trade = poly.get("last_trade")
        tradeable = (
            triggered
            and spread is not None
            and spread <= TRADEABLE_SPREAD_MAX
            and (liquidity or 0) > 100
        )

        cp = Checkpoint(
            offset_s=offset,
            ts_utc=ts_utc,
            btc_live=btc,
            distance=round(distance, 2),
            direction=direction if triggered else "NONE",
            triggered=triggered,
            poly_bid=bid,
            poly_ask=ask,
            poly_spread=spread,
            poly_liquidity=liquidity,
            poly_last_trade=last_trade,
            tradeable=tradeable,
            error=poly_err if poly_err and not triggered else None,
        )
        rec.checkpoints.append(cp)

        signal_str = f"🔔 SIGNAL {direction}" if triggered else "—"
        await _log(
            f"T+{offset}s  BTC={btc:.2f}  dist={distance:+.2f}  "
            f"poly={bid}/{ask}  {signal_str}"
        )

    # --- Step 3: Wait for endDate, then poll resolution ---
    end_ts = event_start_ts + WINDOW_S
    wait_to_end = max(0, end_ts + 20 - int(time.time()))  # poll starting 20s after end
    await asyncio.sleep(wait_to_end)

    deadline = end_ts + RESOLUTION_TIMEOUT_S
    while int(time.time()) < deadline:
        res = await fetch_resolution(session, slug)
        if res.get("outcome"):
            rec.resolved = True
            rec.outcome = res["outcome"]
            rec.price_to_beat = res["price_to_beat"]
            rec.final_price = res["final_price"]
            rec.resolved_ts = int(time.time())
            break
        await asyncio.sleep(RESOLUTION_POLL_INTERVAL_S)

    if not rec.resolved:
        rec.resolution_error = "timeout waiting for finalPrice"

    # --- Step 4: Log outcome + paper PnL ---
    triggered_cps = rec.triggered_checkpoints()
    if triggered_cps and rec.resolved:
        pnls = [p for p in (rec.paper_pnl(c) for c in triggered_cps) if p is not None]
        wins = sum(1 for c in triggered_cps if c.direction == rec.outcome)
        await _log(
            f"RESOLVED → {rec.outcome}  "
            f"signals={len(triggered_cps)}  wins={wins}  "
            f"pnl={[f'{p:+.4f}' for p in pnls]}"
        )
    elif rec.resolved:
        await _log(f"RESOLVED → {rec.outcome}  (no signals triggered)")
    else:
        await _log(f"UNRESOLVED after timeout")

    # --- Step 5: Save to JSONL (only resolved records count for stats) ---
    if rec.resolved:
        _append_jsonl(rec)
    elif triggered_cps:
        # Save partial record so signals aren't lost on early shutdown;
        # marked unresolved so report skips them for win-rate / PnL.
        _append_jsonl(rec)

    return rec


# ---------------------------------------------------------------------------
# JSONL persistence
# ---------------------------------------------------------------------------

def _append_jsonl(rec: WindowRecord) -> None:
    SIGNALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNALS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec.to_dict()) + "\n")


def _load_jsonl() -> list[WindowRecord]:
    if not SIGNALS_PATH.exists():
        return []
    records = []
    with open(SIGNALS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(WindowRecord.from_dict(json.loads(line)))
                except Exception:
                    pass
    return records


# ---------------------------------------------------------------------------
# Main simulation loop
# ---------------------------------------------------------------------------

_shutdown = False


async def run_simulation() -> None:
    global _shutdown
    print_lock = asyncio.Lock()
    tasks: list[asyncio.Task] = []

    connector = _make_connector()
    async with aiohttp.ClientSession(connector=connector) as session:
        existing = _load_jsonl()
        seen_slugs = {r.slug for r in existing}

        # Handle current window (may have started up to 180s ago — still useful)
        now = int(time.time())
        cur_boundary = (now // WINDOW_S) * WINDOW_S
        elapsed = now - cur_boundary

        # Only latch onto current window if we haven't missed all checkpoints
        if elapsed < CHECK_OFFSETS_S[-1] + 30:  # at least T+180 still future
            slug = f"{SLUG_PREFIX}{cur_boundary}"
            if slug not in seen_slugs:
                t = asyncio.create_task(
                    window_worker(cur_boundary, session, print_lock),
                    name=slug,
                )
                tasks.append(t)
                print(f"[init] Joining current window {slug} ({elapsed}s elapsed)")

        # Loop: spawn a worker for each upcoming window boundary
        while not _shutdown:
            now = int(time.time())
            next_boundary = ((now // WINDOW_S) + 1) * WINDOW_S
            sleep_s = max(1, next_boundary - now - 2)  # wake up 2s before boundary

            # Wait, but check for shutdown every second
            for _ in range(sleep_s):
                if _shutdown:
                    break
                await asyncio.sleep(1)

            if _shutdown:
                break

            slug = f"{SLUG_PREFIX}{next_boundary}"
            if slug not in seen_slugs:
                seen_slugs.add(slug)
                t = asyncio.create_task(
                    window_worker(next_boundary, session, print_lock),
                    name=slug,
                )
                tasks.append(t)
                now_s = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"[{now_s}] Spawned window worker for {slug}")

            # Clean up completed tasks
            tasks = [t for t in tasks if not t.done()]

        # Wait for running tasks to finish
        if tasks:
            print(f"\n[shutdown] Waiting for {len(tasks)} active workers...")
            await asyncio.gather(*tasks, return_exceptions=True)

    print("\n[shutdown] All windows complete. Generating report...")
    generate_report()


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _fmt(v: float, fmt: str = ".4f") -> str:
    return f"{v:{fmt}}" if not (math.isnan(v) if isinstance(v, float) else False) else "N/A"


def generate_report() -> None:
    records = _load_jsonl()
    resolved = [r for r in records if r.resolved]
    all_triggered = [
        (r, cp)
        for r in resolved
        for cp in r.triggered_checkpoints()
    ]

    # Per-offset breakdown
    per_offset: dict[int, list[tuple[WindowRecord, Checkpoint]]] = {}
    for r, cp in all_triggered:
        per_offset.setdefault(cp.offset_s, []).append((r, cp))

    # All PnLs
    all_pnls = [p for r, cp in all_triggered for p in [r.paper_pnl(cp)] if p is not None]
    wins = sum(1 for r, cp in all_triggered if cp.direction == r.outcome)
    tradeable_signals = [(r, cp) for r, cp in all_triggered if cp.tradeable]

    n_windows = len(resolved)
    n_triggered = len(all_triggered)
    win_rate = wins / n_triggered if n_triggered else 0
    mean_pnl = mean(all_pnls) if all_pnls else float("nan")
    med_pnl = median(all_pnls) if all_pnls else float("nan")
    std_pnl = stdev(all_pnls) if len(all_pnls) >= 2 else float("nan")

    lines: list[str] = []
    a = lines.append
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    a("# Paper Anchor Simulation Report")
    a("")
    a(f"> Generated: {ts_now}")
    a(f"> Strategy: anchor_est = Binance_T_open − {ANCHOR_CORRECTION}")
    a(f"> Signal threshold: ${SIGNAL_THRESHOLD_USD}")
    a(f"> Check offsets: T+{CHECK_OFFSETS_S[0]}s / T+{CHECK_OFFSETS_S[1]}s / T+{CHECK_OFFSETS_S[2]}s")
    a(f"> Fee: {TAKER_FEE_RATE*100:.0f}% taker, break-even = 53.5%")
    a("")

    # --- Dataset ---
    a("## 1. Dataset")
    a("")
    a(f"| | Value |")
    a(f"|---|---|")
    a(f"| Total windows recorded | {len(records)} |")
    a(f"| Resolved windows | {n_windows} |")
    a(f"| Total checkpoints with signal | {n_triggered} |")
    a(f"| Unique windows with ≥1 signal | {len(set(r.slug for r, _ in all_triggered))} |")
    if resolved:
        t0 = datetime.fromtimestamp(resolved[0].event_start_ts, tz=timezone.utc)
        t1 = datetime.fromtimestamp(resolved[-1].event_start_ts, tz=timezone.utc)
        a(f"| Time range | {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M} UTC |")
    a("")

    # --- Overall stats ---
    a("## 2. Paper Trading Results (All Signals)")
    a("")
    a(f"| Metric | Value |")
    a(f"|--------|-------|")
    a(f"| Total triggered signals | {n_triggered} |")
    a(f"| Wins (direction correct) | {wins} |")
    a(f"| Win rate | {'N/A' if not n_triggered else f'{win_rate:.1%}'} |")
    a(f"| Mean fee-adj PnL/signal | {'N/A' if math.isnan(mean_pnl) else f'{mean_pnl:+.4f}'} |")
    a(f"| Median fee-adj PnL | {'N/A' if math.isnan(med_pnl) else f'{med_pnl:+.4f}'} |")
    a(f"| StdDev PnL | {'N/A' if math.isnan(std_pnl) else f'{std_pnl:.4f}'} |")
    a(f"| Break-even PnL threshold | -0.0350 (fee at 0.50) |")
    a("")

    # --- Per-offset breakdown ---
    a("## 3. Per-Offset Breakdown")
    a("")
    a(f"| Offset | N | Win Rate | Mean PnL | Median PnL | Tradeable |")
    a(f"|--------|---|---------|---------|-----------|-----------|")
    for offset in sorted(per_offset.keys()):
        pairs = per_offset[offset]
        pnls = [p for r, cp in pairs for p in [r.paper_pnl(cp)] if p is not None]
        o_wins = sum(1 for r, cp in pairs if cp.direction == r.outcome)
        o_wr = o_wins / len(pairs) if pairs else 0
        o_mean = mean(pnls) if pnls else float("nan")
        o_med = median(pnls) if pnls else float("nan")
        o_trade = sum(1 for _, cp in pairs if cp.tradeable)
        a(
            f"| T+{offset}s | {len(pairs)} | {o_wr:.1%} |"
            f" {o_mean:+.4f} | {o_med:+.4f} | {o_trade}/{len(pairs)} |"
        )
    a("")

    # --- CLOB tradability ---
    a("## 4. CLOB Tradability")
    a("")
    if all_triggered:
        spreads = [cp.poly_spread for _, cp in all_triggered if cp.poly_spread is not None]
        liq = [cp.poly_liquidity for _, cp in all_triggered if cp.poly_liquidity is not None]
        spread_ok = sum(1 for s in spreads if s <= TRADEABLE_SPREAD_MAX)
        a(f"| Metric | Value |")
        a(f"|--------|-------|")
        a(f"| Signals with spread data | {len(spreads)} |")
        a(f"| Spread ≤ {TRADEABLE_SPREAD_MAX:.2f} | {spread_ok}/{len(spreads)} ({spread_ok/len(spreads):.1%} if spreads else 'N/A') |")
        if spreads:
            a(f"| Mean spread | {mean(spreads):.3f} |")
            a(f"| Median spread | {median(spreads):.3f} |")
        if liq:
            a(f"| Mean CLOB liquidity | ${mean(liq):,.0f} |")
            a(f"| Min CLOB liquidity | ${min(liq):,.0f} |")
        tradeable_n = len(tradeable_signals)
        tradeable_pnls = [p for r, cp in tradeable_signals for p in [r.paper_pnl(cp)] if p is not None]
        if tradeable_pnls:
            tw = sum(1 for r, cp in tradeable_signals if cp.direction == r.outcome)
            a(f"| Tradeable signals (spread ≤ {TRADEABLE_SPREAD_MAX}) | {tradeable_n} |")
            a(f"| Tradeable win rate | {tw/tradeable_n:.1%} |")
            a(f"| Tradeable mean PnL | {mean(tradeable_pnls):+.4f} |")
    else:
        a("No signal data yet.")
    a("")

    # --- Distance distribution ---
    a("## 5. Signal Distance Distribution")
    a("")
    a("Distance = |BTC_live − anchor_est| at checkpoint time.")
    a("")
    distances = [cp.distance for _, cp in all_triggered]
    if distances:
        for thresh in [40, 60, 80, 100, 150]:
            n_above = sum(1 for d in distances if d >= thresh)
            if n_above == 0:
                continue
            subset = [(r, cp) for r, cp in all_triggered if cp.distance >= thresh]
            sw = sum(1 for r, cp in subset if cp.direction == r.outcome)
            spnls = [p for r, cp in subset for p in [r.paper_pnl(cp)] if p is not None]
            a(f"**Distance ≥ ${thresh}** (N={n_above}, {n_above/len(all_triggered):.0%} of signals):"
              f" win rate = {sw/n_above:.1%},"
              f" mean PnL = {mean(spnls):+.4f}")
        a("")
    else:
        a("No signals yet.")
        a("")

    # --- Anchor quality check (post-hoc) ---
    a("## 6. Anchor Proxy Quality (Post-hoc)")
    a("")
    anchor_deltas = [
        r.binance_t_open - r.price_to_beat
        for r in resolved
        if r.binance_t_open and r.price_to_beat
    ]
    if anchor_deltas:
        a(f"| Statistic | Value |")
        a(f"|-----------|-------|")
        a(f"| N anchors measured | {len(anchor_deltas)} |")
        a(f"| Mean (Binance_T_open − priceToBeat) | {mean(anchor_deltas):+.2f} USD |")
        a(f"| Median | {median(anchor_deltas):+.2f} USD |")
        a(f"| StdDev | {stdev(anchor_deltas):.2f} USD |" if len(anchor_deltas) >= 2 else "")
        a(f"| vs calibrated correction ({ANCHOR_CORRECTION}) | diff={mean(anchor_deltas)-ANCHOR_CORRECTION:+.2f} USD |")
        a("")
        if len(anchor_deltas) >= 2 and abs(mean(anchor_deltas) - ANCHOR_CORRECTION) > 10:
            a(f"> ⚠️  Correction drift: live mean ({mean(anchor_deltas):.2f}) differs from "
              f"calibrated value ({ANCHOR_CORRECTION}) by "
              f"{abs(mean(anchor_deltas)-ANCHOR_CORRECTION):.2f} USD. Consider recalibrating.")
            a("")
    else:
        a("No anchor comparisons yet (need more resolved windows).")
        a("")

    # --- GO/NO-GO ---
    a("## 7. GO / NO-GO")
    a("")

    criteria = [
        ("Windows resolved ≥ 50 (full run ≥ 200)", n_windows >= 50, str(n_windows)),
        ("Triggered signals ≥ 30", n_triggered >= 30, str(n_triggered)),
        ("Win rate ≥ 75%", win_rate >= 0.75, f"{win_rate:.1%}" if n_triggered else "N/A"),
        ("Fee-adj mean PnL > 0", mean_pnl > 0, f"{mean_pnl:+.4f}" if not math.isnan(mean_pnl) else "N/A"),
        ("Median PnL > 0", med_pnl > 0, f"{med_pnl:+.4f}" if not math.isnan(med_pnl) else "N/A"),
    ]
    if spreads if "spreads" in dir() else False:
        pct_tradeable = spread_ok / len(spreads) if spreads else 0  # noqa
        criteria.append(("CLOB tradeable spread (≥ 50% signals)", pct_tradeable >= 0.5, f"{pct_tradeable:.0%}"))

    all_pass = all(v for _, v, _ in criteria)
    verdict = "**✅ GO**" if all_pass else "**❌ NO-GO** (insufficient data or edge not confirmed)"
    if n_windows < 50:
        verdict = f"**⏳ IN PROGRESS** — {n_windows}/50 resolved windows"

    a(f"### Verdict: {verdict}")
    a("")
    a(f"| Criterion | Value | Pass? |")
    a(f"|-----------|-------|-------|")
    for label, passed, val in criteria:
        a(f"| {label} | {val} | {'✅' if passed else '❌'} |")
    a("")

    # --- Recent signals table ---
    if all_triggered:
        a("## 8. Recent Signals (last 20)")
        a("")
        a(f"| Window | Offset | BTC | Anchor | Dist | Dir | Outcome | PnL |")
        a(f"|--------|--------|-----|--------|------|-----|---------|-----|")
        recent = all_triggered[-20:]
        for r, cp in recent:
            pnl = r.paper_pnl(cp)
            pnl_s = f"{pnl:+.3f}" if pnl is not None else "—"
            match = "✅" if cp.direction == r.outcome else "❌"
            anchor_s = f"{r.anchor_est:.0f}" if r.anchor_est else "—"
            a(
                f"| {r.event_start_ts} | T+{cp.offset_s} |"
                f" {cp.btc_live:.0f} | {anchor_s} |"
                f" {cp.distance:.0f} | {cp.direction} | {r.outcome} {match} | {pnl_s} |"
            )
        a("")

    a("---")
    a("*Paper simulation only. No real trades. No wallet access.*")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"[report] Written to {REPORT_PATH}")
    print(f"\n=== SUMMARY ===")
    print(f"  Resolved windows:  {n_windows}")
    print(f"  Triggered signals: {n_triggered}")
    print(f"  Win rate:          {win_rate:.1%}" if n_triggered else "  Win rate:          N/A")
    print(f"  Mean fee-adj PnL:  {mean_pnl:+.4f}" if not math.isnan(mean_pnl) else "  Mean PnL:          N/A")
    print(f"  Median fee-adj PnL:{med_pnl:+.4f}" if not math.isnan(med_pnl) else "  Median PnL:        N/A")
    print(f"  Verdict: {verdict.strip('*')}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true", help="Generate report from existing JSONL and exit")
    args = parser.parse_args()

    if args.report:
        generate_report()
        return

    # Detach from parent process group to survive terminal closure / Bash SIGTERM.
    try:
        import os as _os
        _os.setsid()
    except OSError:
        pass  # already a session leader

    # Setup graceful shutdown
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _handle_signal(sig: int, frame: Any) -> None:
        global _shutdown
        print(f"\n[signal] Shutdown requested ({sig}). Finishing active windows...")
        _shutdown = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # ignore hangup

    print("=" * 60)
    print("  Polymarket BTC 5m Paper Anchor Simulation")
    print(f"  anchor_est = Binance_T_open − {ANCHOR_CORRECTION}")
    print(f"  Signal threshold: ${SIGNAL_THRESHOLD_USD}")
    print(f"  Checks at: T+{CHECK_OFFSETS_S}")
    print(f"  Output: {SIGNALS_PATH}")
    print("  Press Ctrl+C to stop and generate report.")
    print("=" * 60)

    try:
        loop.run_until_complete(run_simulation())
    except Exception as e:
        print(f"[error] {e}")
    finally:
        loop.close()
        generate_report()


if __name__ == "__main__":
    main()
