"""
latency_edge_verifier.py — real-time CEX→Polymarket lag measurement.

Connects to OKX / Binance / Bybit live feeds simultaneously, detects
price jumps, then tracks when (and whether) Polymarket YES prices respond.

Measures:
  • lag_ms from CEX move to first Polymarket reprice
  • follow rates at 1s / 3s / 5s windows
  • fee-adjusted theoretical edge
  • false-signal rate (CEX moved, Poly never followed)
  • opportunity frequency per hour

Verdict: NO EDGE / WEAK EDGE / PAPER EDGE ONLY / EXECUTION-WORTHY EDGE

NO real trading. NO wallet. NO order placement. READ-ONLY.

Usage:
    python3 research/latency_edge_verifier.py --duration 3600
    python3 research/latency_edge_verifier.py --once          # single-cycle test
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
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, stdev
from typing import Optional

import aiohttp

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.binance_client import binance_ws_task, PricePoint as BinancePP
from src.bybit_client import bybit_ws_task, PricePoint as BybitPP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GAMMA_URL   = "https://gamma-api.polymarket.com"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"

# Deterministic slug prefixes for 5-min Up-or-Down markets
ASSET_SLUG_PREFIX = {
    "BTC-USDT": "btc-updown-5m",
    "ETH-USDT": "eth-updown-5m",
    "SOL-USDT": "sol-updown-5m",
}

# CEX move detection
MOVE_WINDOW_S      = 10.0      # look-back window for jump detection
MOVE_THRESHOLD_PCT = 0.002     # 0.2% move in 10s qualifies
CONSENSUS_NEEDED   = 2         # how many CEX sources must agree (out of 3)
SIGNAL_COOLDOWN_S  = 60.0      # per-asset cooldown between signals

# Polymarket polling
POLY_POLL_S        = 2.0       # poll interval (HTTP constraint)

# Lag measurement
MAX_LAG_MS         = 30_000    # give up waiting for Poly reprice after 30s
POLY_FOLLOW_THRESHOLD = 0.003  # 0.3% YES change counts as "Poly repriced"
SAMPLE_HORIZONS_S  = [1, 3, 5, 10, 30, 60, 300]   # forward-return horizons

# Edge calculation
TAKER_FEE_RATE   = 0.07        # 7% of (1 - price)  [standard Poly taker]
SLIPPAGE_PCT     = 0.002       # 0.2% entry slippage estimate

# Verdict thresholds
VERDICT_THRESHOLDS = {
    "follow_rate_3s_min":   0.30,   # <30% → no systematic lag
    "net_edge_p50_min":     0.00,   # net edge must be positive
    "net_edge_p50_paper":   0.005,  # 0.5 cents → paper-only
    "net_edge_p50_exec":    0.035,  # 3.5 cents → covers fee at 0.50
    "opportunity_hr_min":   2.0,    # ≥2 tradeable signals/hr
}

OKX_SYMBOLS = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
ASSET_POLY_KEYWORDS = {
    "BTC-USDT": ["bitcoin", "btc"],
    "ETH-USDT": ["ethereum", "eth"],
    "SOL-USDT": ["solana", "sol"],
}

OUTPUT_JSONL = Path("research/latency_edge_events.jsonl")
OUTPUT_REPORT = Path("reports/latency_edge_verification.md")
LOG_FILE = "research/latency_edge_verifier.log"

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CexTick:
    ts_ms: int
    source: str    # "okx" | "binance" | "bybit"
    asset: str
    price: float


@dataclass
class LagEvent:
    """One CEX-move → Polymarket-response measurement."""
    event_id: int
    ts_utc: str
    asset: str
    market_id: str
    market_title: str
    ttl_s: float

    # CEX trigger
    cex_sources_agreed: list[str]      # which exchanges fired
    cex_move_ts_ms: int
    cex_direction: str                  # "up" | "down"
    cex_move_pct: float                 # abs fractional change
    cex_price_before: float
    cex_price_after: float

    # Polymarket at trigger
    poly_yes_at_trigger: float

    # Forward samples (ts_ms offsets → YES price)
    poly_samples: dict[int, Optional[float]] = field(default_factory=dict)

    # Derived — filled in after all samples collected
    lag_ms: Optional[int] = None            # ms until first Poly reprice
    poly_yes_at_lag: Optional[float] = None
    direction_match: Optional[bool] = None  # did Poly move in CEX direction?
    gross_edge: Optional[float] = None      # best YES price in window - trigger price (signed)
    net_edge: Optional[float] = None        # gross - fee - slippage
    followed_1s: bool = False
    followed_3s: bool = False
    followed_5s: bool = False
    is_false_signal: bool = False            # Poly never repriced within MAX_LAG_MS

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class RunState:
    # Per-asset price histories
    okx_history:     dict[str, deque] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=300)))
    binance_history: dict[str, deque] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=300)))
    bybit_history:   dict[str, deque] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=300)))

    # Polymarket: market_id → deque[CexTick] (price history using CexTick for poly prices)
    poly_history: dict[str, deque] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=300)))
    poly_markets: dict[str, tuple] = field(default_factory=dict)  # market_id → (asset, title, end_ts)

    # Events being tracked and completed
    active_events:    list[LagEvent] = field(default_factory=list)
    completed_events: list[LagEvent] = field(default_factory=list)

    signal_last_ts: dict[str, float] = field(default_factory=dict)
    event_counter: int = 0
    okx_ticks: int = 0
    binance_ticks: int = 0
    bybit_ticks: int = 0
    poly_polls: int = 0
    errors: int = 0
    shutdown: asyncio.Event = field(default_factory=asyncio.Event)
    start_ts: float = field(default_factory=time.monotonic)


state = RunState()
log = logging.getLogger("lev")

# ---------------------------------------------------------------------------
# Pure analysis functions (no I/O — testable)
# ---------------------------------------------------------------------------

def compute_fee(price: float, fee_rate: float = TAKER_FEE_RATE) -> float:
    """Taker fee = rate × (1 − price), applied per unit."""
    return round(fee_rate * (1.0 - price), 6)


def compute_net_edge(
    entry_price: float,
    exit_price: float,
    fee_rate: float = TAKER_FEE_RATE,
    slippage: float = SLIPPAGE_PCT,
) -> float:
    """
    Net edge for a YES long entered at entry_price, marked at exit_price.

    gross = exit_price - entry_price (signed)
    costs = fee(entry_price) + slippage × entry_price
    """
    gross = exit_price - entry_price
    costs = compute_fee(entry_price, fee_rate) + slippage * entry_price
    return round(gross - costs, 6)


def percentile(values: list[float], pct: float) -> float:
    """Simple linear-interpolation percentile (0–100 scale)."""
    if not values:
        return float("nan")
    s = sorted(values)
    n = len(s)
    if n == 1:
        return s[0]
    k = (pct / 100.0) * (n - 1)
    lo, hi = int(k), min(int(k) + 1, n - 1)
    return s[lo] + (k - lo) * (s[hi] - s[lo])


def follow_rate(lag_events: list[LagEvent], horizon_ms: int) -> float:
    """Fraction of events where Poly repriced within horizon_ms."""
    if not lag_events:
        return 0.0
    followed = sum(1 for e in lag_events if e.lag_ms is not None and e.lag_ms <= horizon_ms)
    return followed / len(lag_events)


def classify_edge(
    net_edge_p50: float,
    follow_rate_3s: float,
    opportunity_per_hr: float,
    n_events: int,
) -> str:
    """
    Return verdict: NO EDGE / WEAK EDGE / PAPER EDGE ONLY / EXECUTION-WORTHY EDGE.

    Requires minimum sample size for any positive verdict.
    """
    if n_events < 5:
        return "NO EDGE (insufficient data)"

    t = VERDICT_THRESHOLDS
    if follow_rate_3s < t["follow_rate_3s_min"] or net_edge_p50 <= t["net_edge_p50_min"]:
        return "NO EDGE"
    if net_edge_p50 < t["net_edge_p50_paper"]:
        return "WEAK EDGE"
    if net_edge_p50 < t["net_edge_p50_exec"] or opportunity_per_hr < t["opportunity_hr_min"]:
        return "PAPER EDGE ONLY"
    return "EXECUTION-WORTHY EDGE"


def derive_event_stats(event: LagEvent) -> None:
    """Fill in derived fields once all poly_samples are collected (mutates event in place)."""
    samples = event.poly_samples           # {horizon_ms: YES_price}
    trigger = event.poly_yes_at_trigger

    # --- lag to first reprice ---
    for horizon_ms in sorted(samples):
        price = samples[horizon_ms]
        if price is None:
            continue
        change = abs(price - trigger) / (trigger + 1e-9)
        if change >= POLY_FOLLOW_THRESHOLD:
            if event.lag_ms is None or horizon_ms < event.lag_ms:
                event.lag_ms = horizon_ms
                event.poly_yes_at_lag = price
    event.is_false_signal = event.lag_ms is None

    # --- follow within 1s / 3s / 5s ---
    event.followed_1s = event.lag_ms is not None and event.lag_ms <= 1_000
    event.followed_3s = event.lag_ms is not None and event.lag_ms <= 3_000
    event.followed_5s = event.lag_ms is not None and event.lag_ms <= 5_000

    # --- direction match: did poly move in CEX direction? ---
    if event.lag_ms is not None and event.poly_yes_at_lag is not None:
        poly_moved_up = event.poly_yes_at_lag > trigger
        event.direction_match = (
            poly_moved_up == (event.cex_direction == "up")
        )

    # --- gross edge: best achievable price in sample window (in CEX direction) ---
    prices = [p for p in samples.values() if p is not None]
    if prices:
        if event.cex_direction == "up":
            best = max(prices)
        else:
            best = 1.0 - min(prices)   # for DOWN: NO side, effectively
            trigger = 1.0 - trigger
        event.gross_edge = round(best - trigger, 6)
        event.net_edge = compute_net_edge(trigger, best) if event.gross_edge > 0 else None


# ---------------------------------------------------------------------------
# OKX feed (reuse mvp_runner logic inline, lightweight)
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
_OKX_WS = "wss://ws.okx.com:8443/ws/v5/public"


async def okx_feed_task() -> None:
    args = [{"channel": "tickers", "instId": s} for s in OKX_SYMBOLS]
    connector = aiohttp.TCPConnector(ssl=_SSL)
    async with aiohttp.ClientSession(connector=connector) as session:
        while not state.shutdown.is_set():
            try:
                async with session.ws_connect(_OKX_WS, heartbeat=20) as ws:
                    await ws.send_str(json.dumps({"op": "subscribe", "args": args}))
                    async for msg in ws:
                        if state.shutdown.is_set():
                            return
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            d = json.loads(msg.data)
                            for item in d.get("data") or []:
                                inst = item.get("instId", "")
                                last = item.get("last") or item.get("lastPx")
                                if not last:
                                    continue
                                ts_ms = int(item.get("ts") or time.time() * 1000)
                                tick = CexTick(ts_ms=ts_ms, source="okx", asset=inst, price=float(last))
                                state.okx_history[inst].append(tick)
                                state.okx_ticks += 1
                                _update_poly_samples(tick)
                        except Exception:
                            pass
            except Exception as exc:
                state.errors += 1
                log.debug("OKX WS error: %s", exc)
                await asyncio.sleep(3)


def _binance_bridge(history_dict: dict, source_name: str):
    """Return a task that bridges binance_ws_task output into state."""
    async def _task() -> None:
        hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=300))
        shutdown = asyncio.Event()
        # Link shutdown
        async def _watch():
            await state.shutdown.wait()
            shutdown.set()
        asyncio.create_task(_watch())
        try:
            await binance_ws_task(hist, shutdown)
        except Exception as exc:
            state.errors += 1
            log.debug("%s bridge error: %s", source_name, exc)
        finally:
            # Copy accumulated history into state
            for asset, dq in hist.items():
                for pp in list(dq):
                    tick = CexTick(ts_ms=int(pp.ts * 1000), source=source_name,
                                   asset=asset, price=pp.price)
                    history_dict[asset].append(tick)
                    state.binance_ticks += 1
    return _task()


async def binance_feed_task() -> None:
    hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=500))
    shutdown = asyncio.Event()

    async def _watch():
        await state.shutdown.wait()
        shutdown.set()

    asyncio.create_task(_watch())
    try:
        await binance_ws_task(hist, shutdown)
    except Exception as exc:
        state.errors += 1
        log.debug("Binance feed error: %s", exc)
    # Drain remaining history → this task is fire-and-forget for continuous mode


_BINANCE_ASSETS = {"btcusdt": "BTC", "ethusdt": "ETH", "solusdt": "SOL"}
_BYBIT_ASSETS   = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL"}
_BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade/ethusdt@trade/solusdt@trade"
_BYBIT_WS_URL   = "wss://stream.bybit.com/v5/public/linear"


async def _binance_live_task() -> None:
    """Inline Binance trade stream → writes CexTick to state in real time."""
    connector = aiohttp.TCPConnector(ssl=_SSL)
    async with aiohttp.ClientSession(connector=connector) as session:
        while not state.shutdown.is_set():
            try:
                async with session.ws_connect(_BINANCE_WS_URL, heartbeat=30) as ws:
                    log.debug("[BNB] connected")
                    async for msg in ws:
                        if state.shutdown.is_set():
                            return
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            d = json.loads(msg.data)
                            sym = (d.get("s") or "").lower()
                            asset = _BINANCE_ASSETS.get(sym)
                            price = d.get("p")
                            if not asset or not price:
                                continue
                            ts_ms = int(d.get("T") or time.time() * 1000)
                            tick = CexTick(ts_ms, "binance", asset, float(price))
                            state.binance_history[asset].append(tick)
                            state.binance_ticks += 1
                        except Exception:
                            pass
            except Exception as exc:
                state.errors += 1
                log.debug("Binance WS error: %s", exc)
                await asyncio.sleep(3)


async def _bybit_live_task() -> None:
    """Inline Bybit trade stream → writes CexTick to state in real time."""
    connector = aiohttp.TCPConnector(ssl=_SSL)
    topics = ["publicTrade.BTCUSDT", "publicTrade.ETHUSDT", "publicTrade.SOLUSDT"]
    async with aiohttp.ClientSession(connector=connector) as session:
        while not state.shutdown.is_set():
            try:
                async with session.ws_connect(_BYBIT_WS_URL, heartbeat=20) as ws:
                    await ws.send_str(json.dumps({"op": "subscribe", "args": topics}))
                    log.debug("[BYBIT] connected")
                    async for msg in ws:
                        if state.shutdown.is_set():
                            return
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            d = json.loads(msg.data)
                            for item in d.get("data") or []:
                                sym = item.get("s") or item.get("symbol", "")
                                asset = _BYBIT_ASSETS.get(sym)
                                price = item.get("p")
                                if not asset or not price:
                                    continue
                                ts_ms = int(item.get("T") or time.time() * 1000)
                                tick = CexTick(ts_ms, "bybit", asset, float(price))
                                state.bybit_history[asset].append(tick)
                                state.bybit_ticks += 1
                        except Exception:
                            pass
            except Exception as exc:
                state.errors += 1
                log.debug("Bybit WS error: %s", exc)
                await asyncio.sleep(3)
            state.bybit_ticks += 1


# ---------------------------------------------------------------------------
# Polymarket polling
# ---------------------------------------------------------------------------

async def poly_poll_task() -> None:
    connector = aiohttp.TCPConnector(ssl=_SSL)
    headers = {"User-Agent": "latency-edge-verifier/1.0 (research, read-only)"}
    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        # Discovery
        await _discover_poly(session)
        while not state.shutdown.is_set():
            try:
                await _poll_poly_prices(session)
            except Exception as exc:
                state.errors += 1
                log.debug("Poly poll error: %s", exc)
            try:
                await asyncio.wait_for(asyncio.shield(state.shutdown.wait()), timeout=POLY_POLL_S)
            except asyncio.TimeoutError:
                pass


async def _discover_poly(session: aiohttp.ClientSession) -> None:
    """
    Discover live 5-min Up-or-Down markets via deterministic slug patterns.

    Slug format: {asset}-updown-5m-{window_start_unix}
    Window boundaries are 5-minute UTC multiples.
    Fetches current window + next 2 windows for each asset.
    Falls back to general discovery if deterministic approach yields nothing.
    """
    now = time.time()
    boundary = (int(now) // 300) * 300
    count = 0

    for okx_sym, slug_prefix in ASSET_SLUG_PREFIX.items():
        # Try current window and the next 4 (up to 20 min ahead)
        for offset in range(5):
            ts = boundary + offset * 300
            slug = f"{slug_prefix}-{ts}"
            url = f"{GAMMA_EVENTS}/slug/{slug}"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    r.raise_for_status()
                    d = await r.json(content_type=None)
                if not isinstance(d, dict) or d.get("type") == "not found error":
                    continue
                if d.get("closed"):
                    continue
                markets = d.get("markets") or []
                if not markets:
                    continue
                m = markets[0]
                mid = str(m.get("id") or d.get("id") or "")
                if not mid:
                    continue
                yes_p = _parse_yes(m)
                if yes_p is None:
                    continue
                try:
                    end_dt = datetime.fromisoformat(
                        (m.get("endDate") or "").replace("Z", "+00:00")
                    )
                    ttl = (end_dt - datetime.now(timezone.utc)).total_seconds()
                except Exception:
                    continue
                if ttl < 30:
                    continue   # about to expire
                title = m.get("question") or d.get("title") or slug
                state.poly_markets[mid] = (okx_sym, title[:80], now + ttl)
                count += 1
                log.debug("[POLY] %s mid=%s ttl=%.0fs yes=%.3f", slug, mid, ttl, yes_p)
            except Exception as exc:
                log.debug("[POLY] slug %s error: %s", slug, exc)
                continue

    log.info("[POLY] discovered %d markets via deterministic slugs", count)

    # Fallback: general API if deterministic found nothing
    if count == 0:
        await _discover_poly_fallback(session)


async def _discover_poly_fallback(session: aiohttp.ClientSession) -> None:
    """General Gamma API discovery fallback."""
    now = time.time()
    try:
        params = {"limit": "200", "active": "true", "closed": "false",
                  "order": "startDate", "ascending": "false"}
        async with session.get(f"{GAMMA_URL}/markets", params=params,
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            r.raise_for_status()
            items = await r.json(content_type=None)
        count = 0
        for m in items:
            mid = str(m.get("id") or "")
            q = (m.get("question") or "").lower()
            if not mid or "up or down" not in q:
                continue
            yes_p = _parse_yes(m)
            if yes_p is None:
                continue
            try:
                end_dt = datetime.fromisoformat(
                    (m.get("endDate") or "").replace("Z", "+00:00")
                )
                ttl = (end_dt - datetime.now(timezone.utc)).total_seconds()
            except Exception:
                continue
            if not (30 <= ttl <= 3600):
                continue
            for okx_sym, kws in ASSET_POLY_KEYWORDS.items():
                if any(kw in q for kw in kws):
                    state.poly_markets[mid] = (okx_sym, q[:80], now + ttl)
                    count += 1
                    break
        log.info("[POLY-FB] fallback discovered %d markets", count)
    except Exception as exc:
        log.warning("[POLY-FB] failed: %s", exc)


async def _poll_poly_prices(session: aiohttp.ClientSession) -> None:
    ids = list(state.poly_markets.keys())
    if not ids:
        return
    url = f"{GAMMA_URL}/markets"
    ts_ms = int(time.time() * 1000)
    # Fetch all IDs in one query using repeated ?id= param format
    try:
        # Build URL manually with multiple id params
        id_str = "&".join(f"id={mid}" for mid in ids[:30])   # cap at 30
        full_url = f"{url}?{id_str}"
        async with session.get(full_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            items = await r.json(content_type=None)
        for m in (items if isinstance(items, list) else []):
            mid = str(m.get("id") or "")
            if mid not in state.poly_markets:
                continue
            yes_price = _parse_yes(m)
            if yes_price is None:
                continue
            asset = state.poly_markets[mid][0]
            tick = CexTick(ts_ms=ts_ms, source="poly", asset=asset, price=yes_price)
            state.poly_history[mid].append(tick)
            state.poly_polls += 1
            _update_poly_samples_for_market(mid, ts_ms, yes_price)
    except Exception as exc:
        state.errors += 1
        log.debug("Poly poll error: %s", exc)


def _parse_yes(m: dict) -> Optional[float]:
    op = m.get("outcomePrices")
    try:
        ps = op if isinstance(op, list) else json.loads(op or "[]")
        v = float(ps[0]) if ps else None
        return v if v and 0.0 < v < 1.0 else None
    except Exception:
        return None


def _update_poly_samples(cex_tick: CexTick) -> None:
    """When a CEX tick arrives, update forward samples for active events."""
    now_ms = cex_tick.ts_ms
    for ev in list(state.active_events):
        for h_s in SAMPLE_HORIZONS_S:
            h_ms = h_s * 1000
            target = ev.cex_move_ts_ms + h_ms
            if now_ms >= target and h_ms not in ev.poly_samples:
                # Use the most recent poly price for this market
                hist = state.poly_history.get(ev.market_id)
                price = hist[-1].price if hist else None
                ev.poly_samples[h_ms] = price


def _update_poly_samples_for_market(market_id: str, ts_ms: int, yes_price: float) -> None:
    """When a Poly price arrives, fill in pending samples for active events of this market."""
    for ev in list(state.active_events):
        if ev.market_id != market_id:
            continue
        for h_s in SAMPLE_HORIZONS_S:
            h_ms = h_s * 1000
            target = ev.cex_move_ts_ms + h_ms
            if ts_ms >= target and h_ms not in ev.poly_samples:
                ev.poly_samples[h_ms] = yes_price


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------

async def signal_detector_task() -> None:
    """Every 1s: check for CEX jumps, open new lag events, retire completed ones."""
    while not state.shutdown.is_set():
        try:
            now_ms = int(time.time() * 1000)
            _check_new_signals(now_ms)
            _retire_completed_events(now_ms)
        except Exception as exc:
            state.errors += 1
            log.debug("detector error: %s", exc)
        try:
            await asyncio.wait_for(asyncio.shield(state.shutdown.wait()), timeout=1.0)
        except asyncio.TimeoutError:
            pass


def _check_new_signals(now_ms: int) -> None:
    window_ms = int(MOVE_WINDOW_S * 1000)
    now_mono = time.monotonic()

    for okx_sym, kws in ASSET_POLY_KEYWORDS.items():
        # --- cooldown ---
        if now_mono - state.signal_last_ts.get(okx_sym, 0.0) < SIGNAL_COOLDOWN_S:
            continue

        # --- check each CEX source ---
        moved_up, moved_down = set(), set()
        for source, history in [
            ("okx",     state.okx_history.get(okx_sym)),
            ("binance", state.binance_history.get(okx_sym.split("-")[0])),
            ("bybit",   state.bybit_history.get(okx_sym.split("-")[0])),
        ]:
            if not history or len(history) < 2:
                continue
            latest = history[-1]
            cutoff_ms = now_ms - window_ms
            baseline = next((t for t in history if t.ts_ms >= cutoff_ms), None)
            if baseline is None or baseline.price == 0:
                continue
            pct = (latest.price - baseline.price) / baseline.price
            if pct >= MOVE_THRESHOLD_PCT:
                moved_up.add(source)
            elif pct <= -MOVE_THRESHOLD_PCT:
                moved_down.add(source)

        for direction, sources in [("up", moved_up), ("down", moved_down)]:
            if len(sources) < CONSENSUS_NEEDED:
                continue

            # Find a live Poly market for this asset
            best_mid, best_yes = None, None
            for mid, (asset, title, end_ts) in state.poly_markets.items():
                if asset != okx_sym:
                    continue
                if end_ts < time.time() + 60:
                    continue
                hist = state.poly_history.get(mid)
                if not hist:
                    continue
                yes = hist[-1].price
                if not (0.30 <= yes <= 0.70):
                    continue
                best_mid, best_yes = mid, yes
                break

            if best_mid is None:
                continue

            # Get representative CEX price move
            okx_hist = state.okx_history.get(okx_sym)
            if not okx_hist or len(okx_hist) < 2:
                continue
            latest = okx_hist[-1]
            cutoff_ms = now_ms - window_ms
            baseline = next((t for t in okx_hist if t.ts_ms >= cutoff_ms), None)
            if baseline is None:
                continue
            pct = abs((latest.price - baseline.price) / baseline.price)
            asset_tup = state.poly_markets[best_mid]

            state.event_counter += 1
            ev = LagEvent(
                event_id=state.event_counter,
                ts_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                asset=okx_sym,
                market_id=best_mid,
                market_title=asset_tup[1],
                ttl_s=max(0.0, asset_tup[2] - time.time()),
                cex_sources_agreed=list(sources),
                cex_move_ts_ms=now_ms,
                cex_direction=direction,
                cex_move_pct=pct,
                cex_price_before=baseline.price,
                cex_price_after=latest.price,
                poly_yes_at_trigger=best_yes,
            )
            state.active_events.append(ev)
            state.signal_last_ts[okx_sym] = now_mono
            log.info(
                "[SIGNAL] %s %s %.2f%% | YES=%.3f | sources=%s",
                okx_sym, direction.upper(), pct * 100, best_yes, list(sources),
            )


def _retire_completed_events(now_ms: int) -> None:
    """Move events whose MAX_LAG_MS window has elapsed to completed list."""
    still_active = []
    for ev in state.active_events:
        age_ms = now_ms - ev.cex_move_ts_ms
        max_horizon_ms = SAMPLE_HORIZONS_S[-1] * 1000
        if age_ms >= max(MAX_LAG_MS, max_horizon_ms):
            derive_event_stats(ev)
            state.completed_events.append(ev)
            _append_event(ev)
            log.info(
                "[DONE] id=%d lag=%s follow_1s=%s follow_3s=%s net_edge=%s",
                ev.event_id,
                f"{ev.lag_ms}ms" if ev.lag_ms else "none",
                ev.followed_1s,
                ev.followed_3s,
                f"{ev.net_edge:+.4f}" if ev.net_edge else "n/a",
            )
        else:
            still_active.append(ev)
    state.active_events[:] = still_active


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _append_event(ev: LagEvent) -> None:
    OUTPUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_JSONL, "a") as f:
        f.write(json.dumps(ev.to_dict()) + "\n")


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _fmt(v: Optional[float], fmt: str = ".3f") -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:{fmt}}"


def generate_report(events: list[LagEvent], elapsed_s: float) -> str:
    n = len(events)
    followed = [e for e in events if not e.is_false_signal]
    false_signals = [e for e in events if e.is_false_signal]

    lags_ms = [e.lag_ms for e in followed if e.lag_ms is not None]
    net_edges = [e.net_edge for e in events if e.net_edge is not None]
    gross_edges = [e.gross_edge for e in events if e.gross_edge is not None]

    follow_1s = follow_rate(events, 1_000)
    follow_3s = follow_rate(events, 3_000)
    follow_5s = follow_rate(events, 5_000)
    false_rate = len(false_signals) / n if n else 0.0
    opp_hr = (n / elapsed_s) * 3600 if elapsed_s > 0 else 0.0

    ne_p50 = percentile(net_edges, 50)
    ne_p75 = percentile(net_edges, 75)
    ne_p90 = percentile(net_edges, 90)
    lag_p50 = percentile(lags_ms, 50) if lags_ms else None
    lag_p75 = percentile(lags_ms, 75) if lags_ms else None
    lag_p90 = percentile(lags_ms, 90) if lags_ms else None

    verdict = classify_edge(ne_p50, follow_3s, opp_hr, n)

    lines = []
    a = lines.append
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    a("# Latency Edge Verification Report")
    a("")
    a(f"> Generated: {ts}")
    a(f"> Duration: {elapsed_s/3600:.2f}h  |  Total events: {n}")
    a(f"> Poly poll interval: {POLY_POLL_S}s  |  CEX consensus required: {CONSENSUS_NEEDED}/3")
    a(f"> Move threshold: {MOVE_THRESHOLD_PCT*100:.1f}% in {MOVE_WINDOW_S:.0f}s")
    a(f"> Fee model: {TAKER_FEE_RATE*100:.0f}% taker + {SLIPPAGE_PCT*100:.1f}% slippage")
    a("")

    # Verdict box
    a("## Verdict")
    a("")
    icon = {"NO EDGE": "❌", "WEAK EDGE": "⚠️", "PAPER EDGE ONLY": "📄",
            "EXECUTION-WORTHY EDGE": "✅"}.get(verdict.split(" (")[0], "⚠️")
    a(f"> ## {icon} {verdict}")
    a("")

    # Lag distribution
    a("## 1. Observed Lag Distribution")
    a("")
    a("*Quantization note: Polymarket is polled every 2s via HTTP. "
      "True lag may be lower than measured. All figures are lower bounds.*")
    a("")
    a(f"| Metric | Value |")
    a(f"|--------|-------|")
    a(f"| Total CEX signals detected | {n} |")
    a(f"| Poly repriced within 30s | {len(followed)} ({len(followed)/n:.0%}) |" if n else
      f"| Poly repriced within 30s | 0 (—) |")
    a(f"| False signals (no reprice) | {len(false_signals)} ({false_rate:.0%}) |")
    a(f"| Lag p50 | {_fmt(lag_p50, '.0f')} ms |")
    a(f"| Lag p75 | {_fmt(lag_p75, '.0f')} ms |")
    a(f"| Lag p90 | {_fmt(lag_p90, '.0f')} ms |")
    a("")

    # Follow rates
    a("## 2. Follow Rates by Horizon")
    a("")
    a("*Follow = Polymarket YES price moved ≥ 0.3% in same direction as CEX*")
    a("")
    a(f"| Horizon | Follow Rate | Assessment |")
    a(f"|---------|-------------|------------|")
    for hr, fr, label in [
        (1,  follow_1s, "Very fast — execution-critical"),
        (3,  follow_3s, "Realistic HTTP round-trip"),
        (5,  follow_5s, "Comfortable window"),
    ]:
        flag = "✅" if fr >= 0.40 else ("⚠️" if fr >= 0.20 else "❌")
        a(f"| ≤{hr}s | {fr:.0%} | {flag} {label} |")
    a("")

    # Edge calculation
    a("## 3. Theoretical Edge (paper, fee-adjusted)")
    a("")
    a("*Assumes entry at YES price at moment of CEX signal, exit at best price in 5-min window.*")
    a("")
    a(f"| Metric | Value |")
    a(f"|--------|-------|")
    a(f"| Gross edge p50 | {_fmt(percentile(gross_edges, 50))} |")
    a(f"| Gross edge p75 | {_fmt(percentile(gross_edges, 75))} |")
    a(f"| Net edge p50 (after fee+slip) | {_fmt(ne_p50)} |")
    a(f"| Net edge p75 | {_fmt(ne_p75)} |")
    a(f"| Net edge p90 | {_fmt(ne_p90)} |")
    a(f"| Events with positive net edge | {sum(1 for e in net_edges if e > 0)}/{len(net_edges)} |")
    a(f"| Fee break-even (at YES=0.50) | 0.035 (3.5 cents) |")
    a("")

    # Opportunity frequency
    a("## 4. Opportunity Metrics")
    a("")
    a(f"| Metric | Value |")
    a(f"|--------|-------|")
    a(f"| Elapsed time | {elapsed_s/60:.1f} min |")
    a(f"| Signals / hour | {opp_hr:.1f} |")
    a(f"| OKX ticks | {state.okx_ticks:,} |")
    a(f"| Binance ticks | {state.binance_ticks:,} |")
    a(f"| Bybit ticks | {state.bybit_ticks:,} |")
    a(f"| Polymarket polls | {state.poly_polls:,} |")
    a(f"| Errors | {state.errors} |")
    a("")

    # Per-event table (last 20)
    if events:
        a("## 5. Event Log (most recent 20)")
        a("")
        a("| # | Asset | Dir | Lag ms | Net edge | Follow 1s | Follow 3s | False? |")
        a("|---|-------|-----|--------|----------|-----------|-----------|--------|")
        for ev in events[-20:]:
            a(f"| {ev.event_id} | {ev.asset[:12]} | {ev.cex_direction} "
              f"| {ev.lag_ms or '—'} | {_fmt(ev.net_edge)} "
              f"| {'✅' if ev.followed_1s else '❌'} "
              f"| {'✅' if ev.followed_3s else '❌'} "
              f"| {'yes' if ev.is_false_signal else '—'} |")
        a("")

    # Verdict criteria
    a("## 6. Verdict Criteria Reference")
    a("")
    a("| Verdict | Condition |")
    a("|---------|-----------|")
    a("| NO EDGE | follow_rate_3s < 30% OR net_edge_p50 ≤ 0 |")
    a("| WEAK EDGE | net_edge_p50 ∈ (0, 0.005) |")
    a("| PAPER EDGE ONLY | net_edge_p50 ≥ 0.005 but < 0.035, or opp/hr < 2 |")
    a("| EXECUTION-WORTHY EDGE | net_edge_p50 ≥ 0.035 AND opp/hr ≥ 2 |")
    a("")
    a("**Structural limitation:** Polymarket HTTP polling (2s interval) creates a "
      "measurement floor. Real lag is likely shorter. Any measured lag ≤ 2s should "
      "be treated as 'approximately immediate'. Verdict reflects measurable edge "
      "within this constraint, not theoretical best-case.")
    a("")
    a("---")
    a("*Read-only research. No orders placed. No wallet access.*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

async def run(duration_s: float, once: bool) -> list[LagEvent]:
    tasks = [
        asyncio.create_task(okx_feed_task(),       name="okx"),
        asyncio.create_task(_binance_live_task(),  name="binance"),
        asyncio.create_task(_bybit_live_task(),    name="bybit"),
        asyncio.create_task(poly_poll_task(),      name="poly"),
        asyncio.create_task(signal_detector_task(), name="detector"),
    ]

    if once:
        # In --once mode: wait for Poly discovery, collect for 120s, then exit
        log.info("[once] Waiting for initial Poly discovery…")
        await asyncio.sleep(15)
        log.info("[once] Collecting for 120s…")
        await asyncio.sleep(120)
    else:
        log.info("[run] Running for %.0fs…", duration_s)
        try:
            await asyncio.sleep(duration_s)
        except asyncio.CancelledError:
            pass

    state.shutdown.set()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    # Retire any still-active events
    now_ms = int(time.time() * 1000)
    for ev in list(state.active_events):
        derive_event_stats(ev)
        state.completed_events.append(ev)
        _append_event(ev)
    state.active_events.clear()

    return state.completed_events


def main() -> None:
    parser = argparse.ArgumentParser(description="Latency edge verifier (read-only)")
    parser.add_argument("--duration", type=float, default=3600, help="Run duration seconds (default 3600)")
    parser.add_argument("--once",  action="store_true", help="Quick 120s test run")
    parser.add_argument("--report", type=Path, default=OUTPUT_REPORT)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
    )

    events = asyncio.run(run(duration_s=args.duration, once=args.once))
    elapsed = time.monotonic() - state.start_ts
    report = generate_report(events, elapsed)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(report)
    log.info("[done] %d events  →  %s", len(events), args.report)


if __name__ == "__main__":
    main()
