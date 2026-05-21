"""
src/poly_lead_stats.py — Polymarket → OKX + Binance lead signal statistics.

STATS_ONLY / DRY_RUN — No real orders. No wallet. No real capital at risk.

Hypothesis under test:
  Short-term YES price jumps/drops in Polymarket crypto Up-or-Down markets
  LEAD OKX and/or Binance spot price moves within 60–300 seconds.
  Dual-exchange comparison reveals which venue reacts faster.

Usage:
    DISABLE_SSL_VERIFY=1 STATS_ONLY=1 REAL_ORDER=0 \\
        python3 -m src.poly_lead_stats --duration 3600

Output:
    POLY_LEAD_STATS_REPORT.md
    poly_lead_stats.log
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import ssl
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp

from src.binance_client import binance_ws_task
from src.bybit_client import bybit_ws_task

EXCHANGES = ["okx", "binance", "bybit"]

# Maps exchange name → LeadSignal attribute that stores its returns
_RETURNS_ATTR: dict[str, str] = {
    "okx":     "forward_returns",
    "binance": "binance_returns",
    "bybit":   "bybit_returns",
}

# ─────────────────────────────────────────────────────────────────────────────
# SAFETY GUARD — hard-coded, cannot be overridden via env or CLI
# ─────────────────────────────────────────────────────────────────────────────
STATS_ONLY: bool = True
REAL_ORDER: bool = False

# ─────────────────────────────────────────────────────────────────────────────
# Experiment parameters (overridable via environment variables)
# ─────────────────────────────────────────────────────────────────────────────
POLY_JUMP_10S  = float(os.environ.get("POLY_JUMP_10S",  "0.03"))
POLY_JUMP_30S  = float(os.environ.get("POLY_JUMP_30S",  "0.05"))
POLY_JUMP_60S  = float(os.environ.get("POLY_JUMP_60S",  "0.08"))
MIN_LIQUIDITY  = float(os.environ.get("MIN_LIQUIDITY",  "1000"))
MIN_PRICE      = float(os.environ.get("MIN_PRICE",      "0.05"))
MAX_PRICE      = float(os.environ.get("MAX_PRICE",      "0.95"))

# ── TTL filter (avoid expiry convergence noise) ──────────────────────────────
MIN_TTL_S = 30 * 60        # 30 minutes
MAX_TTL_S = 8 * 3600       # 8 hours

# ── Dynamic threshold & activity filter constants ─────────────────────────────
MIN_YES_ACTIVITY  = int(os.environ.get("MIN_YES_ACTIVITY", "3"))
MIN_YES_DELTA     = float(os.environ.get("MIN_YES_DELTA", "0.001"))   # recent 60s std
DYNAMIC_THR_MULT  = float(os.environ.get("DYNAMIC_THR_MULT", "1.8"))  # p80 multiplier
OKX_VOLATILITY_CAP = float(os.environ.get("OKX_VOLATILITY_CAP", "0.005"))  # 0.5%

POLY_POLL_S    = 5.0     # Polymarket poll interval (seconds)
SIGNAL_COOLDOWN_S = 30.0  # Minimum gap between signals for same (market, direction)
FORWARD_HORIZONS  = [60, 180, 300]  # OKX forward-return windows (seconds)
OKX_HISTORY_MAXLEN = 3000           # ~50 min at 1 tick/s

GAMMA_URL = "https://gamma-api.polymarket.com"
OKX_WS_URLS = [
    "wss://ws.okx.com:8443/ws/v5/public",
    "wss://wsaws.okx.com:8443/ws/v5/public",
    "wss://wsap.okx.com:8443/ws/v5/public",
]
OKX_SYMBOLS = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]

ASSET_KW: dict[str, list[str]] = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
    "SOL": ["solana", "sol"],
}

# Windows and their thresholds — ordered for detection loop
JUMP_WINDOWS: list[tuple[float, float]] = [
    (10.0,  POLY_JUMP_10S),
    (30.0,  POLY_JUMP_30S),
    (60.0,  POLY_JUMP_60S),
]

REPORT_PATH = Path("POLY_LEAD_STATS_REPORT.md")

log = logging.getLogger("poly_lead")


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PricePoint:
    ts: float
    price: float


@dataclass
class LeadSignal:
    """One Polymarket YES-price event with corresponding OKX forward returns."""
    ts: float
    asset: str           # BTC / ETH / SOL
    market_id: str
    market_title: str
    direction: str       # "jump" or "drop"
    window_s: float      # which detection window fired (10 / 30 / 60)
    threshold: float
    magnitude: float     # abs(price change) in the window
    yes_before: float
    yes_after: float
    okx_symbol: str      # e.g. "BTC-USDT"
    okx_price_at_trigger: Optional[float]
    # ── Feature engineering fields ────────────────────────────────────────────
    ttl_at_signal: Optional[float] = None        # seconds until market expiry
    dynamic_threshold: float = 0.0               # threshold actually used (after p80 adjust)
    yes_std_60s: Optional[float] = None          # std of YES prices in last 60s
    # Populated later when horizon elapses — per-exchange keyed by seconds
    forward_returns: dict[int, Optional[float]] = field(default_factory=dict)          # OKX
    binance_returns: dict[int, Optional[float]] = field(default_factory=dict)          # Binance
    bybit_returns:   dict[int, Optional[float]] = field(default_factory=dict)          # Bybit


@dataclass
class RunState:
    start_ts: float = field(default_factory=time.monotonic)
    start_wall: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    signals: list[LeadSignal] = field(default_factory=list)
    # market_id → deque[PricePoint] (rolling 120s of YES price history)
    poly_history: dict[str, deque] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=300))
    )
    # okx_symbol → deque[PricePoint]
    okx_history: dict[str, deque] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=OKX_HISTORY_MAXLEN))
    )
    # asset → deque[PricePoint]  (Binance trade stream)
    binance_history: dict[str, deque] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=OKX_HISTORY_MAXLEN))
    )
    # asset → deque[PricePoint]  (Bybit trade stream)
    bybit_history: dict[str, deque] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=OKX_HISTORY_MAXLEN))
    )
    binance_ticks: int = 0
    bybit_ticks: int = 0
    # (market_id, direction) → last signal ts (for cooldown)
    last_signal_ts: dict[tuple, float] = field(default_factory=dict)
    # pending forward-return fills: list of (signal, horizon_s, fill_at_ts)
    pending_returns: list[tuple] = field(default_factory=list)
    # discovered markets: market_id → (asset, title, liquidity, end_ts)
    poly_markets: dict[str, tuple] = field(default_factory=dict)
    # market_id → deque[float] of YES abs deltas (for dynamic threshold)
    poly_yes_deltas: dict[str, deque] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=180))
    )
    # (asset, direction) → last signal ts for cross-market dedup
    _last_signal_asset_ts: dict[tuple, float] = field(default_factory=dict)
    okx_ticks: int = 0
    poly_polls: int = 0
    errors: int = 0
    shutdown: asyncio.Event = field(default_factory=asyncio.Event)
    reconnect_counts: dict = field(default_factory=lambda: {"okx": 0, "binance": 0, "bybit": 0})
    discovery_refreshes: int = 0
    hourly_signals: list = field(default_factory=list)


state = RunState()


# ─────────────────────────────────────────────────────────────────────────────
# Pure computation functions (no I/O — fully unit-testable)
# ─────────────────────────────────────────────────────────────────────────────

def detect_price_event(
    series: list[PricePoint],
    window_s: float,
    threshold: float,
    now_ts: float,
) -> Optional[dict]:
    """
    Detect if YES price jumped or dropped >= threshold in the last window_s seconds.

    Compares the EARLIEST point in [now - window_s, now] against the LATEST point.
    Returns a dict describing the event, or None if no threshold was crossed.

    Args:
        series:    time-ordered list of PricePoint (oldest first)
        window_s:  detection window in seconds
        threshold: minimum absolute price change to trigger
        now_ts:    current timestamp (monotonic or wall; must match series.ts)

    Returns:
        {"direction": "jump"|"drop", "magnitude": float,
         "yes_before": float, "yes_after": float,
         "ts_start": float, "ts_end": float}
        or None
    """
    if len(series) < 2:
        return None

    cutoff = now_ts - window_s
    oldest: Optional[PricePoint] = None
    for pt in series:
        if pt.ts >= cutoff:
            oldest = pt
            break

    if oldest is None:
        return None

    latest = series[-1]
    if latest.ts <= oldest.ts:
        return None

    change = latest.price - oldest.price

    if change >= threshold:
        return {
            "direction": "jump",
            "magnitude": change,
            "yes_before": oldest.price,
            "yes_after": latest.price,
            "ts_start": oldest.ts,
            "ts_end": latest.ts,
        }
    if change <= -threshold:
        return {
            "direction": "drop",
            "magnitude": abs(change),
            "yes_before": oldest.price,
            "yes_after": latest.price,
            "ts_start": oldest.ts,
            "ts_end": latest.ts,
        }
    return None


def compute_forward_return(
    okx_series: list[PricePoint],
    trigger_ts: float,
    horizon_s: float,
) -> Optional[float]:
    """
    Compute OKX percentage return between trigger_ts and trigger_ts + horizon_s.

    Scans the series (oldest first) for the first price at or after trigger_ts,
    then the first price at or after trigger_ts + horizon_s.

    Returns fractional return (e.g. 0.002 = +0.2%), or None if data is missing.
    """
    target_ts = trigger_ts + horizon_s

    price_at_trigger: Optional[float] = None
    for pt in okx_series:
        if pt.ts >= trigger_ts:
            price_at_trigger = pt.price
            break

    if price_at_trigger is None or price_at_trigger == 0.0:
        return None

    price_at_target: Optional[float] = None
    for pt in okx_series:
        if pt.ts >= target_ts:
            price_at_target = pt.price
            break

    if price_at_target is None:
        return None

    return (price_at_target - price_at_trigger) / price_at_trigger


def _std(values: list[float]) -> float:
    """Population standard deviation (for small samples use ddof=0)."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return variance**0.5


def _percentile(sv: list[float], p: float) -> float:
    """Linear-interpolation percentile on a pre-sorted list."""
    n = len(sv)
    if n == 0:
        return 0.0
    if n == 1:
        return sv[0]
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return sv[lo] + (idx - lo) * (sv[hi] - sv[lo])


def _check_cooldown(
    mid: str,
    asset: str,
    direction: str,
    window_s: float,
    now_ts: float,
) -> bool:
    """Return True if this signal should be suppressed by cooldown rules."""
    # single-market cooldown (per window)
    if now_ts - state.last_signal_ts.get((mid, direction, window_s), 0) < 45:
        return True
    # cross-market cooldown: same asset + direction
    if now_ts - state._last_signal_asset_ts.get((asset, direction), 0) < 25:
        return True
    # record timestamps
    state.last_signal_ts[(mid, direction, window_s)] = now_ts
    state._last_signal_asset_ts[(asset, direction)] = now_ts
    return False


def _direction_stats(returns: list[float]) -> dict:
    """Compute descriptive stats for a list of returns."""
    if not returns:
        return {"n": 0, "mean": None, "win_rate": None,
                "median": None, "p25": None, "p75": None}
    sv = sorted(returns)
    n = len(sv)
    mean = sum(sv) / n
    win_rate = sum(1 for r in sv if r > 0) / n
    return {
        "n": n,
        "mean": mean,
        "win_rate": win_rate,
        "median": _percentile(sv, 50),
        "p25": _percentile(sv, 25),
        "p75": _percentile(sv, 75),
    }


def _build_segmented_stats(signals: list[LeadSignal]) -> dict:
    """Segment signals by TTL bucket and compute aligned +60s OKX stats."""
    TTL_BUCKETS = [
        ("0-30min",     0,        30 * 60),
        ("30min-2h",   30 * 60,  2 * 3600),
        ("2h-4h",      2 * 3600, 4 * 3600),
        ("4h-8h",      4 * 3600, 8 * 3600),
    ]
    buckets: dict[str, dict] = {}
    for label, lo, hi in TTL_BUCKETS:
        matched = [
            s for s in signals
            if s.ttl_at_signal is not None and lo <= s.ttl_at_signal < hi
        ]
        aligned_rets = []
        for s in matched:
            r = s.forward_returns.get(60)
            if r is not None:
                aligned_rets.append(r if s.direction == "jump" else -r)
        st = _direction_stats(aligned_rets)
        buckets[label] = {
            "n_signals": len(matched),
            "aligned_60s": st,
        }
    return buckets


def _rank_venues(by_exchange: dict[str, dict]) -> dict:
    """
    Rank exchanges by signal quality. Priority:
      1. Number of horizons with positive expectation (win_rate > 0.5 AND mean > 0)
      2. 60s aligned win_rate
      3. 60s aligned mean return
      4. 60s signal coverage (n)
    Returns best_venue, second_venue, weakest_venue, full ranked list.
    """
    scores: dict[str, tuple] = {}
    for ex in EXCHANGES:
        ex_data = by_exchange.get(ex, {})
        pos_horizons = sum(
            1 for h in FORWARD_HORIZONS
            if (ex_data.get(h, {}).get("aligned", {}).get("mean") or 0.0) > 0
            and (ex_data.get(h, {}).get("aligned", {}).get("win_rate") or 0.0) > 0.5
        )
        wr_60  = ex_data.get(60,  {}).get("aligned", {}).get("win_rate") or 0.0
        mn_60  = ex_data.get(60,  {}).get("aligned", {}).get("mean")     or 0.0
        n_60   = ex_data.get(60,  {}).get("aligned", {}).get("n")        or 0
        scores[ex] = (pos_horizons, wr_60, mn_60, n_60)

    ranked = sorted(EXCHANGES, key=lambda x: scores[x], reverse=True)
    has_any_pos = any(scores[ex][0] > 0 for ex in EXCHANGES)
    return {
        "ranked":        ranked,
        "best_venue":    ranked[0] if ranked else None,
        "second_venue":  ranked[1] if len(ranked) > 1 else None,
        "weakest_venue": ranked[-1] if ranked else None,
        "scores":        {ex: scores[ex] for ex in EXCHANGES},
        "recommend_paper_trade": has_any_pos,
    }


def _exchange_horizon_stats(
    signals: list[LeadSignal], exchange: str
) -> dict[int, dict]:
    """Return per-horizon aligned stats for a single exchange."""
    returns_attr = _RETURNS_ATTR.get(exchange, "forward_returns")
    by_h: dict[int, dict] = {}
    for h in FORWARD_HORIZONS:
        jump_rets = [
            getattr(s, returns_attr).get(h)
            for s in signals
            if s.direction == "jump" and getattr(s, returns_attr).get(h) is not None
        ]
        drop_rets = [
            getattr(s, returns_attr).get(h)
            for s in signals
            if s.direction == "drop" and getattr(s, returns_attr).get(h) is not None
        ]
        aligned = jump_rets + [-r for r in drop_rets]
        by_h[h] = {
            "jump":    _direction_stats(jump_rets),
            "drop":    _direction_stats(drop_rets),
            "aligned": _direction_stats(aligned),
        }
    return by_h


def build_stats_report(signals: list[LeadSignal], elapsed_s: float) -> dict:
    """
    Aggregate LeadSignal list into summary statistics for OKX and Binance.

    For each horizon, jump/drop directions are analysed separately.
    "aligned" stats flip drop returns (expect negative → positive if lead holds).
    """
    if not signals:
        return {
            "total_signals": 0,
            "by_asset": {},
            "by_direction": {"jump": 0, "drop": 0},
            "by_horizon": {},
            "by_exchange": {ex: {} for ex in EXCHANGES},
            "exchange_comparison": {},
            "venue_ranking": _rank_venues({ex: {} for ex in EXCHANGES}),
            "has_positive_expectation": False,
            "elapsed_s": elapsed_s,
        }

    by_asset: dict[str, int] = defaultdict(int)
    for s in signals:
        by_asset[s.asset] += 1

    by_direction = {
        "jump": sum(1 for s in signals if s.direction == "jump"),
        "drop": sum(1 for s in signals if s.direction == "drop"),
    }

    # OKX horizon stats (primary — backward compat key)
    by_horizon: dict[int, dict] = _exchange_horizon_stats(signals, "okx")

    # Per-exchange stats
    by_exchange: dict[str, dict] = {
        ex: _exchange_horizon_stats(signals, ex) for ex in EXCHANGES
    }

    # Exchange comparison: which has higher aligned win_rate at each horizon?
    comparison: dict[int, dict] = {}
    for h in FORWARD_HORIZONS:
        okx_wr = by_exchange["okx"][h]["aligned"].get("win_rate")
        bnb_wr = by_exchange["binance"][h]["aligned"].get("win_rate")
        okx_mn = by_exchange["okx"][h]["aligned"].get("mean")
        bnb_mn = by_exchange["binance"][h]["aligned"].get("mean")
        if okx_mn is not None and bnb_mn is not None:
            leader = "binance" if bnb_mn > okx_mn else "okx"
        else:
            leader = "binance" if (bnb_mn is not None) else "okx"
        comparison[h] = {
            "leader": leader,
            "okx_win_rate": okx_wr,
            "bnb_win_rate": bnb_wr,
            "okx_mean": okx_mn,
            "bnb_mean": bnb_mn,
        }

    has_pos = any(
        h_data["aligned"].get("mean") is not None
        and h_data["aligned"]["mean"] > 0
        and h_data["aligned"].get("win_rate") is not None
        and h_data["aligned"]["win_rate"] > 0.5
        for h_data in by_horizon.values()
    )

    venue_ranking = _rank_venues(by_exchange)

    return {
        "total_signals": len(signals),
        "by_asset": dict(by_asset),
        "by_direction": by_direction,
        "by_horizon": by_horizon,
        "by_exchange": by_exchange,
        "exchange_comparison": comparison,
        "venue_ranking": venue_ranking,
        "has_positive_expectation": has_pos,
        "elapsed_s": elapsed_s,
    }


def format_markdown_report(
    report: dict,
    signals: list[LeadSignal],
    long_run_stats: Optional[dict] = None,
) -> str:
    """Render the stats report as Markdown. STATS_ONLY — no orders placed."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    elapsed = report["elapsed_s"]
    h_str = f"{elapsed/3600:.1f}h" if elapsed >= 3600 else f"{elapsed:.0f}s"

    def _fmt_pct(v: Optional[float]) -> str:
        return f"{v:.4%}" if v is not None else "N/A"

    def _fmt_stat(st: dict) -> str:
        if st.get("n", 0) == 0:
            return "no data"
        return (
            f"n={st['n']} mean={_fmt_pct(st.get('mean'))} "
            f"win={_fmt_pct(st.get('win_rate'))} "
            f"med={_fmt_pct(st.get('median'))} "
            f"p25={_fmt_pct(st.get('p25'))} p75={_fmt_pct(st.get('p75'))}"
        )

    lines = [
        "# POLY_LEAD_STATS_REPORT",
        "",
        "> **STATS_ONLY / DRY_RUN — No real orders placed. No capital at risk.**",
        "",
        f"Generated: {now}  ",
        f"Elapsed: {h_str}  ",
        f"Thresholds: JUMP_10S={POLY_JUMP_10S} | JUMP_30S={POLY_JUMP_30S} | JUMP_60S={POLY_JUMP_60S}  ",
        f"Filters: MIN_LIQ={MIN_LIQUIDITY:.0f} | YES=[{MIN_PRICE},{MAX_PRICE}]",
        f"TTL: [{MIN_TTL_S/60:.0f}min - {MAX_TTL_S/3600:.1f}h] | ACTIVITY>={MIN_YES_ACTIVITY} | STD>={MIN_YES_DELTA:.3f} | DYN_MULT={DYNAMIC_THR_MULT}x | OKX_CAP<={OKX_VOLATILITY_CAP*100:.1f}%",
        "",
        "---",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Total signals | **{report['total_signals']}** |",
        f"| Jump signals | {report['by_direction'].get('jump', 0)} |",
        f"| Drop signals | {report['by_direction'].get('drop', 0)} |",
        f"| Has positive expectation | {'✅ YES' if report['has_positive_expectation'] else '❌ NO'} |",
        "",
    ]

    if report["by_asset"]:
        lines += ["## By Asset", "", "| Asset | Signals |", "|---|---|"]
        for asset in sorted(report["by_asset"]):
            lines.append(f"| {asset} | {report['by_asset'][asset]} |")
        lines.append("")

    # ── Per-exchange forward return analysis ─────────────────────────────────
    for exchange in EXCHANGES:
        label = exchange.upper()
        lines += [f"## {label} Forward Return Analysis", ""]
        ex_data = report.get("by_exchange", {}).get(exchange, report.get("by_horizon", {}))
        for h in FORWARD_HORIZONS:
            h_data = ex_data.get(h, {})
            lines.append(f"### {label} +{h}s after Poly YES event")
            lines.append("")
            lines.append(f"- **jump**: {_fmt_stat(h_data.get('jump', {}))}")
            lines.append(f"- **drop**: {_fmt_stat(h_data.get('drop', {}))}")
            lines.append(f"- **aligned**: {_fmt_stat(h_data.get('aligned', {}))}")
            lines.append("")

    # ── Exchange Comparison ───────────────────────────────────────────────────
    lines += ["---", "", "## Exchange Comparison", ""]
    lines.append(
        "| exchange | signals | win_rate_60s | mean_60s | median_60s "
        "| mean_180s | median_180s | mean_300s | median_300s |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for exchange in EXCHANGES:
        ex_data = report.get("by_exchange", {}).get(exchange, {})
        n_total = report["total_signals"]
        win60  = ex_data.get(60,  {}).get("aligned", {}).get("win_rate")
        mn60   = ex_data.get(60,  {}).get("aligned", {}).get("mean")
        med60  = ex_data.get(60,  {}).get("aligned", {}).get("median")
        mn180  = ex_data.get(180, {}).get("aligned", {}).get("mean")
        med180 = ex_data.get(180, {}).get("aligned", {}).get("median")
        mn300  = ex_data.get(300, {}).get("aligned", {}).get("mean")
        med300 = ex_data.get(300, {}).get("aligned", {}).get("median")
        lines.append(
            f"| {exchange} | {n_total} "
            f"| {_fmt_pct(win60)} | {_fmt_pct(mn60)} | {_fmt_pct(med60)} "
            f"| {_fmt_pct(mn180)} | {_fmt_pct(med180)} "
            f"| {_fmt_pct(mn300)} | {_fmt_pct(med300)} |"
        )
    lines.append("")

    # ── Venue Ranking ─────────────────────────────────────────────────────────
    vr = report.get("venue_ranking", {})
    if vr:
        lines += ["## Venue Ranking", ""]
        ranked = vr.get("ranked", EXCHANGES)
        medal = {0: "🥇", 1: "🥈", 2: "🥉"}
        for rank, ex in enumerate(ranked):
            score = vr.get("scores", {}).get(ex, (0, 0, 0, 0))
            pos_h, wr60, mn60, n60 = score
            lines.append(
                f"{medal.get(rank, f'{rank+1}.')} **{ex.upper()}** — "
                f"pos_horizons={pos_h} | win60={_fmt_pct(wr60)} | "
                f"mean60={_fmt_pct(mn60)} | n60={n60}"
            )
        lines.append("")
        best = vr.get("best_venue")
        second = vr.get("second_venue")
        weakest = vr.get("weakest_venue")
        if best:
            lines.append(f"- **best_venue:** `{best.upper()}`")
        if second:
            lines.append(f"- **second_venue:** `{second.upper()}`")
        if weakest:
            lines.append(f"- **weakest_venue:** `{weakest.upper()}`")
        lines.append(
            f"- **recommend_paper_trade:** "
            f"{'✅ Yes — at least one venue shows positive expectation' if vr.get('recommend_paper_trade') else '❌ No — insufficient positive expectation'}"
        )
    lines.append("")

    # ── Recent Signals ────────────────────────────────────────────────────────
    lines += ["---", "", "## Recent Signals (last 20)", ""]
    if not signals:
        lines.append("_No signals detected yet._")
    else:
        lines.append(
            "| Time | Asset | Dir | Win(s) | Mag "
            "| OKX+60s | OKX+300s "
            "| BNB+60s | BNB+300s "
            "| BYB+60s | BYB+300s |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for s in signals[-20:]:
            ts_str = datetime.fromtimestamp(s.ts, tz=timezone.utc).strftime("%H:%M:%S")

            def _ret(d: dict, h: int) -> str:
                v = d.get(h)
                return _fmt_pct(v) if v is not None else "⏳"

            lines.append(
                f"| {ts_str} | {s.asset} | {s.direction} | {s.window_s:.0f}s "
                f"| {s.magnitude:.3f} "
                f"| {_ret(s.forward_returns, 60)} "
                f"| {_ret(s.forward_returns, 300)} "
                f"| {_ret(s.binance_returns, 60)} "
                f"| {_ret(s.binance_returns, 300)} "
                f"| {_ret(s.bybit_returns, 60)} "
                f"| {_ret(s.bybit_returns, 300)} |"
            )
    lines.append("")

    lines += ["---", "", "## Recommendation", ""]
    total = report["total_signals"]
    if total < 10:
        lines.append(
            "- Insufficient data (< 10 signals). Run for at least 2–4 hours "
            "to collect meaningful samples."
        )
    elif report["has_positive_expectation"]:
        lines.append("- ✅ Positive expectation detected across at least one horizon.")
        lines.append(
            "- Consider paper-trading with tight risk controls after "
            "2+ independent confirming sessions."
        )
        dominant_ex = (
            max(set(leaders), key=leaders.count)
            if (cmp := report.get("exchange_comparison", {})) and
               (leaders := [cmp[h]["leader"] for h in FORWARD_HORIZONS if h in cmp])
            else None
        )
        if dominant_ex:
            lines.append(
                f"- Recommended venue for paper trade: **{dominant_ex.upper()}** "
                f"(stronger lead response)."
            )
    else:
        lines.append("- No consistent positive expectation detected.")
        lines.append(
            "- Suggestions: tighten YES range (MIN_PRICE=0.45 / MAX_PRICE=0.55), "
            "increase POLY_JUMP_10S=0.05, or raise MIN_LIQUIDITY=5000."
        )
    lines += ["", "*STATS_ONLY / REAL_ORDER_DISABLED — no real orders placed.*"]

    # ── TTL-Segmented Analysis ───────────────────────────────────────────────
    segmented = _build_segmented_stats(signals)
    if segmented and any(v["n_signals"] > 0 for v in segmented.values()):
        lines += ["---", "", "## TTL-Segmented Analysis (OKX +60s aligned)", ""]
        lines.append(
            "| TTL Bucket | n_signals | win_rate_60s | mean_60s | median_60s |"
        )
        lines.append("|---|---|---|---|---|")
        for label in ["0-30min", "30min-2h", "2h-4h", "4h-8h"]:
            b = segmented.get(label, {})
            n = b.get("n_signals", 0)
            st = b.get("aligned_60s", {})
            if n == 0:
                lines.append(f"| {label} | 0 | — | — | — |")
                continue
            lines.append(
                f"| {label} | {n} "
                f"| {_fmt_pct(st.get('win_rate'))} "
                f"| {_fmt_pct(st.get('mean'))} "
                f"| {_fmt_pct(st.get('median'))} |"
            )
        lines.append("")
        # Highlight the best bucket
        best_bucket = max(
            segmented.items(),
            key=lambda kv: (kv[1].get("aligned_60s", {}).get("win_rate") or 0.0),
            default=None,
        )
        if best_bucket:
            label, bdata = best_bucket
            lines.append(
                f"- **Best TTL bucket:** `{label}` "
                f"(n={bdata['n_signals']}, "
                f"win={_fmt_pct(bdata['aligned_60s'].get('win_rate'))}, "
                f"mean={_fmt_pct(bdata['aligned_60s'].get('mean'))})"
            )
        lines.append("")

    if long_run_stats:
        lines += ["", "---", "", "## Long Run Summary", ""]
        runtime_h = long_run_stats.get("runtime_hours", 0.0)
        lines.append(f"- **Runtime:** {runtime_h:.2f}h")
        lines.append(f"- **Total signals:** {report['total_signals']}")
        hourly = long_run_stats.get("hourly_signals", [])
        if hourly:
            lines.append(f"- **Per-hour signals (completed hrs):** {hourly}")
        else:
            lines.append("- **Per-hour signals:** _no completed hours yet_")
        rc = long_run_stats.get("reconnect_counts", {})
        lines.append(
            f"- **Reconnects:** OKX={rc.get('okx', 0)} "
            f"BNB={rc.get('binance', 0)} "
            f"BYB={rc.get('bybit', 0)}"
        )
        lines.append(f"- **Discovery refreshes:** {long_run_stats.get('discovery_refreshes', 0)}")
        lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Async I/O tasks
# ─────────────────────────────────────────────────────────────────────────────

def _make_ssl_ctx() -> ssl.SSLContext:
    if os.environ.get("DISABLE_SSL_VERIFY", "").strip() == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi  # type: ignore[import]
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL_CTX = _make_ssl_ctx()


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


async def poly_discovery_task() -> None:
    """Discover active BTC/ETH/SOL Up-or-Down markets using startDate sort."""
    conn = aiohttp.TCPConnector(ssl=_SSL_CTX)
    async with aiohttp.ClientSession(
        headers={"User-Agent": "poly-lead-stats/1.0"},
        connector=conn,
    ) as session:
        while not state.shutdown.is_set():
            try:
                await _discover_markets(session)
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


async def _discover_markets(session: aiohttp.ClientSession) -> None:
    url = f"{GAMMA_URL}/markets"
    timeout = aiohttp.ClientTimeout(total=20)
    new_markets: dict[str, tuple] = {}
    import re as _re

    now_utc = datetime.now(timezone.utc)

    for offset in range(0, 3000, 100):
        if state.shutdown.is_set():
            break
        params = {
            "limit": "100",
            "order": "startDate",
            "ascending": "false",
            "offset": str(offset),
        }
        async with session.get(url, params=params, timeout=timeout) as resp:
            resp.raise_for_status()
            items = await resp.json(content_type=None)
        if not isinstance(items, list) or not items:
            break

        for m in items:
            mid = str(m.get("id") or "")
            if not mid:
                continue
            q = (m.get("question") or m.get("title") or "").lower()
            if "up or down" not in q and "higher or lower" not in q:
                continue
            # must have time window suffix
            if not _re.search(r"\d{1,2}:\d{2}(am|pm)-\d{1,2}:\d{2}(am|pm)", q):
                continue

            # ── TTL filter: skip markets that expire too soon or too far ──
            ed = m.get("endDate")
            if not ed:
                continue
            try:
                end_dt = datetime.fromisoformat(ed.replace("Z", "+00:00"))
                ttl_s = (end_dt - now_utc).total_seconds()
                if not (MIN_TTL_S <= ttl_s <= MAX_TTL_S):
                    continue
            except Exception:
                continue

            liq = float(m.get("liquidity") or 0)
            if liq < MIN_LIQUIDITY:
                continue
            yes = _parse_yes_price(m)
            if yes is None or not (MIN_PRICE < yes < MAX_PRICE):
                continue
            for asset, kws in ASSET_KW.items():
                if any(kw in q for kw in kws):
                    new_markets[mid] = (
                        asset,
                        (m.get("question") or "")[:80],
                        liq,
                        end_dt.timestamp(),  # store end_ts as float
                    )
                    break

        if len(items) < 100:
            break

    # ── Full replacement (not update) to clean stale markets ──
    state.poly_markets = new_markets
    log.info(
        "[DISC] %d markets in TTL [%.0fmin - %.1fh]",
        len(new_markets),
        MIN_TTL_S / 60,
        MAX_TTL_S / 3600,
    )


async def poly_poll_task() -> None:
    """Poll YES prices for discovered markets every POLY_POLL_S seconds."""
    conn = aiohttp.TCPConnector(ssl=_SSL_CTX)
    async with aiohttp.ClientSession(
        headers={"User-Agent": "poly-lead-stats/1.0"},
        connector=conn,
    ) as session:
        # Wait for at least one discovery run
        for _ in range(30):
            if state.poly_markets:
                break
            await asyncio.sleep(2)

        while not state.shutdown.is_set():
            t0 = time.monotonic()
            try:
                await _poll_prices(session)
                state.poly_polls += 1
            except Exception as exc:
                state.errors += 1
                log.warning("[POLL] price fetch failed: %s", exc)
            elapsed = time.monotonic() - t0
            sleep = max(0.0, POLY_POLL_S - elapsed)
            try:
                await asyncio.wait_for(
                    asyncio.shield(state.shutdown.wait()), timeout=sleep
                )
            except asyncio.TimeoutError:
                pass


async def _poll_prices(session: aiohttp.ClientSession) -> None:
    mids = list(state.poly_markets.keys())
    if not mids:
        return
    url = f"{GAMMA_URL}/markets"
    timeout = aiohttp.ClientTimeout(total=15)
    BATCH = 50
    now_ts = time.time()

    for i in range(0, len(mids), BATCH):
        batch = mids[i: i + BATCH]
        params = [("id", mid) for mid in batch]
        async with session.get(url, params=params, timeout=timeout) as resp:
            resp.raise_for_status()
            items = await resp.json(content_type=None)
        if not isinstance(items, list):
            continue
        for m in items:
            mid = str(m.get("id") or "")
            yes = _parse_yes_price(m)
            if yes is None:
                continue
            # Record delta for dynamic threshold estimation
            hist = state.poly_history.get(mid)
            if hist and hist[-1].price:
                prev = hist[-1].price
                state.poly_yes_deltas[mid].append(abs(yes - prev))
            state.poly_history[mid].append(PricePoint(ts=now_ts, price=yes))


async def okx_ws_task() -> None:
    """Stream OKX ticker data for BTC/ETH/SOL into state.okx_history."""
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
                                _handle_okx_msg(msg.data)
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


def _handle_okx_msg(raw: str) -> None:
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
    inst_id = item.get("instId", arg.get("instId", ""))
    if not inst_id:
        return
    try:
        last = float(item.get("last", 0))
    except (TypeError, ValueError):
        return
    if last == 0:
        return
    ts_str = item.get("ts")
    ts = int(ts_str) / 1000.0 if ts_str else time.time()
    state.okx_history[inst_id].append(PricePoint(ts=ts, price=last))
    state.okx_ticks += 1


async def signal_detection_task() -> None:
    """Check poly history for price events; record OKX forward-return tasks."""
    while not state.shutdown.is_set():
        now_ts = time.time()
        try:
            _detect_events(now_ts)
            _fill_pending_returns(now_ts)
        except Exception as exc:
            state.errors += 1
            log.error("[DETECT] error: %s", exc)
        try:
            await asyncio.wait_for(
                asyncio.shield(state.shutdown.wait()), timeout=1.0
            )
        except asyncio.TimeoutError:
            pass


def _detect_events(now_ts: float) -> None:
    for mid, market_tuple in list(state.poly_markets.items()):
        # Unpack 4-tuple: (asset, title, liq, end_ts)
        if len(market_tuple) == 3:
            asset, title, liq = market_tuple
            end_ts = None
        else:
            asset, title, liq, end_ts = market_tuple

        # ── TTL check ────────────────────────────────────────────────────────
        if end_ts is not None:
            ttl_s = end_ts - now_ts
            if not (MIN_TTL_S <= ttl_s <= MAX_TTL_S):
                continue
        else:
            ttl_s = None

        hist = list(state.poly_history.get(mid, []))
        if len(hist) < 8:
            continue

        # ── Layer 2: activity filter (recent 60s) ─────────────────────────────
        recent_points = [p for p in hist if now_ts - p.ts <= 60]
        if len(recent_points) < MIN_YES_ACTIVITY:
            continue
        recent_prices = [p.price for p in recent_points]
        yes_std = _std(recent_prices)
        if yes_std < MIN_YES_DELTA:
            continue

        # ── Layer 3: dynamic threshold from YES deltas ────────────────────────
        deltas = list(state.poly_yes_deltas.get(mid, []))  # deque of abs deltas
        if len(deltas) >= 30:
            p80 = _percentile(sorted(deltas), 80)
            dynamic_thr = max(POLY_JUMP_10S, p80 * DYNAMIC_THR_MULT)
        else:
            dynamic_thr = POLY_JUMP_10S

        # ── OKX volatility filter (avoid chasing already-moving OKX) ──────────
        okx_sym = f"{asset}-USDT"
        okx_hist = list(state.okx_history.get(okx_sym, []))
        okx_price = okx_hist[-1].price if okx_hist else None
        if len(okx_hist) >= 12:
            recent_okx = [p for p in okx_hist if now_ts - p.ts <= 60]
            if len(recent_okx) >= 3:
                okx_ret = abs(recent_okx[-1].price - recent_okx[0].price) / recent_okx[0].price
                if okx_ret > OKX_VOLATILITY_CAP:
                    continue

        # ── Windows with scaled dynamic thresholds ────────────────────────────
        dynamic_windows: list[tuple[float, float]] = [
            (10.0, dynamic_thr),
            (30.0, dynamic_thr * 1.6),
            (60.0, dynamic_thr * 2.4),
        ]

        for window_s, base_thr in dynamic_windows:
            event = detect_price_event(hist, window_s, base_thr, now_ts)
            if event is None:
                continue
            direction = event["direction"]

            # Cooldown + cross-market dedup
            if _check_cooldown(mid, asset, direction, window_s, now_ts):
                continue

            sig = LeadSignal(
                ts=now_ts,
                asset=asset,
                market_id=mid,
                market_title=title,
                direction=direction,
                window_s=window_s,
                threshold=base_thr,
                magnitude=event["magnitude"],
                yes_before=event["yes_before"],
                yes_after=event["yes_after"],
                okx_symbol=okx_sym,
                okx_price_at_trigger=okx_price,
                ttl_at_signal=round(ttl_s, 1) if ttl_s is not None else None,
                dynamic_threshold=round(dynamic_thr, 5),
                yes_std_60s=round(yes_std, 5),
            )
            state.signals.append(sig)
            log.info(
                "[SIGNAL] %s %s YES %s %.3f→%.3f w=%.0fs mag=%.3f "
                "thr=%.4f(dyn) ttl=%.0fm std=%.4f OKX=%s",
                asset, direction, "↑" if direction == "jump" else "↓",
                event["yes_before"], event["yes_after"],
                window_s, event["magnitude"],
                base_thr, ttl_s / 60 if ttl_s else -1,
                yes_std,
                f"{okx_price:.4f}" if okx_price else "N/A",
            )
            # Schedule forward-return fills
            for h in FORWARD_HORIZONS:
                state.pending_returns.append((sig, h, now_ts + h))
            # Only fire the SMALLEST window that triggered (avoid triple-counting)
            break


def _fill_pending_returns(now_ts: float) -> None:
    still_pending = []
    for sig, horizon_s, fill_at in state.pending_returns:
        if now_ts < fill_at:
            still_pending.append((sig, horizon_s, fill_at))
            continue
        # OKX return
        okx_hist = list(state.okx_history.get(sig.okx_symbol, []))
        ret_okx = compute_forward_return(okx_hist, sig.ts, horizon_s)
        sig.forward_returns[int(horizon_s)] = ret_okx
        # Binance return
        bnb_hist = list(state.binance_history.get(sig.asset, []))
        ret_bnb = compute_forward_return(bnb_hist, sig.ts, horizon_s)
        sig.binance_returns[int(horizon_s)] = ret_bnb
        # Bybit return
        byb_hist = list(state.bybit_history.get(sig.asset, []))
        ret_byb = compute_forward_return(byb_hist, sig.ts, horizon_s)
        sig.bybit_returns[int(horizon_s)] = ret_byb
        log.info(
            "[RETURN] %s +%ds OKX=%s BNB=%s BYB=%s (%s)",
            sig.asset, int(horizon_s),
            f"{ret_okx:+.4%}" if ret_okx is not None else "N/A",
            f"{ret_bnb:+.4%}" if ret_bnb is not None else "N/A",
            f"{ret_byb:+.4%}" if ret_byb is not None else "N/A",
            sig.direction,
        )
    state.pending_returns = still_pending


async def report_task(report_path: Path) -> None:
    """Write markdown report every 60 seconds."""
    while not state.shutdown.is_set():
        try:
            await asyncio.wait_for(
                asyncio.shield(state.shutdown.wait()), timeout=60.0
            )
        except asyncio.TimeoutError:
            pass
        _write_report(report_path)


async def heartbeat_task(duration_s: float) -> None:
    """Log progress summary every 60 seconds; bucket signals by completed hour."""
    elapsed = 0.0
    current_hour = 0
    signals_at_hour_start = 0
    while not state.shutdown.is_set() and elapsed < duration_s:
        try:
            await asyncio.wait_for(
                asyncio.shield(state.shutdown.wait()), timeout=60.0
            )
        except asyncio.TimeoutError:
            pass
        elapsed += 60.0
        new_hour = int(elapsed // 3600)
        if new_hour > current_hour:
            hour_signals = len(state.signals) - signals_at_hour_start
            state.hourly_signals.append(hour_signals)
            signals_at_hour_start = len(state.signals)
            current_hour = new_hour
        completed = sum(
            1 for s in state.signals
            if all(s.forward_returns.get(h) is not None for h in FORWARD_HORIZONS)
        )
        log.info(
            "━━ %.0f/%.0fs | OKX: %d BNB: %d BYB: %d ticks | "
            "Poly: %d markets %d polls | Signals: %d (%d complete) | Errors: %d ━━",
            elapsed, duration_s,
            state.okx_ticks, state.binance_ticks, state.bybit_ticks,
            len(state.poly_markets), state.poly_polls,
            len(state.signals), completed, state.errors,
        )


async def long_run_heartbeat_task(duration_s: float) -> None:
    """Log detailed 10-minute heartbeat for long runs."""
    elapsed = 0.0
    while not state.shutdown.is_set() and elapsed < duration_s:
        try:
            await asyncio.wait_for(
                asyncio.shield(state.shutdown.wait()), timeout=600.0
            )
        except asyncio.TimeoutError:
            pass
        elapsed += 600.0
        rc = state.reconnect_counts
        log.info(
            "━━━ 10-MIN | %.1fh/%.1fh | Signals: %d | "
            "DiscRefresh: %d | Reconnects OKX=%d BNB=%d BYB=%d ━━━",
            elapsed / 3600, duration_s / 3600,
            len(state.signals), state.discovery_refreshes,
            rc["okx"], rc["binance"], rc["bybit"],
        )
        if state.hourly_signals:
            log.info("Hourly signals (completed hrs): %s", state.hourly_signals)


def _write_report(report_path: Path) -> None:
    elapsed = time.monotonic() - state.start_ts
    report = build_stats_report(state.signals, elapsed)
    long_run_stats = {
        "runtime_hours": elapsed / 3600,
        "hourly_signals": list(state.hourly_signals),
        "reconnect_counts": dict(state.reconnect_counts),
        "discovery_refreshes": state.discovery_refreshes,
    }
    md = format_markdown_report(report, state.signals, long_run_stats=long_run_stats)
    report_path.write_text(md, encoding="utf-8")
    log.info("[REPORT] written → %s (%d signals)", report_path, len(state.signals))


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def _run(duration_s: float, report_path: Path) -> None:
    log.info(
        "━━ POLY_LEAD_STATS START | STATS_ONLY=%s REAL_ORDER=%s | %.0fs ━━",
        STATS_ONLY, REAL_ORDER, duration_s,
    )
    log.info(
        "Thresholds: JUMP_10S=%.2f JUMP_30S=%.2f JUMP_60S=%.2f "
        "MIN_LIQ=%.0f YES=[%.2f,%.2f]",
        POLY_JUMP_10S, POLY_JUMP_30S, POLY_JUMP_60S,
        MIN_LIQUIDITY, MIN_PRICE, MAX_PRICE,
    )

    def _on_bnb_tick(asset: str, pt: object) -> None:
        state.binance_ticks += 1

    def _on_byb_tick(asset: str, pt: object) -> None:
        state.bybit_ticks += 1

    def _on_bnb_reconnect() -> None:
        state.reconnect_counts["binance"] += 1

    def _on_byb_reconnect() -> None:
        state.reconnect_counts["bybit"] += 1

    tasks = [
        asyncio.create_task(poly_discovery_task(), name="disc"),
        asyncio.create_task(poly_poll_task(),      name="poll"),
        asyncio.create_task(okx_ws_task(),         name="okx"),
        asyncio.create_task(
            binance_ws_task(
                state.binance_history, state.shutdown,
                on_tick=_on_bnb_tick, on_reconnect=_on_bnb_reconnect,
            ),
            name="binance",
        ),
        asyncio.create_task(
            bybit_ws_task(
                state.bybit_history, state.shutdown,
                on_tick=_on_byb_tick, on_reconnect=_on_byb_reconnect,
            ),
            name="bybit",
        ),
        asyncio.create_task(signal_detection_task(), name="detect"),
        asyncio.create_task(report_task(report_path), name="report"),
        asyncio.create_task(heartbeat_task(duration_s), name="heartbeat"),
        asyncio.create_task(long_run_heartbeat_task(duration_s), name="long_hb"),
    ]

    async def _stopper():
        await asyncio.sleep(duration_s)
        log.info("Time limit reached — stopping.")
        state.shutdown.set()

    tasks.append(asyncio.create_task(_stopper(), name="stopper"))

    try:
        await state.shutdown.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    _write_report(report_path)
    log.info("━━ POLY_LEAD_STATS END | %d signals collected ━━", len(state.signals))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Polymarket → OKX lead signal stats (STATS_ONLY, no real orders)"
    )
    parser.add_argument("--duration", type=float, default=3600,
                        help="Run duration in seconds (default: 3600)")
    parser.add_argument("--log",    default="poly_lead_stats.log")
    parser.add_argument("--report", default=str(REPORT_PATH))
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(args.log, mode="a", encoding="utf-8"),
        ],
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

    def _on_signal(sig: int, _frame: object) -> None:
        log.info("Signal %s — stopping.", sig)
        state.shutdown.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        asyncio.run(_run(args.duration, Path(args.report)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
