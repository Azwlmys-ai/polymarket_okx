"""
latency_threshold_scan.py — CEX move-threshold sensitivity scan.

Single live collection pass → offline replay at 5 thresholds.
No re-connection needed. No real trading. Read-only.

Thresholds tested: 0.05% / 0.10% / 0.15% / 0.20% / 0.30%  (in 10s window)

Output: reports/latency_threshold_scan.md

Usage:
    python3 research/latency_threshold_scan.py --duration 1800   # 30 min
    python3 research/latency_threshold_scan.py --once            # 2 min smoke test
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import ssl
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Optional

import aiohttp

sys.path.insert(0, str(Path(__file__).parent.parent))

# Reuse pure analysis functions from verifier
from research.latency_edge_verifier import (
    LagEvent,
    compute_net_edge,
    derive_event_stats,
    follow_rate,
    percentile,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCAN_THRESHOLDS = [0.0005, 0.001, 0.0015, 0.002, 0.003]   # 0.05 – 0.30 %
MOVE_WINDOW_S   = 10.0
COOLDOWN_S      = 60.0
CONSENSUS_NEED  = 2
MAX_LAG_MS      = 30_000
POLY_FOLLOW_THR = 0.003   # 0.3% YES change = "repriced"
SAMPLE_HORIZONS_S = [1, 3, 5, 10, 30, 60, 300]

TAKER_FEE_RATE = 0.07
SLIPPAGE_PCT   = 0.002

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
GAMMA_MKTS   = "https://gamma-api.polymarket.com/markets"
OKX_WS       = "wss://ws.okx.com:8443/ws/v5/public"
BNB_WS       = "wss://stream.binance.com:9443/ws/btcusdt@trade/ethusdt@trade/solusdt@trade"
BYBIT_WS     = "wss://stream.bybit.com/v5/public/linear"

OKX_SYMBOLS  = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
ASSET_SLUGS  = {"BTC-USDT": "btc-updown-5m",
                "ETH-USDT": "eth-updown-5m",
                "SOL-USDT": "sol-updown-5m"}
BNB_ASSETS   = {"btcusdt": "BTC", "ethusdt": "ETH", "solusdt": "SOL"}
BYBIT_ASSETS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL"}
ASSET_POLY_KW = {"BTC-USDT": ["btc","bitcoin"],
                 "ETH-USDT": ["eth","ethereum"],
                 "SOL-USDT": ["sol","solana"]}

OUTPUT_REPORT = Path("reports/latency_threshold_scan.md")
LOG_FILE      = "research/latency_threshold_scan.log"

# Scan GO conditions
NEXT_STEP_CONDITIONS = {
    "signals_min":       30,
    "signals_hr_min":    5.0,
    "follow_5s_min":     0.50,
    "net_edge_p50_min":  0.0,
}


# ---------------------------------------------------------------------------
# Raw data store (compact format to minimise memory)
# ---------------------------------------------------------------------------

TickList = list[tuple[int, float]]   # [(ts_ms, price)]

@dataclass
class RawStore:
    """Compact store of all raw ticks and Poly polls from one collection session."""
    # "source:asset" → sorted [(ts_ms, price)]
    ticks: dict[str, TickList] = field(default_factory=lambda: defaultdict(list))
    # market_id → sorted [(ts_ms, yes_price)]
    poly_polls: dict[str, list[tuple[int, float]]] = field(
        default_factory=lambda: defaultdict(list))
    # market_id → (okx_sym, title)
    poly_markets: dict[str, tuple] = field(default_factory=dict)
    elapsed_s: float = 0.0
    okx_count:     int = 0
    binance_count: int = 0
    bybit_count:   int = 0
    poly_count:    int = 0
    errors:        int = 0


# ---------------------------------------------------------------------------
# Pure analysis functions (no I/O — testable)
# ---------------------------------------------------------------------------

def price_at_time(ticks: TickList, ts_ms: int) -> Optional[float]:
    """
    Most recent price at or before ts_ms (binary search, O(log n)).
    Returns None if ticks is empty or all ticks are after ts_ms.
    """
    if not ticks:
        return None
    lo, hi = 0, len(ticks) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if ticks[mid][0] <= ts_ms:
            lo = mid + 1
        else:
            hi = mid - 1
    return ticks[hi][1] if hi >= 0 else None


def detect_cex_move(
    ticks_by_source: dict[str, TickList],
    ts_ms: int,
    threshold_pct: float,
    window_ms: int,
) -> tuple[str, list[str]] | None:
    """
    At a single checkpoint ts_ms, check if 2+ sources agree on a threshold move.

    Returns (direction, [sources]) if consensus reached, else None.
    """
    moved_up:   list[str] = []
    moved_down: list[str] = []

    for source, ticks in ticks_by_source.items():
        current  = price_at_time(ticks, ts_ms)
        baseline = price_at_time(ticks, ts_ms - window_ms)
        if current is None or baseline is None or baseline == 0:
            continue
        pct = (current - baseline) / baseline
        if pct >= threshold_pct:
            moved_up.append(source)
        elif pct <= -threshold_pct:
            moved_down.append(source)

    if len(moved_up) >= CONSENSUS_NEED:
        return "up", moved_up
    if len(moved_down) >= CONSENSUS_NEED:
        return "down", moved_down
    return None


def fill_poly_forward_samples(
    event: LagEvent,
    store: RawStore,
    horizons_s: list[int] = SAMPLE_HORIZONS_S,
) -> None:
    """
    Mutate event.poly_samples using stored poll data.
    For each horizon, find the closest Poly poll at or after
    event.cex_move_ts_ms + horizon_ms.
    """
    polls = store.poly_polls.get(event.market_id, [])
    if not polls:
        return
    t0 = event.cex_move_ts_ms
    for h_s in horizons_s:
        h_ms = h_s * 1000
        target = t0 + h_ms
        # Find first poll at or after target (polls are sorted ascending)
        for poll_ts, price in polls:
            if poll_ts >= target:
                event.poly_samples[h_ms] = price
                break


def replay_threshold(
    store: RawStore,
    threshold_pct: float,
    window_s: float = MOVE_WINDOW_S,
    cooldown_s: float = COOLDOWN_S,
) -> list[LagEvent]:
    """
    Replay raw collected data at a given detection threshold.

    Simulates the signal detector in 1-second steps over the entire
    collected window, identifying consensus moves and pairing them with
    stored Polymarket price samples.

    Returns a list of LagEvent objects (with forward samples filled).
    """
    window_ms  = int(window_s * 1000)
    cooldown_ms = int(cooldown_s * 1000)

    # Gather tick range
    all_ts: list[int] = []
    for tl in store.ticks.values():
        if tl:
            all_ts.append(tl[0][0])
            all_ts.append(tl[-1][0])
    if not all_ts:
        return []

    start_ms = min(all_ts)
    end_ms   = max(all_ts)

    events: list[LagEvent] = []
    last_signal_ms: dict[str, int] = {}
    event_id = [0]

    for ts_ms in range(start_ms, end_ms, 1000):
        for okx_sym in OKX_SYMBOLS:
            asset_base = okx_sym.split("-")[0]

            # Build per-source tick lookup for this asset
            ticks_by_src = {
                src: store.ticks.get(f"{src}:{asset_base}", [])
                for src in ("okx", "binance", "bybit")
            }
            # OKX uses full instrument ID as key
            if not ticks_by_src.get("okx"):
                ticks_by_src["okx"] = store.ticks.get(f"okx:{okx_sym}", [])

            result = detect_cex_move(ticks_by_src, ts_ms, threshold_pct, window_ms)
            if result is None:
                continue

            direction, sources = result

            # Cooldown per asset
            last_ms = last_signal_ms.get(okx_sym, 0)
            if ts_ms - last_ms < cooldown_ms:
                continue
            last_signal_ms[okx_sym] = ts_ms

            # Find a Poly market and current YES price
            market_id = _find_poly_market(store, okx_sym, ts_ms)
            if market_id is None:
                continue
            yes_now = _poly_yes_at(store, market_id, ts_ms)
            if yes_now is None or not (0.30 <= yes_now <= 0.70):
                continue

            # OKX prices for the signal
            okx_ticks = ticks_by_src.get("okx", [])
            cur_price  = price_at_time(okx_ticks, ts_ms)
            base_price = price_at_time(okx_ticks, ts_ms - window_ms)
            if cur_price is None or base_price is None:
                continue
            move_pct = abs((cur_price - base_price) / base_price)

            _, title = store.poly_markets.get(market_id, (okx_sym, market_id))
            event_id[0] += 1
            ev = LagEvent(
                event_id=event_id[0],
                ts_utc=_ms_to_utc(ts_ms),
                asset=okx_sym,
                market_id=market_id,
                market_title=title,
                ttl_s=0.0,
                cex_sources_agreed=sources,
                cex_move_ts_ms=ts_ms,
                cex_direction=direction,
                cex_move_pct=move_pct,
                cex_price_before=base_price,
                cex_price_after=cur_price,
                poly_yes_at_trigger=yes_now,
            )
            fill_poly_forward_samples(ev, store)
            derive_event_stats(ev)
            events.append(ev)

    return events


def _find_poly_market(store: RawStore, okx_sym: str, ts_ms: int) -> Optional[str]:
    """Return a market_id for okx_sym that had a price near ts_ms."""
    for mid, (asset, _) in store.poly_markets.items():
        if asset != okx_sym:
            continue
        if _poly_yes_at(store, mid, ts_ms) is not None:
            return mid
    return None


def _poly_yes_at(store: RawStore, market_id: str, ts_ms: int) -> Optional[float]:
    polls = store.poly_polls.get(market_id, [])
    best: Optional[float] = None
    for poll_ts, price in polls:
        if poll_ts <= ts_ms:
            best = price
        else:
            break
    return best


def _ms_to_utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@dataclass
class ThresholdStats:
    threshold_pct: float
    n_signals: int
    signals_per_hour: float
    follow_rate_3s: float
    follow_rate_5s: float
    false_signal_rate: float
    gross_edge_p50: Optional[float]
    net_edge_p50: Optional[float]
    positive_net_edge_count: int
    total_net_edges: int
    verdict: str


def compute_threshold_stats(
    events: list[LagEvent],
    elapsed_s: float,
    threshold_pct: float,
) -> ThresholdStats:
    """Aggregate statistics from one threshold's replay events."""
    n = len(events)
    opp_hr = (n / elapsed_s) * 3600 if elapsed_s > 0 else 0.0

    fr3 = follow_rate(events, 3_000)
    fr5 = follow_rate(events, 5_000)
    false_rate = (sum(1 for e in events if e.is_false_signal) / n) if n else 0.0

    gross_vals = [e.gross_edge for e in events if e.gross_edge is not None]
    net_vals   = [e.net_edge   for e in events if e.net_edge   is not None]

    ge_p50 = percentile(gross_vals, 50) if gross_vals else None
    ne_p50 = percentile(net_vals,   50) if net_vals   else None
    pos    = sum(1 for v in net_vals if v > 0)

    verdict = classify_threshold(n, opp_hr, fr5,
                                 ne_p50 if ne_p50 is not None else float("nan"))
    return ThresholdStats(
        threshold_pct=threshold_pct,
        n_signals=n,
        signals_per_hour=round(opp_hr, 2),
        follow_rate_3s=fr3,
        follow_rate_5s=fr5,
        false_signal_rate=false_rate,
        gross_edge_p50=ge_p50,
        net_edge_p50=ne_p50,
        positive_net_edge_count=pos,
        total_net_edges=len(net_vals),
        verdict=verdict,
    )


def classify_threshold(
    n_signals: int,
    signals_per_hour: float,
    follow_rate_5s: float,
    net_edge_p50: float,
) -> str:
    """
    THRESHOLD TOO STRICT     — too few signals to measure
    NO MEASURABLE EDGE       — enough signals but edge absent
    WEAK PAPER EDGE          — edge present but below fee hurdle
    CANDIDATE THRESHOLD FOUND — meets all GO conditions
    """
    c = NEXT_STEP_CONDITIONS
    if n_signals < c["signals_min"] or signals_per_hour < c["signals_hr_min"]:
        return "THRESHOLD TOO STRICT"
    if math.isnan(net_edge_p50) or net_edge_p50 <= c["net_edge_p50_min"]:
        return "NO MEASURABLE EDGE"
    if follow_rate_5s < c["follow_5s_min"]:
        return "NO MEASURABLE EDGE"
    # Positive edge + decent follow rate
    if net_edge_p50 < TAKER_FEE_RATE * 0.5:   # below half of fee
        return "WEAK PAPER EDGE"
    return "CANDIDATE THRESHOLD FOUND"


# ---------------------------------------------------------------------------
# Live data collection (writes compact tuples into RawStore)
# ---------------------------------------------------------------------------

def _make_ssl() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


_SSL = _make_ssl()
log  = logging.getLogger("lts")


async def _collect(store: RawStore, duration_s: float, once: bool) -> None:
    """Single collection pass — fills store with raw ticks and Poly polls."""
    shutdown = asyncio.Event()

    async def _okx_task() -> None:
        args = [{"channel": "tickers", "instId": s} for s in OKX_SYMBOLS]
        connector = aiohttp.TCPConnector(ssl=_SSL)
        async with aiohttp.ClientSession(connector=connector) as sess:
            while not shutdown.is_set():
                try:
                    async with sess.ws_connect(OKX_WS, heartbeat=20) as ws:
                        await ws.send_str(json.dumps({"op":"subscribe","args":args}))
                        async for msg in ws:
                            if shutdown.is_set(): return
                            if msg.type != aiohttp.WSMsgType.TEXT: continue
                            try:
                                d = json.loads(msg.data)
                                for item in d.get("data") or []:
                                    inst = item.get("instId","")
                                    last = item.get("last") or item.get("lastPx")
                                    if not last: continue
                                    ts = int(item.get("ts") or time.time()*1000)
                                    store.ticks[f"okx:{inst}"].append((ts, float(last)))
                                    store.okx_count += 1
                            except Exception: pass
                except Exception as exc:
                    store.errors += 1; log.debug("OKX: %s", exc)
                    await asyncio.sleep(3)

    async def _bnb_task() -> None:
        connector = aiohttp.TCPConnector(ssl=_SSL)
        async with aiohttp.ClientSession(connector=connector) as sess:
            while not shutdown.is_set():
                try:
                    async with sess.ws_connect(BNB_WS, heartbeat=30) as ws:
                        async for msg in ws:
                            if shutdown.is_set(): return
                            if msg.type != aiohttp.WSMsgType.TEXT: continue
                            try:
                                d = json.loads(msg.data)
                                sym = (d.get("s") or "").lower()
                                asset = BNB_ASSETS.get(sym)
                                price = d.get("p")
                                if not asset or not price: continue
                                ts = int(d.get("T") or time.time()*1000)
                                store.ticks[f"binance:{asset}"].append((ts, float(price)))
                                store.binance_count += 1
                            except Exception: pass
                except Exception as exc:
                    store.errors += 1; log.debug("BNB: %s", exc)
                    await asyncio.sleep(3)

    async def _bybit_task() -> None:
        topics = ["publicTrade.BTCUSDT","publicTrade.ETHUSDT","publicTrade.SOLUSDT"]
        connector = aiohttp.TCPConnector(ssl=_SSL)
        async with aiohttp.ClientSession(connector=connector) as sess:
            while not shutdown.is_set():
                try:
                    async with sess.ws_connect(BYBIT_WS, heartbeat=20) as ws:
                        await ws.send_str(json.dumps({"op":"subscribe","args":topics}))
                        async for msg in ws:
                            if shutdown.is_set(): return
                            if msg.type != aiohttp.WSMsgType.TEXT: continue
                            try:
                                d = json.loads(msg.data)
                                for item in d.get("data") or []:
                                    sym = item.get("s") or item.get("symbol","")
                                    asset = BYBIT_ASSETS.get(sym)
                                    price = item.get("p")
                                    if not asset or not price: continue
                                    ts = int(item.get("T") or time.time()*1000)
                                    store.ticks[f"bybit:{asset}"].append((ts, float(price)))
                                    store.bybit_count += 1
                            except Exception: pass
                except Exception as exc:
                    store.errors += 1; log.debug("BYBIT: %s", exc)
                    await asyncio.sleep(3)

    async def _poly_task() -> None:
        connector = aiohttp.TCPConnector(ssl=_SSL)
        headers   = {"User-Agent":"latency-threshold-scan/1.0 (research)"}
        async with aiohttp.ClientSession(connector=connector,headers=headers) as sess:
            await _poly_discover(sess, store)
            while not shutdown.is_set():
                try:
                    await _poly_poll(sess, store)
                except Exception as exc:
                    store.errors += 1; log.debug("Poly: %s", exc)
                try:
                    await asyncio.wait_for(asyncio.shield(shutdown.wait()), timeout=2.0)
                except asyncio.TimeoutError: pass

    tasks = [
        asyncio.create_task(_okx_task(),   name="okx"),
        asyncio.create_task(_bnb_task(),   name="bnb"),
        asyncio.create_task(_bybit_task(), name="bybit"),
        asyncio.create_task(_poly_task(),  name="poly"),
    ]

    collect_s = 120.0 if once else duration_s
    log.info("[collect] %.0fs collection…", collect_s)
    t0 = time.monotonic()
    await asyncio.sleep(collect_s)
    store.elapsed_s = time.monotonic() - t0

    shutdown.set()
    for t in tasks: t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    # Sort all tick lists by ts_ms
    for key in store.ticks:
        store.ticks[key].sort(key=lambda x: x[0])
    for mid in store.poly_polls:
        store.poly_polls[mid].sort(key=lambda x: x[0])

    log.info(
        "[collect] done: okx=%d bnb=%d bybit=%d poly=%d err=%d",
        store.okx_count, store.binance_count, store.bybit_count,
        store.poly_count, store.errors,
    )


async def _poly_discover(session: aiohttp.ClientSession, store: RawStore) -> None:
    now = time.time()
    boundary = (int(now) // 300) * 300
    count = 0
    for okx_sym, prefix in ASSET_SLUGS.items():
        for off in range(6):
            ts = boundary + off * 300
            url = f"{GAMMA_EVENTS}/slug/{prefix}-{ts}"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    r.raise_for_status()
                    d = await r.json(content_type=None)
                if not isinstance(d, dict) or d.get("closed"): continue
                mkts = d.get("markets") or []
                if not mkts: continue
                m   = mkts[0]
                mid = str(m.get("id") or "")
                if not mid: continue
                try:
                    end_dt = datetime.fromisoformat(
                        (m.get("endDate") or "").replace("Z","+00:00"))
                    ttl = (end_dt - datetime.now(timezone.utc)).total_seconds()
                except Exception: continue
                if ttl < 30: continue
                ep = m.get("outcomePrices","[]")
                try:
                    ps = json.loads(ep) if isinstance(ep,str) else ep
                    yp = float(ps[0]) if ps else None
                except Exception: yp = None
                if yp is None: continue
                title = m.get("question","")[:80]
                store.poly_markets[mid] = (okx_sym, title)
                count += 1
                break
            except Exception: pass
    log.info("[poly] discovered %d markets", count)


async def _poly_poll(session: aiohttp.ClientSession, store: RawStore) -> None:
    ids = list(store.poly_markets.keys())
    if not ids: return
    id_str = "&".join(f"id={mid}" for mid in ids[:30])
    url = f"{GAMMA_MKTS}?{id_str}"
    ts_ms = int(time.time() * 1000)
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
        r.raise_for_status()
        items = await r.json(content_type=None)
    for m in (items if isinstance(items,list) else []):
        mid = str(m.get("id") or "")
        if mid not in store.poly_markets: continue
        ep = m.get("outcomePrices","[]")
        try:
            ps = json.loads(ep) if isinstance(ep,str) else ep
            yp = float(ps[0]) if ps else None
        except Exception: yp = None
        if yp is None: continue
        store.poly_polls[mid].append((ts_ms, yp))
        store.poly_count += 1


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _f(v: Optional[float], fmt: str = ".4f") -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)): return "—"
    return f"{v:{fmt}}"


def generate_report(
    results: list[ThresholdStats],
    store: RawStore,
) -> str:
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dur = store.elapsed_s / 60

    candidates = [r for r in results if r.verdict == "CANDIDATE THRESHOLD FOUND"]
    best = candidates[0] if candidates else None

    conclusion = (
        "CANDIDATE THRESHOLD FOUND" if candidates
        else ("WEAK PAPER EDGE" if any(r.verdict == "WEAK PAPER EDGE" for r in results)
              else ("NO MEASURABLE EDGE"
                    if any(r.verdict == "NO MEASURABLE EDGE" for r in results)
                    else "THRESHOLD TOO STRICT"))
    )

    L: list[str] = []
    a = L.append
    a("# Latency Threshold Sensitivity Scan")
    a("")
    a(f"> Generated: {ts}")
    a(f"> Collection duration: {dur:.1f} min")
    a(f"> OKX ticks: {store.okx_count:,}  Binance: {store.binance_count:,}"
      f"  Bybit: {store.bybit_count:,}  Poly polls: {store.poly_count:,}")
    a(f"> Consensus: {CONSENSUS_NEED}/3 sources  |  Window: {MOVE_WINDOW_S:.0f}s  |"
      f"  Cooldown: {COOLDOWN_S:.0f}s")
    a("")
    icon = {"CANDIDATE THRESHOLD FOUND": "✅", "WEAK PAPER EDGE": "⚠️",
            "NO MEASURABLE EDGE": "❌", "THRESHOLD TOO STRICT": "🔇"}.get(conclusion,"")
    a(f"## Overall Conclusion: {icon} {conclusion}")
    a("")
    a("## Threshold Comparison")
    a("")
    a("| Threshold | Signals | Sig/hr | Follow 3s | Follow 5s | False% | GrossEdge p50 | NetEdge p50 | Verdict |")
    a("|-----------|---------|--------|-----------|-----------|--------|--------------|-------------|---------|")
    for r in results:
        icon2 = "✅" if r.verdict == "CANDIDATE THRESHOLD FOUND" else (
            "⚠️" if r.verdict == "WEAK PAPER EDGE" else
            ("❌" if r.verdict == "NO MEASURABLE EDGE" else "🔇"))
        a(f"| {r.threshold_pct*100:.2f}% "
          f"| {r.n_signals} "
          f"| {r.signals_per_hour:.1f} "
          f"| {r.follow_rate_3s:.0%} "
          f"| {r.follow_rate_5s:.0%} "
          f"| {r.false_signal_rate:.0%} "
          f"| {_f(r.gross_edge_p50)} "
          f"| {_f(r.net_edge_p50)} "
          f"| {icon2} {r.verdict} |")
    a("")
    a("## Verdict Criteria")
    a("")
    a("| Criterion | Threshold |")
    a("|-----------|-----------|")
    c = NEXT_STEP_CONDITIONS
    a(f"| Minimum signals | {c['signals_min']} |")
    a(f"| Minimum signals/hr | {c['signals_hr_min']} |")
    a(f"| Follow rate 5s | ≥ {c['follow_5s_min']:.0%} |")
    a(f"| Net edge p50 | > {c['net_edge_p50_min']} |")
    a("")
    if best:
        a("## Best Candidate")
        a("")
        a(f"**Threshold: {best.threshold_pct*100:.2f}%**")
        a("")
        a(f"| Metric | Value |")
        a(f"|--------|-------|")
        a(f"| Signals | {best.n_signals} |")
        a(f"| Signals/hr | {best.signals_per_hour:.1f} |")
        a(f"| Follow rate 5s | {best.follow_rate_5s:.0%} |")
        a(f"| Net edge p50 | {_f(best.net_edge_p50)} |")
        a(f"| Positive net edge | {best.positive_net_edge_count}/{best.total_net_edges} |")
        a("")
    a("## Structural Limitations")
    a("")
    a("- Polymarket polled every 2s (HTTP). Measured lag has 2s quantization floor.")
    a("- Short collection window → all edge estimates are preliminary.")
    a("- Fee model: 7% taker on (1-price). At YES=0.50: 3.5 cent break-even.")
    a("- Consensus requirement (2/3) reduces noise but may miss fast uni-source moves.")
    a("")
    a("---")
    a("*Read-only. No orders. No wallet.*")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Latency threshold sensitivity scan")
    p.add_argument("--duration", type=float, default=1800, help="Collection seconds (default 1800)")
    p.add_argument("--once",     action="store_true", help="2-min smoke test")
    p.add_argument("--report",   type=Path,  default=OUTPUT_REPORT)
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
    )

    # Phase 1: collect
    store = RawStore()
    asyncio.run(_collect(store, duration_s=args.duration, once=args.once))

    elapsed = store.elapsed_s
    print(f"\n[scan] Collected {elapsed/60:.1f} min | "
          f"OKX={store.okx_count:,} BNB={store.binance_count:,} "
          f"BYBIT={store.bybit_count:,} POLY={store.poly_count:,}")

    # Phase 2: offline replay at each threshold
    results: list[ThresholdStats] = []
    for thr in SCAN_THRESHOLDS:
        events = replay_threshold(store, thr)
        stats  = compute_threshold_stats(events, elapsed, thr)
        results.append(stats)
        print(f"  {thr*100:.2f}%: {stats.n_signals:3d} signals  "
              f"{stats.signals_per_hour:.1f}/hr  "
              f"follow5s={stats.follow_rate_5s:.0%}  "
              f"net_p50={_f(stats.net_edge_p50)}  "
              f"→ {stats.verdict}")

    report = generate_report(results, store)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(f"\n[out] {args.report}")
    print("\n" + "="*60)
    for r in results:
        mark = "★" if r.verdict == "CANDIDATE THRESHOLD FOUND" else " "
        print(f"  {mark} {r.threshold_pct*100:.2f}%  →  {r.verdict}")
    print("="*60)


if __name__ == "__main__":
    main()
