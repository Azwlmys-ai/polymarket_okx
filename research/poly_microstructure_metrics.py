"""
poly_microstructure_metrics.py — pure orderbook microstructure functions.

No I/O, no network, no state. All inputs are plain Python scalars or lists.
"""
from __future__ import annotations

from statistics import mean, stdev
from typing import Sequence


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Level = tuple[float, float]   # (price, size)


# ---------------------------------------------------------------------------
# Spread
# ---------------------------------------------------------------------------

def spread(bid: float | None, ask: float | None) -> float | None:
    """Absolute spread = ask − bid. None if either side missing."""
    if bid is None or ask is None:
        return None
    return round(ask - bid, 8)


def spread_bps(bid: float | None, ask: float | None) -> float | None:
    """Spread in basis points = (ask − bid) / mid × 10 000. None if invalid."""
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return round((ask - bid) / mid * 10_000, 4)


# ---------------------------------------------------------------------------
# Depth
# ---------------------------------------------------------------------------

def depth_usd(levels: Sequence[Level], n: int | None = None) -> float:
    """
    Total USD value (price × size) across the top-n levels.

    levels: iterable of (price, size) tuples, pre-sorted by the caller.
    n=None uses all levels.
    """
    subset = list(levels)[:n] if n is not None else list(levels)
    return round(sum(p * s for p, s in subset if p > 0 and s > 0), 6)


def depth_near_mid(
    bids: Sequence[Level],
    asks: Sequence[Level],
    mid: float | None,
    pct: float = 0.05,
) -> float:
    """
    Total USD depth on both sides within ±pct of mid.

    Returns 0.0 when mid is None, zero, or negative.
    """
    if not mid or mid <= 0:
        return 0.0
    lo, hi = mid * (1.0 - pct), mid * (1.0 + pct)
    total = 0.0
    for p, s in bids:
        if lo <= p <= hi and p > 0 and s > 0:
            total += p * s
    for p, s in asks:
        if lo <= p <= hi and p > 0 and s > 0:
            total += p * s
    return round(total, 6)


# ---------------------------------------------------------------------------
# Order imbalance
# ---------------------------------------------------------------------------

def order_imbalance(
    bids: Sequence[Level],
    asks: Sequence[Level],
    n: int | None = None,
) -> float | None:
    """
    Normalised order-book imbalance = (bid_vol − ask_vol) / (bid_vol + ask_vol).

    Result in [−1, +1]:
      +1  all volume on the bid side (buying pressure)
      −1  all volume on the ask side (selling pressure)
      0   balanced book

    Returns None when total volume is zero.
    """
    bid_vol = depth_usd(bids, n)
    ask_vol = depth_usd(asks, n)
    total = bid_vol + ask_vol
    if total == 0:
        return None
    return round((bid_vol - ask_vol) / total, 8)


# ---------------------------------------------------------------------------
# Price velocity
# ---------------------------------------------------------------------------

def price_velocity(
    prices: Sequence[float],
    times: Sequence[float],
) -> float | None:
    """
    Estimate price velocity (change per second) via linear regression slope.

    prices: sequence of prices in chronological order.
    times:  matching sequence of unix timestamps (seconds).

    Returns None when fewer than 2 points or zero time span.
    """
    pts = [(t, p) for t, p in zip(times, prices) if t is not None and p is not None]
    if len(pts) < 2:
        return None
    ts = [t for t, _ in pts]
    ps = [p for _, p in pts]
    dt = ts[-1] - ts[0]
    if dt <= 0:
        return None
    # Ordinary least-squares slope
    n = len(pts)
    t_mean = mean(ts)
    p_mean = mean(ps)
    num = sum((t - t_mean) * (p - p_mean) for t, p in zip(ts, ps))
    den = sum((t - t_mean) ** 2 for t in ts)
    if den == 0:
        return None
    return round(num / den, 8)


# ---------------------------------------------------------------------------
# Paired edge (market-maker round-trip)
# ---------------------------------------------------------------------------

def paired_edge(
    yes_ask: float | None,
    no_ask: float | None,
    rebate_rate: float,
    taker_fee: float,
) -> float | None:
    """
    Per-unit net edge on a completed YES + NO maker round-trip.

    When a taker fills our YES ask AND our NO ask:
      revenue = yes_ask + no_ask
      payout  = 1.0 (always)
      rebates = rebate_rate × taker_fee × ((1 − yes_ask) + (1 − no_ask))

    net_edge = (yes_ask + no_ask − 1.0) + rebates

    Positive → profitable round-trip after rebates.
    None if either ask is missing.
    """
    if yes_ask is None or no_ask is None:
        return None
    hc = yes_ask + no_ask
    rebates = rebate_rate * taker_fee * (2.0 - hc)
    return round((hc - 1.0) + rebates, 8)


def hedge_cost(yes_ask: float | None, no_ask: float | None) -> float | None:
    """YES_ask + NO_ask. None if either is missing."""
    if yes_ask is None or no_ask is None:
        return None
    return round(yes_ask + no_ask, 8)


def hedge_cost_cv(costs: Sequence[float]) -> float | None:
    """Coefficient of variation of hedge_cost across rounds. None if < 2 points."""
    cs = [c for c in costs if c is not None]
    if len(cs) < 2:
        return None
    m = mean(cs)
    if m == 0:
        return None
    return round(stdev(cs) / m, 8)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def verdict(
    net_edge: float | None,
    avg_depth: float,
    sp_bps: float | None,
    hc_cv: float | None,
    rewards_avail: float,
    book_avail: float,
) -> str:
    """
    GO / WATCH / NO-GO for one market given aggregate metrics.

    GO requires ALL:
      net_edge > 0
      book_avail >= 0.80
      rewards_avail >= 0.80
      avg_depth > 0
      sp_bps is not None and sp_bps <= 300
      hc_cv is not None and hc_cv < 0.01

    NO-GO if: net_edge <= 0 OR book_avail < 0.50 OR rewards_avail < 0.50
    WATCH: everything else.
    """
    if net_edge is None or net_edge <= 0:
        return "NO-GO"
    if book_avail < 0.50 or rewards_avail < 0.50:
        return "NO-GO"
    if (
        book_avail >= 0.80
        and rewards_avail >= 0.80
        and avg_depth > 0
        and sp_bps is not None and sp_bps <= 300
        and hc_cv is not None and hc_cv < 0.01
    ):
        return "GO"
    return "WATCH"
