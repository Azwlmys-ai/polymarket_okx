"""
research/engine.py — Microstructure experiment engine.

Implements:
  Experiment A: Settlement Reversion
  Experiment B: Poly Price Lag
  Experiment C: Spread Distortion

Consumes live data from Polymarket API + OKX WS, outputs tagged signals.
STATS_ONLY — No real orders. No capital at risk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp

from research.models import (
    ExperimentType,
    MarketPhase,
    NewsEvent,
    ResearchSignal,
    SignalDirection,
    VolatilityRegime,
    classify_market_phase,
    classify_volatility_regime,
)

log = logging.getLogger("research.engine")

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

GAMMA_URL = "https://gamma-api.polymarket.com"
OKX_WS_URLS = [
    "wss://ws.okx.com:8443/ws/v5/public",
    "wss://wsaws.okx.com:8443/ws/v5/public",
]
OKX_SYMBOLS = ["BTC-USDT"]

ASSET_KW: dict[str, list[str]] = {
    "BTC": ["bitcoin", "btc"],
}

# Market discovery filters
MIN_LIQUIDITY = float(os.environ.get("RESEARCH_MIN_LIQUIDITY", "500"))
MIN_PRICE = float(os.environ.get("RESEARCH_MIN_PRICE", "0.02"))
MAX_PRICE = float(os.environ.get("RESEARCH_MAX_PRICE", "0.98"))
POLY_POLL_S = float(os.environ.get("RESEARCH_POLL_S", "3.0"))  # faster poll for microstructure

# History windows
POLY_HISTORY_MAXLEN = 600   # keep ~30 min at 3s poll
OKX_HISTORY_MAXLEN = 3000
BTC_HISTORY_MAXLEN = 3000

# Experiment thresholds
SETTLEMENT_WINDOW_S = 180.0
SETTLEMENT_PRICE_BAND = 0.10  # 50±10
REVERSION_HORIZONS = [15, 30, 60]

# Spread distortion thresholds
SPREAD_ALERT_FACTOR = 2.0  # spread > 2x normal
LIQUIDITY_COLLAPSE_FACTOR = 0.3  # liquidity < 30% of average

# Output paths
RAW_EVENTS_PATH = Path("research/raw_events.jsonl")
TAGGED_SIGNALS_PATH = Path("research/tagged_signals.jsonl")
EXPERIMENT_RESULTS_PATH = Path("research/experiment_results.json")
DAILY_REPORT_PATH = Path("research/daily_research_report.md")

# Volatility regime classification
VOL_LOW_THRESH = float(os.environ.get("VOL_LOW_THRESH", "0.0005"))   # 5 bps
VOL_HIGH_THRESH = float(os.environ.get("VOL_HIGH_THRESH", "0.0020"))  # 20 bps


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class PricePoint:
    ts: float
    price: float


@dataclass
class PolyMarketState:
    """Full state for one Polymarket."""
    market_id: str
    question: str
    asset: str
    yes_price: float
    no_price: float
    spread: float
    volume: float
    liquidity: float
    end_ts: float  # unix epoch
    last_price_change: float = 0.0
    yes_history: list[PricePoint] = field(default_factory=list)
    source: str = "gamma"  # "gamma" or "clob"


@dataclass
class EngineState:
    """Global engine state — all live data."""
    # Polymarket
    poly_markets: dict[str, PolyMarketState] = field(default_factory=dict)
    poly_polls: int = 0
    discovery_refreshes: int = 0

    # OKX BTC
    btc_history: list[PricePoint] = field(default_factory=list)
    btc_price: Optional[float] = None
    okx_ticks: int = 0

    # Output buffers
    raw_events: list[ResearchSignal] = field(default_factory=list)
    tagged_signals: list[ResearchSignal] = field(default_factory=list)
    news_events: list[NewsEvent] = field(default_factory=list)

    # Spread history for anomaly detection (per market)
    spread_history: dict[str, list[float]] = field(
        default_factory=lambda: defaultdict(list)
    )
    liquidity_history: dict[str, list[float]] = field(
        default_factory=lambda: defaultdict(list)
    )

    # Pending forward fills
    pending_fills: list[tuple] = field(default_factory=list)

    # Shutdown
    shutdown: asyncio.Event = field(default_factory=asyncio.Event)

    # CLOB token_id lookup: condition_id -> (yes_token_id, no_token_id)
    clob_token_ids: dict[str, tuple[str, str]] = field(default_factory=dict)

    # Stats
    errors: int = 0
    reconnect_counts: dict = field(default_factory=lambda: {"okx": 0})


# ─────────────────────────────────────────────────────────────────────────────
# SSL helper
# ─────────────────────────────────────────────────────────────────────────────


def _make_ssl_ctx() -> ssl.SSLContext:
    if os.environ.get("DISABLE_SSL_VERIFY", "").strip() == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL_CTX = _make_ssl_ctx()


# ─────────────────────────────────────────────────────────────────────────────
# Pure computation functions
# ─────────────────────────────────────────────────────────────────────────────


def compute_return(series: list[PricePoint], start_ts: float, horizon_s: float) -> Optional[float]:
    """Compute percentage return over horizon_s from start_ts."""
    if horizon_s <= 0:
        return None
    target = start_ts + horizon_s
    p_start: Optional[float] = None
    p_end: Optional[float] = None
    for pt in series:
        if pt.ts >= start_ts and p_start is None:
            p_start = pt.price
        if pt.ts >= target and p_end is None:
            p_end = pt.price
        if p_start is not None and p_end is not None:
            break
    if p_start and p_end and p_start != 0:
        return (p_end - p_start) / p_start
    return None


def compute_volatility(series: list[PricePoint], window_s: float, now_ts: float) -> Optional[float]:
    """Compute std of log returns over window_s."""
    cutoff = now_ts - window_s
    prices = [pt.price for pt in series if pt.ts >= cutoff]
    if len(prices) < 3:
        return None
    import math
    log_rets = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    if not log_rets:
        return None
    n = len(log_rets)
    mean = sum(log_rets) / n
    variance = sum((r - mean) ** 2 for r in log_rets) / n
    return variance ** 0.5


def _std(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return (sum((v - mean) ** 2 for v in values) / n) ** 0.5


def _percentile(sv: list[float], p: float) -> float:
    n = len(sv)
    if n == 0:
        return 0.0
    if n == 1:
        return sv[0]
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return sv[lo] + (idx - lo) * (sv[hi] - sv[lo])


def _fmt_pct(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.4%}"


# ─────────────────────────────────────────────────────────────────────────────
# Experiment A: Settlement Reversion
# ─────────────────────────────────────────────────────────────────────────────


def detect_settlement_reversion(
    market: PolyMarketState,
    btc_history: list[PricePoint],
    now_ts: float,
) -> Optional[ResearchSignal]:
    """
    Detect settlement reversion conditions:
    - ttl <= 60s
    - BTC had fast 30s directional move
    - Poly YES still near 50±5
    """
    ttl = market.end_ts - now_ts
    if ttl > SETTLEMENT_WINDOW_S:
        return None

    # Check Poly YES is near 0.50
    if abs(market.yes_price - 0.50) > SETTLEMENT_PRICE_BAND:
        return None

    # Check BTC 30s return for fast directional move
    btc_30s_ret = compute_return(btc_history, now_ts - 30, 30)
    if btc_30s_ret is None or abs(btc_30s_ret) < 0.0003:  # < 0.03% move (relaxed)
        return None

    # Determine direction: if BTC up → signal "drop" (expect reversion down)
    # if BTC down → signal "jump" (expect reversion up)
    direction = SignalDirection.DROP if btc_30s_ret > 0 else SignalDirection.JUMP
    strength = min(abs(btc_30s_ret) / 0.005, 1.0)  # normalize to 0-1

    btc_vol = compute_volatility(btc_history, 60, now_ts)
    btc_price = btc_history[-1].price if btc_history else 0.0

    sig = ResearchSignal(
        timestamp=now_ts,
        market_id=market.market_id,
        market_question=market.question,
        experiment=ExperimentType.SETTLEMENT_REVERSION,
        time_to_expiry_s=ttl,
        poly_yes_price=market.yes_price,
        poly_no_price=market.no_price,
        poly_spread=market.spread,
        poly_volume=market.volume,
        poly_last_price_change=market.last_price_change,
        btc_price=btc_price,
        btc_30s_return=btc_30s_ret,
        btc_60s_return=compute_return(btc_history, now_ts - 60, 60),
        btc_120s_return=compute_return(btc_history, now_ts - 120, 120),
        btc_volatility_60s=btc_vol,
        signal_direction=direction,
        signal_strength=strength,
        trigger_reason=(
            f"settlement_reversion: ttl={ttl:.1f}s btc_30s_ret={btc_30s_ret:.4%} "
            f"yes={market.yes_price:.4f}"
        ),
        market_phase=classify_market_phase(ttl),
        volatility_regime=classify_volatility_regime(btc_vol),
    )
    return sig


# ─────────────────────────────────────────────────────────────────────────────
# Experiment B: Poly Price Lag
# ─────────────────────────────────────────────────────────────────────────────


def detect_poly_price_lag(
    market: PolyMarketState,
    btc_history: list[PricePoint],
    now_ts: float,
) -> Optional[ResearchSignal]:
    """
    Detect when Polymarket YES price moved significantly but OKX BTC hasn't yet.
    Records lead-lag timing.
    """
    # Need at least a few Poly price points
    if len(market.yes_history) < 5:
        return None

    recent_yes = market.yes_history[-5:]
    prices = [p.price for p in recent_yes]
    yes_change = (prices[-1] - prices[0])

    # Only trigger on meaningful YES moves (> 0.02 absolute)
    if abs(yes_change) < 0.02:
        return None

    direction = SignalDirection.JUMP if yes_change > 0 else SignalDirection.DROP
    strength = min(abs(yes_change) / 0.05, 1.0)

    # Check if BTC has already moved in same direction recently
    btc_30s_ret = compute_return(btc_history, now_ts - 30, 30)
    same_direction_btc = (
        (direction == SignalDirection.JUMP and btc_30s_ret and btc_30s_ret > 0.001) or
        (direction == SignalDirection.DROP and btc_30s_ret and btc_30s_ret < -0.001)
    )

    if same_direction_btc:
        return None  # BTC already moved, not a lead signal

    # Record Poly move time
    poly_move_ts = recent_yes[-1].ts if len(recent_yes) >= 2 else now_ts

    btc_vol = compute_volatility(btc_history, 60, now_ts)
    btc_price = btc_history[-1].price if btc_history else 0.0
    ttl = market.end_ts - now_ts

    sig = ResearchSignal(
        timestamp=now_ts,
        market_id=market.market_id,
        market_question=market.question,
        experiment=ExperimentType.POLY_PRICE_LAG,
        time_to_expiry_s=ttl,
        poly_yes_price=market.yes_price,
        poly_no_price=market.no_price,
        poly_spread=market.spread,
        poly_volume=market.volume,
        poly_last_price_change=yes_change,
        btc_price=btc_price,
        btc_30s_return=btc_30s_ret,
        btc_60s_return=compute_return(btc_history, now_ts - 60, 60),
        btc_120s_return=compute_return(btc_history, now_ts - 120, 120),
        btc_volatility_60s=btc_vol,
        signal_direction=direction,
        signal_strength=strength,
        trigger_reason=(
            f"poly_price_lag: yes_change={yes_change:.4f} "
            f"btc_30s_ret={_fmt_pct(btc_30s_ret)}"
        ),
        market_phase=classify_market_phase(ttl),
        volatility_regime=classify_volatility_regime(btc_vol),
        poly_move_timestamp=poly_move_ts,
    )
    return sig


# ─────────────────────────────────────────────────────────────────────────────
# Experiment C: Spread Distortion
# ─────────────────────────────────────────────────────────────────────────────


def detect_spread_distortion(
    market: PolyMarketState,
    spread_history: list[float],
    liquidity_history: list[float],
    now_ts: float,
) -> Optional[ResearchSignal]:
    """
    Detect when spread widens abnormally or liquidity collapses near settlement.
    """
    ttl = market.end_ts - now_ts
    # Only interesting in late/settlement phase
    phase = classify_market_phase(ttl)
    if phase not in (MarketPhase.LATE, MarketPhase.SETTLEMENT):
        return None

    # Need history for baseline
    if len(spread_history) < 5 or len(liquidity_history) < 5:
        return None

    avg_spread = sum(spread_history[-20:]) / len(spread_history[-20:]) if len(spread_history) >= 5 else spread_history[-1]
    avg_liquidity = sum(liquidity_history[-20:]) / len(liquidity_history[-20:]) if len(liquidity_history) >= 5 else liquidity_history[-1]

    current_spread = market.spread
    current_liquidity = market.liquidity

    spread_anomaly = avg_spread > 0 and current_spread > avg_spread * SPREAD_ALERT_FACTOR
    liquidity_collapse = avg_liquidity > 0 and current_liquidity < avg_liquidity * LIQUIDITY_COLLAPSE_FACTOR

    if not (spread_anomaly or liquidity_collapse):
        return None

    reason_parts = []
    if spread_anomaly:
        reason_parts.append(f"spread={current_spread:.4f} vs avg={avg_spread:.4f}")
    if liquidity_collapse:
        reason_parts.append(f"liquidity={current_liquidity:.0f} vs avg={avg_liquidity:.0f}")

    btc_price = 0.0  # will be filled from state
    btc_vol = None

    sig = ResearchSignal(
        timestamp=now_ts,
        market_id=market.market_id,
        market_question=market.question,
        experiment=ExperimentType.SPREAD_DISTORTION,
        time_to_expiry_s=ttl,
        poly_yes_price=market.yes_price,
        poly_no_price=market.no_price,
        poly_spread=current_spread,
        poly_volume=market.volume,
        poly_last_price_change=market.last_price_change,
        btc_price=btc_price,
        btc_volatility_60s=btc_vol,
        signal_direction=SignalDirection.NEUTRAL,
        signal_strength=0.5,
        trigger_reason=f"spread_distortion: {', '.join(reason_parts)}",
        market_phase=phase,
        volatility_regime=VolatilityRegime.MEDIUM,
        spread_before=avg_spread,
        spread_after=current_spread,
        liquidity_before=avg_liquidity,
        liquidity_after=current_liquidity,
    )
    return sig


# ─────────────────────────────────────────────────────────────────────────────
# Forward fill
# ─────────────────────────────────────────────────────────────────────────────


def fill_forward_outcomes(
    signals: list[ResearchSignal],
    btc_history: list[PricePoint],
    poly_histories: dict[str, list[PricePoint]],
    now_ts: float,
) -> list[ResearchSignal]:
    """Fill forward returns for signals whose horizons have elapsed."""
    completed = []
    still_pending = []

    for sig, fill_at in signals:
        if now_ts < fill_at:
            still_pending.append((sig, fill_at))
            continue

        # BTC forward returns
        sig.btc_return_15s = compute_return(btc_history, sig.timestamp, 15)
        sig.btc_return_30s = compute_return(btc_history, sig.timestamp, 30)
        sig.btc_return_60s = compute_return(btc_history, sig.timestamp, 60)
        sig.btc_return_300s = compute_return(btc_history, sig.timestamp, 300)

        # Poly forward returns
        poly_hist = poly_histories.get(sig.market_id, [])
        sig.poly_return_15s = compute_return(poly_hist, sig.timestamp, 15)
        sig.poly_return_30s = compute_return(poly_hist, sig.timestamp, 30)
        sig.poly_return_60s = compute_return(poly_hist, sig.timestamp, 60)
        sig.poly_return_300s = compute_return(poly_hist, sig.timestamp, 300)

        # Lead-lag: find when OKX first moved in signal direction
        if sig.experiment == ExperimentType.POLY_PRICE_LAG and sig.poly_move_timestamp:
            detect_okx_first_move(sig, btc_history)

        completed.append(sig)

    return completed


def detect_okx_first_move(sig: ResearchSignal, btc_history: list[PricePoint]) -> None:
    """
    After poly jump/drop, find the first OKX tick that moved >= 0.05% in the
    same direction. Record lead_lag_ms.
    """
    if not sig.poly_move_timestamp:
        return
    threshold = 0.0005  # 0.05%
    poly_ts = sig.poly_move_timestamp

    # Start scanning from poly move time
    prev_price = None
    for pt in btc_history:
        if pt.ts < poly_ts:
            prev_price = pt.price
            continue
        if prev_price and prev_price != 0:
            ret = (pt.price - prev_price) / prev_price
            if (sig.signal_direction == SignalDirection.JUMP and ret >= threshold) or \
               (sig.signal_direction == SignalDirection.DROP and ret <= -threshold):
                sig.okx_move_timestamp = pt.ts
                sig.lead_lag_ms = (pt.ts - poly_ts) * 1000  # positive = Poly leads
                return
        prev_price = pt.price

    # No matching OKX move found
    sig.lead_lag_ms = None


# ─────────────────────────────────────────────────────────────────────────────
# Async I/O: Polymarket discovery
# ─────────────────────────────────────────────────────────────────────────────


async def poly_discovery_task(state: EngineState) -> None:
    """Discover active BTC Up-or-Down markets."""
    conn = aiohttp.TCPConnector(ssl=_SSL_CTX)
    async with aiohttp.ClientSession(
        headers={"User-Agent": "research-engine/1.0"},
        connector=conn,
    ) as session:
        while not state.shutdown.is_set():
            try:
                await _discover_markets(session, state)
                state.discovery_refreshes += 1
            except Exception as exc:
                state.errors += 1
                log.warning("[DISC] failed: %s", exc)
            try:
                await asyncio.wait_for(
                    asyncio.shield(state.shutdown.wait()), timeout=600.0
                )
            except asyncio.TimeoutError:
                pass


async def _discover_markets(session: aiohttp.ClientSession, state: EngineState) -> None:
    url = f"{GAMMA_URL}/markets"
    timeout = aiohttp.ClientTimeout(total=20)

    new_markets: dict[str, PolyMarketState] = {}
    source_label = "gamma"

    def _ingest_item(m: dict, default_source: str = "gamma") -> Optional[str]:
        """Parse one market item; return market_id if accepted, else None."""
        if not _is_open_market(m):
            return None
        mid = str(m.get("condition_id") or m.get("conditionId") or m.get("id") or "")
        if not mid:
            return None
        question = m.get("question") or m.get("title") or ""
        if not _looks_like_btc_short_term(question, m):
            return None
        end_ts = _parse_end_ts(m)
        if end_ts is None:
            return None
        ttl = end_ts - time.time()
        if ttl <= 0 or ttl > 3600:
            return None
        liq = _as_float(m.get("liquidityClob") or m.get("liquidity") or 0)
        if liq < MIN_LIQUIDITY:
            return None
        yes, no_price = _parse_market_prices(m)
        if yes is None or not (MIN_PRICE < yes < MAX_PRICE):
            return None
        if no_price is None:
            no_price = 1.0 - yes
        spread = abs(yes + no_price - 1.0)
        vol = _as_float(m.get("volumeClob") or m.get("volume") or 0)
        token_ids = _extract_clob_token_ids(m)
        source = "clob" if token_ids else default_source
        new_markets[mid] = PolyMarketState(
            market_id=mid,
            question=question[:120],
            asset="BTC",
            yes_price=yes,
            no_price=no_price,
            spread=spread,
            volume=vol,
            liquidity=liq,
            end_ts=end_ts,
            source=source,
        )
        if token_ids:
            state.clob_token_ids[mid] = token_ids
        return mid

    # ── Phase 0: CLOB read-only short-term discovery ─────────────────────
    clob_markets: dict[str, dict] = {}
    clob_seen = 0
    clob_active = 0
    try:
        # Public CLOB markets are paginated and may include old/closed rows,
        # so all short-term/open filtering stays local and explicit.
        async with session.get(
            f"{CLOB_URL}/markets",
            params={"limit": "1000"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            items = await resp.json(content_type=None)
        if isinstance(items, dict):
            # CLOB may return {"data": [...], ...} or {"markets": [...]}
            items = items.get("data") or items.get("markets") or items.get("results") or []
        if isinstance(items, list):
            clob_seen = len(items)
            for m in items:
                if not isinstance(m, dict):
                    continue
                if _is_open_market(m):
                    clob_active += 1
                mid = _ingest_item(m, default_source="clob")
                if mid:
                    clob_markets[mid] = m
        if clob_markets:
            source_label = "clob"
    except Exception as exc:
        log.warning("[CLOB] discovery failed: %s", exc)

    # ── CLOB-DIAG: short-term distribution from CLOB ─────────────────────
    now_ts_cd = time.time()
    clob_ttls = sorted(
        t for t in (
            (_parse_end_ts(m) or 0.0) - now_ts_cd for m in clob_markets.values()
        ) if t > 0
    )
    clob_short_3600 = sum(1 for t in clob_ttls if t <= 3600)
    clob_short_180 = sum(1 for t in clob_ttls if t <= 180)
    log.info(
        "[CLOB-DIAG] endpoint=%s/markets raw=%d open=%d btc_short=%d "
        "short_term_3600=%d short_term_180=%d ttl_min=%.0fs",
        CLOB_URL, clob_seen, clob_active, len(clob_markets),
        clob_short_3600, clob_short_180, clob_ttls[0] if clob_ttls else -1,
    )

    # ── Phase 1: Gamma standard paginated discovery ────────────
    if not any((m.end_ts - time.time()) <= 3600 for m in new_markets.values()):
        for offset in range(0, 2000, 100):
            if state.shutdown.is_set():
                break
            params = {
                "limit": "100",
                "order": "startDate",
                "ascending": "false",
                "offset": str(offset),
                "active": "true",
                "closed": "false",
            }
            async with session.get(url, params=params, timeout=timeout) as resp:
                resp.raise_for_status()
                items = await resp.json(content_type=None)
            if not isinstance(items, list) or not items:
                break
            for m in items:
                _ingest_item(m)
            if len(items) < 100:
                break

    # ── Phase 2: Gamma search-based short-term discovery ──────────────────
    SEARCH_QUERIES = [
        "Bitcoin up or down",
        "BTC up or down",
        "Bitcoin higher or lower",
        "BTC higher or lower",
        "Bitcoin 5m",
        "Bitcoin 15m",
    ]
    now_ts_s = time.time()
    if not any((m.end_ts - now_ts_s) <= 3600 for m in new_markets.values()):
        search_url = f"{GAMMA_URL}/markets"
        for query in SEARCH_QUERIES:
            if state.shutdown.is_set():
                break
            try:
                async with session.get(
                    search_url,
                    params={
                        "limit": "100",
                        "search": query,
                        "active": "true",
                        "closed": "false",
                    },
                    timeout=timeout,
                ) as resp:
                    resp.raise_for_status()
                    items = await resp.json(content_type=None)
                if not isinstance(items, list) or not items:
                    continue
                if source_label == "gamma":
                    source_label = "search"
                for m in items:
                    _ingest_item(m)
                now_ts_s2 = time.time()
                if any(
                    (m.end_ts - now_ts_s2) <= 3600 for m in new_markets.values()
                ):
                    break
            except Exception:
                continue

    # ── Phase 3: Gamma events fallback ────────────────────────────────────
    now_ts_s3 = time.time()
    if not any((m.end_ts - now_ts_s3) <= 3600 for m in new_markets.values()):
        for query in SEARCH_QUERIES[:4]:
            if state.shutdown.is_set():
                break
            try:
                async with session.get(
                    f"{GAMMA_URL}/events",
                    params={
                        "limit": "25",
                        "search": query,
                        "active": "true",
                        "closed": "false",
                    },
                    timeout=timeout,
                ) as resp:
                    resp.raise_for_status()
                    events = await resp.json(content_type=None)
                if not isinstance(events, list):
                    continue
                for event in events:
                    markets = event.get("markets") if isinstance(event, dict) else None
                    if not isinstance(markets, list):
                        continue
                    for m in markets:
                        if isinstance(m, dict):
                            _ingest_item(m)
                if source_label == "gamma":
                    source_label = "events"
                if any(
                    (m.end_ts - time.time()) <= 3600 for m in new_markets.values()
                ):
                    break
            except Exception:
                continue

    # ── Exclude markets with TTL too far (>24h) ───────────────────────────
    now_ts_t = time.time()
    filtered: dict[str, PolyMarketState] = {}
    for mid, mkt in new_markets.items():
        if (mkt.end_ts - now_ts_t) <= 86400:
            filtered[mid] = mkt
    if filtered:
        new_markets = filtered
    if any(m.source == "clob" for m in new_markets.values()) and source_label != "clob":
        source_label = "mixed"

    state.poly_markets = new_markets
    log.info("[DISC] %d BTC markets discovered", len(new_markets))

    # ── Discovery diagnostic: TTL distribution ──────────────────────────
    now_ts_d = time.time()
    ttls = sorted([m.end_ts - now_ts_d for m in new_markets.values()])
    if ttls:
        n = len(ttls)
        def _pct(ts, p):
            idx = int((p / 100.0) * (n - 1))
            return ts[min(idx, n - 1)]
        short_term_180 = sum(1 for t in ttls if t <= 180)
        short_term_3600 = sum(1 for t in ttls if t <= 3600)
        log.info(
            "[DISCOVERY-DIAG] total=%d btc=%d short_term_180=%d short_term_3600=%d "
            "ttl_min=%.0fs p25=%.0fs p50=%.0fs p75=%.0fs source=%s",
            n, n, short_term_180, short_term_3600,
            ttls[0], _pct(ttls, 25), _pct(ttls, 50), _pct(ttls, 75),
            source_label,
        )
    else:
        log.info("[DISCOVERY-DIAG] total=0 btc=0 short_term_180=0 short_term_3600=0 source=%s", source_label)


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_open_market(m: dict) -> bool:
    if m.get("active") is False:
        return False
    if m.get("closed") is True or m.get("archived") is True:
        return False
    # Gamma uses acceptingOrders, CLOB uses accepting_orders.
    accepting = m.get("accepting_orders")
    if accepting is None:
        accepting = m.get("acceptingOrders")
    return accepting is not False


def _parse_end_ts(m: dict) -> Optional[float]:
    from datetime import datetime, timezone

    raw = (
        m.get("end_date_iso")
        or m.get("endDate")
        or m.get("endDateIso")
        or m.get("end_date")
    )
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        text = f"{text}T00:00:00Z"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _looks_like_btc_short_term(question: str, m: dict) -> bool:
    import re as _re

    blob = " ".join(
        str(v or "") for v in (
            question,
            m.get("title"),
            m.get("market_slug"),
            m.get("slug"),
            m.get("description"),
            m.get("groupItemTitle"),
        )
    ).lower()
    if not any(kw in blob for kw in ASSET_KW.get("BTC", ["bitcoin", "btc"])):
        return False
    if "up or down" not in blob and "higher or lower" not in blob:
        return False
    # 5m/15m markets include a concrete clock time in the title/slug.
    return bool(_re.search(r"\d{1,2}:\d{2}", blob) or "5m" in blob or "15m" in blob)


def _extract_clob_token_ids(m: dict) -> Optional[tuple[str, str]]:
    raw = m.get("clobTokenIds")
    if raw:
        try:
            ids = raw if isinstance(raw, list) else json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            ids = []
        if isinstance(ids, list) and len(ids) >= 2 and ids[0] and ids[1]:
            return str(ids[0]), str(ids[1])

    tokens = m.get("tokens") or []
    yes_tok = None
    no_tok = None
    fallback: list[str] = []
    if isinstance(tokens, list):
        for tok in tokens:
            if not isinstance(tok, dict):
                continue
            tid = str(tok.get("token_id") or tok.get("id") or "")
            if not tid:
                continue
            fallback.append(tid)
            outcome = str(tok.get("outcome") or "").strip().lower()
            if outcome == "yes":
                yes_tok = tid
            elif outcome == "no":
                no_tok = tid
    if yes_tok and no_tok:
        return yes_tok, no_tok
    if len(fallback) >= 2:
        return fallback[0], fallback[1]
    return None


def _parse_market_prices(m: dict) -> tuple[Optional[float], Optional[float]]:
    yes = _parse_yes_price(m)
    no = _parse_no_price(m)
    if yes is not None:
        return yes, no

    tokens = m.get("tokens") or []
    fallback: list[float] = []
    if isinstance(tokens, list):
        for tok in tokens:
            if not isinstance(tok, dict):
                continue
            price = _as_float(tok.get("price"), default=-1.0)
            if price < 0:
                continue
            fallback.append(price)
            outcome = str(tok.get("outcome") or "").strip().lower()
            if outcome == "yes":
                yes = price
            elif outcome == "no":
                no = price
    if yes is None and fallback:
        yes = fallback[0]
    if no is None and len(fallback) >= 2:
        no = fallback[1]
    return yes, no


def _parse_yes_price(m: dict) -> Optional[float]:
    op = m.get("outcomePrices")
    if op:
        try:
            ps = op if isinstance(op, list) else json.loads(op)
            v = float(ps[0]) if ps else None
            return v if v and v > 0 else None
        except Exception:
            pass
    return None


def _parse_no_price(m: dict) -> Optional[float]:
    op = m.get("outcomePrices")
    if op:
        try:
            ps = op if isinstance(op, list) else json.loads(op)
            v = float(ps[1]) if len(ps) > 1 else None
            return v if v and v > 0 else None
        except Exception:
            pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Async I/O: Polymarket poll
# ─────────────────────────────────────────────────────────────────────────────


async def poly_poll_task(state: EngineState) -> None:
    """Poll YES/NO prices every POLY_POLL_S seconds."""
    conn = aiohttp.TCPConnector(ssl=_SSL_CTX)
    async with aiohttp.ClientSession(
        headers={"User-Agent": "research-engine/1.0"},
        connector=conn,
    ) as session:
        # Wait for first discovery
        for _ in range(30):
            if state.poly_markets:
                break
            await asyncio.sleep(2)

        while not state.shutdown.is_set():
            t0 = time.monotonic()
            try:
                await _poll_prices(session, state)
                state.poly_polls += 1
            except Exception as exc:
                state.errors += 1
                log.warning("[POLL] failed: %s", exc)
            elapsed = time.monotonic() - t0
            sleep_s = max(0.0, POLY_POLL_S - elapsed)
            try:
                await asyncio.wait_for(
                    asyncio.shield(state.shutdown.wait()), timeout=sleep_s
                )
            except asyncio.TimeoutError:
                pass


CLOB_URL = "https://clob.polymarket.com"


async def _poll_prices(session: aiohttp.ClientSession, state: EngineState) -> None:
    mids = list(state.poly_markets.keys())
    if not mids:
        return
    gamma_url = f"{GAMMA_URL}/markets"
    timeout = aiohttp.ClientTimeout(total=15)
    BATCH = 50
    now_ts = time.time()

    # Split markets by source
    gamma_mids = [mid for mid in mids if state.poly_markets[mid].source == "gamma"]
    clob_mids = [mid for mid in mids if state.poly_markets[mid].source == "clob"]

    # ── Gamma price poll ──────────────────────────────────────────────────
    for i in range(0, len(gamma_mids), BATCH):
        batch = gamma_mids[i: i + BATCH]
        params = [("id", mid) for mid in batch]
        async with session.get(gamma_url, params=params, timeout=timeout) as resp:
            resp.raise_for_status()
            items = await resp.json(content_type=None)
        if not isinstance(items, list):
            continue
        for m in items:
            mid = str(m.get("id") or "")
            mkt = state.poly_markets.get(mid)
            if mkt is None:
                continue
            yes = _parse_yes_price(m)
            if yes is None:
                continue
            no = _parse_no_price(m)
            if no is None:
                no = 1.0 - yes
            _apply_price_update(mkt, state, mid, yes, no, now_ts)

    # ── CLOB orderbook price poll ─────────────────────────────────────────
    for ci, cid in enumerate(clob_mids):
        tok_ids = state.clob_token_ids.get(cid)
        if not tok_ids:
            continue
        yes_tok, no_tok = tok_ids
        try:
            async with session.get(
                f"{CLOB_URL}/book",
                params={"token_id": yes_tok},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    continue
                book = await resp.json(content_type=None)
            yes_price = _clob_midpoint(book)
            if yes_price is None:
                continue
            async with session.get(
                f"{CLOB_URL}/book",
                params={"token_id": no_tok},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    continue
                book = await resp.json(content_type=None)
            no_price = _clob_midpoint(book)
            if no_price is None:
                no_price = 1.0 - yes_price
            mkt = state.poly_markets.get(cid)
            if mkt is None:
                continue
            _apply_price_update(mkt, state, cid, yes_price, no_price, now_ts)
        except Exception:
            continue


def _clob_midpoint(book: dict) -> Optional[float]:
    """Extract mid price from CLOB orderbook response."""
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None

    def _level_price(level) -> Optional[float]:
        if isinstance(level, dict):
            return _as_float(level.get("price"), default=-1.0)
        if isinstance(level, (int, float, str)):
            return _as_float(level, default=-1.0)
        return None

    try:
        best_bid = _level_price(bids[0])
        best_ask = _level_price(asks[0])
    except IndexError:
        return None
    if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0:
        return None
    return (best_bid + best_ask) / 2.0


def _apply_price_update(
    mkt: PolyMarketState,
    state: EngineState,
    mid: str,
    yes: float,
    no: float,
    now_ts: float,
) -> None:
    """Apply a price update to market state (shared by Gamma + CLOB paths)."""
    mkt.last_price_change = yes - mkt.yes_price
    mkt.yes_price = yes
    mkt.no_price = no
    mkt.spread = abs(yes + no - 1.0)
    mkt.yes_history.append(PricePoint(ts=now_ts, price=yes))
    if len(mkt.yes_history) > POLY_HISTORY_MAXLEN:
        mkt.yes_history = mkt.yes_history[-POLY_HISTORY_MAXLEN:]
    state.spread_history[mid].append(mkt.spread)
    if len(state.spread_history[mid]) > 100:
        state.spread_history[mid] = state.spread_history[mid][-100:]
    state.liquidity_history[mid].append(mkt.liquidity)
    if len(state.liquidity_history[mid]) > 100:
        state.liquidity_history[mid] = state.liquidity_history[mid][-100:]

# ─────────────────────────────────────────────────────────────────────────────
# Async I/O: OKX WS
# ─────────────────────────────────────────────────────────────────────────────


async def okx_ws_task(state: EngineState) -> None:
    """Stream OKX BTC-USDT ticker into state.btc_history."""
    delay = 2.0
    url_idx = 0
    while not state.shutdown.is_set():
        url = OKX_WS_URLS[url_idx % len(OKX_WS_URLS)]
        try:
            conn = aiohttp.TCPConnector(ssl=_SSL_CTX)
            async with aiohttp.ClientSession(connector=conn) as session:
                to = aiohttp.ClientTimeout(connect=15)
                async with session.ws_connect(url, timeout=to) as ws:
                    args = [{"channel": "tickers", "instId": s} for s in OKX_SYMBOLS]
                    await ws.send_str(json.dumps({"op": "subscribe", "args": args}))
                    log.info("[OKX] subscribed: %s", OKX_SYMBOLS)
                    delay = 2.0

                    async def _ping():
                        while not ws.closed:
                            await asyncio.sleep(20)
                            if not ws.closed:
                                await ws.send_str("ping")

                    ping_task = asyncio.create_task(_ping())
                    try:
                        async for msg in ws:
                            if state.shutdown.is_set():
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                if msg.data == "pong":
                                    continue
                                _handle_okx_msg(msg.data, state)
                            elif msg.type in (
                                aiohttp.WSMsgType.ERROR,
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSED,
                            ):
                                break
                    finally:
                        ping_task.cancel()
        except Exception as exc:
            state.errors += 1
            url_idx += 1
            state.reconnect_counts["okx"] += 1
            log.warning("[OKX] disconnected: %s — retry in %.0fs", exc, delay)
            try:
                await asyncio.wait_for(
                    asyncio.shield(state.shutdown.wait()), timeout=delay
                )
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 30.0)


def _handle_okx_msg(raw: str, state: EngineState) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    if msg.get("event"):
        return
    arg = msg.get("arg", {})
    if arg.get("channel") != "tickers":
        return
    data_list = msg.get("data")
    if not data_list:
        return
    item = data_list[0]
    try:
        last = float(item.get("last", 0))
    except (TypeError, ValueError):
        return
    if last == 0:
        return
    ts_str = item.get("ts")
    ts = int(ts_str) / 1000.0 if ts_str else time.time()
    state.btc_history.append(PricePoint(ts=ts, price=last))
    if len(state.btc_history) > BTC_HISTORY_MAXLEN:
        state.btc_history = state.btc_history[-BTC_HISTORY_MAXLEN:]
    state.btc_price = last
    state.okx_ticks += 1


# ─────────────────────────────────────────────────────────────────────────────
# Signal detection loop
# ─────────────────────────────────────────────────────────────────────────────


async def signal_detection_task(state: EngineState) -> None:
    """Run all three experiments every second."""
    cooldowns: dict[str, float] = {}  # market_id -> last signal ts

    while not state.shutdown.is_set():
        now_ts = time.time()
        try:
            _run_experiments(state, now_ts, cooldowns)
            _fill_pending(state, now_ts)
        except Exception as exc:
            state.errors += 1
            log.error("[DETECT] error: %s", exc)
        try:
            await asyncio.wait_for(
                asyncio.shield(state.shutdown.wait()), timeout=1.0
            )
        except asyncio.TimeoutError:
            pass


def _run_experiments(state: EngineState, now_ts: float, cooldowns: dict[str, float]) -> None:
    """Run experiments A, B, C on all active markets."""
    for mid, mkt in list(state.poly_markets.items()):
        # Cooldown: max 1 signal per market per 30s
        if now_ts - cooldowns.get(mid, 0) < 30:
            continue

        # Refresh BTC context from state
        btc_price = state.btc_price or 0.0
        btc_30s = compute_return(state.btc_history, now_ts - 30, 30)
        btc_60s = compute_return(state.btc_history, now_ts - 60, 60)
        btc_120s = compute_return(state.btc_history, now_ts - 120, 120)
        btc_vol = compute_volatility(state.btc_history, 60, now_ts)

        # ── Experiment A: Settlement Reversion ────────────────────────────────
        sig_a = detect_settlement_reversion(mkt, state.btc_history, now_ts)
        if sig_a:
            sig_a.btc_price = btc_price
            sig_a.btc_30s_return = btc_30s
            sig_a.btc_60s_return = btc_60s
            sig_a.btc_120s_return = btc_120s
            sig_a.btc_volatility_60s = btc_vol
            state.raw_events.append(sig_a)
            state.tagged_signals.append(sig_a)
            _append_jsonl(RAW_EVENTS_PATH, sig_a.to_jsonl())
            _append_jsonl(TAGGED_SIGNALS_PATH, sig_a.to_jsonl())
            # Schedule forward fills
            for h in [15, 30, 60, 300]:
                state.pending_fills.append((sig_a, now_ts + h))
            cooldowns[mid] = now_ts
            log.info(
                "[EXP-A] Settlement Rev | %s ttl=%.1fs yes=%.4f "
                "btc_30s=%.4f%% dir=%s",
                mkt.asset, sig_a.time_to_expiry_s, sig_a.poly_yes_price,
                (sig_a.btc_30s_return or 0) * 100, sig_a.signal_direction.value,
            )
            continue  # one signal per poll per market

        # ── Experiment B: Poly Price Lag ──────────────────────────────────────
        sig_b = detect_poly_price_lag(mkt, state.btc_history, now_ts)
        if sig_b:
            sig_b.btc_price = btc_price
            sig_b.btc_30s_return = btc_30s
            sig_b.btc_60s_return = btc_60s
            sig_b.btc_120s_return = btc_120s
            sig_b.btc_volatility_60s = btc_vol
            state.raw_events.append(sig_b)
            state.tagged_signals.append(sig_b)
            _append_jsonl(RAW_EVENTS_PATH, sig_b.to_jsonl())
            _append_jsonl(TAGGED_SIGNALS_PATH, sig_b.to_jsonl())
            for h in [15, 30, 60, 300]:
                state.pending_fills.append((sig_b, now_ts + h))
            cooldowns[mid] = now_ts
            log.info(
                "[EXP-B] Poly Lag | %s yes_change=%.4f dir=%s",
                mkt.asset, sig_b.poly_last_price_change, sig_b.signal_direction.value,
            )
            continue

        # ── Experiment C: Spread Distortion ───────────────────────────────────
        spread_hist = state.spread_history.get(mid, [])
        liq_hist = state.liquidity_history.get(mid, [])
        sig_c = detect_spread_distortion(mkt, spread_hist, liq_hist, now_ts)
        if sig_c:
            sig_c.btc_price = btc_price
            sig_c.btc_30s_return = btc_30s
            sig_c.btc_60s_return = btc_60s
            sig_c.btc_120s_return = btc_120s
            sig_c.btc_volatility_60s = btc_vol
            state.raw_events.append(sig_c)
            state.tagged_signals.append(sig_c)
            _append_jsonl(RAW_EVENTS_PATH, sig_c.to_jsonl())
            _append_jsonl(TAGGED_SIGNALS_PATH, sig_c.to_jsonl())
            for h in [15, 30, 60, 300]:
                state.pending_fills.append((sig_c, now_ts + h))
            cooldowns[mid] = now_ts
            log.info(
                "[EXP-C] Spread Distortion | %s spread=%.4f liq=%.0f phase=%s",
                mkt.asset, sig_c.poly_spread, mkt.liquidity, sig_c.market_phase.value,
            )


def _fill_pending(state: EngineState, now_ts: float) -> None:
    """Fill forward outcomes for signals whose horizon elapsed."""
    still_pending = []
    for sig, fill_at in state.pending_fills:
        if now_ts < fill_at:
            still_pending.append((sig, fill_at))
            continue
        poly_hist = state.poly_markets.get(sig.market_id)
        poly_prices = poly_hist.yes_history if poly_hist else []
        fill_forward_outcomes(
            [(sig, fill_at)], state.btc_history,
            {sig.market_id: poly_prices}, now_ts
        )
        # Re-write to JSONL with filled outcomes
        _append_jsonl(TAGGED_SIGNALS_PATH, sig.to_jsonl(), mode="a")
    state.pending_fills = still_pending


# ─────────────────────────────────────────────────────────────────────────────
# JSONL output helpers
# ─────────────────────────────────────────────────────────────────────────────


def _append_jsonl(path: Path, line: str, mode: str = "a") -> None:
    """Append a line to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as f:
        f.write(line + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Save state (experiment_results.json)
# ─────────────────────────────────────────────────────────────────────────────


def save_experiment_results(state: EngineState) -> None:
    """Write experiment_results.json from current tagged signals."""
    from research.stats import compute_experiment_summaries

    summaries = compute_experiment_summaries(state.tagged_signals)

    result = {
        "experiments": [s.to_dict() for s in summaries],
        "total_signals": len(state.tagged_signals),
        "news_events": len(state.news_events),
        "okx_ticks": state.okx_ticks,
        "poly_polls": state.poly_polls,
        "errors": state.errors,
    }

    EXPERIMENT_RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(EXPERIMENT_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    log.info("[SAVE] experiment_results.json written (%d signals)", len(state.tagged_signals))


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat
# ─────────────────────────────────────────────────────────────────────────────


async def heartbeat_task(state: EngineState, duration_s: float) -> None:
    """Log progress every 60s."""
    elapsed = 0.0
    while not state.shutdown.is_set() and elapsed < duration_s:
        try:
            await asyncio.wait_for(
                asyncio.shield(state.shutdown.wait()), timeout=60.0
            )
        except asyncio.TimeoutError:
            pass
        elapsed += 60.0
        # Count completed signals (have at least btc_return_60s filled)
        complete = sum(
            1 for s in state.tagged_signals if s.btc_return_60s is not None
        )
        log.info(
            "━━ %.0f/%.0fs | BTC ticks: %d | Poly markets: %d polls: %d | "
            "Raw: %d | Tagged: %d (complete: %d) | News: %d | Errors: %d ━━",
            elapsed, duration_s,
            state.okx_ticks, len(state.poly_markets), state.poly_polls,
            len(state.raw_events), len(state.tagged_signals), complete,
            len(state.news_events), state.errors,
        )

        # ── Experiment A funnel diagnostic ──────────────────────────────────
        now_ts_f = time.time()
        total = len(state.poly_markets)
        btc_markets = 0
        ttl_le_180 = 0
        price_40_60 = 0
        btc_move_ge_5bps = False
        final_candidates = 0

        btc_30s_ret = compute_return(state.btc_history, now_ts_f - 30, 30)
        btc_moved = btc_30s_ret is not None and abs(btc_30s_ret) >= 0.0003

        for mkt in state.poly_markets.values():
            btc_markets += 1
            ttl = mkt.end_ts - now_ts_f
            if ttl <= SETTLEMENT_WINDOW_S:
                ttl_le_180 += 1
                if abs(mkt.yes_price - 0.50) <= SETTLEMENT_PRICE_BAND:
                    price_40_60 += 1
                    if btc_moved:
                        final_candidates += 1

        btc_move_ge_5bps = btc_moved
        log.info(
            "[A-DIAG] markets=%d btc_markets=%d ttl_le_180=%d price_40_60=%d "
            "btc_move_ge_5bps=%s final_candidates=%d",
            total, btc_markets, ttl_le_180, price_40_60,
            str(btc_move_ge_5bps), final_candidates,
        )
