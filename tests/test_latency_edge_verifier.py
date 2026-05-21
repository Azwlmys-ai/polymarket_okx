"""
tests/test_latency_edge_verifier.py — pure-function tests for latency_edge_verifier.

All offline, no network. Covers:
  compute_fee, compute_net_edge, percentile,
  follow_rate, classify_edge, derive_event_stats.
"""

from __future__ import annotations

import sys, os, math, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from research.latency_edge_verifier import (
    LagEvent,
    classify_edge,
    compute_fee,
    compute_net_edge,
    derive_event_stats,
    follow_rate,
    percentile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(
    cex_direction: str = "up",
    poly_yes_at_trigger: float = 0.50,
    market_id: str = "mkt1",
    lag_ms: int | None = None,
    is_false_signal: bool = False,
    net_edge: float | None = None,
    cex_move_ts_ms: int | None = None,
) -> LagEvent:
    ts = cex_move_ts_ms or int(time.time() * 1000) - 60_000
    ev = LagEvent(
        event_id=1, ts_utc="2026-01-01T00:00:00Z",
        asset="BTC-USDT", market_id=market_id, market_title="BTC test",
        ttl_s=300.0,
        cex_sources_agreed=["okx", "binance"],
        cex_move_ts_ms=ts,
        cex_direction=cex_direction,
        cex_move_pct=0.003,
        cex_price_before=78000.0, cex_price_after=78234.0,
        poly_yes_at_trigger=poly_yes_at_trigger,
    )
    ev.lag_ms = lag_ms
    ev.is_false_signal = is_false_signal
    ev.net_edge = net_edge
    ev.followed_1s = lag_ms is not None and lag_ms <= 1_000
    ev.followed_3s = lag_ms is not None and lag_ms <= 3_000
    ev.followed_5s = lag_ms is not None and lag_ms <= 5_000
    return ev


# ---------------------------------------------------------------------------
# compute_fee
# ---------------------------------------------------------------------------

class TestComputeFee:
    def test_standard(self):
        # 7% × (1 - 0.50) = 0.035
        assert compute_fee(0.50) == pytest.approx(0.035)

    def test_high_price(self):
        # fee is smaller when price is high
        assert compute_fee(0.90) == pytest.approx(0.07 * 0.10)

    def test_low_price(self):
        assert compute_fee(0.10) == pytest.approx(0.07 * 0.90)

    def test_zero_fee_rate(self):
        assert compute_fee(0.50, fee_rate=0.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# compute_net_edge
# ---------------------------------------------------------------------------

class TestComputeNetEdge:
    def test_zero_move_is_loss(self):
        # No price movement: gross=0, costs > 0
        ne = compute_net_edge(0.50, 0.50)
        assert ne < 0

    def test_large_move_is_profitable(self):
        # YES goes from 0.50 to 0.60 → gross=0.10, fee≈0.035, slip≈0.001
        ne = compute_net_edge(0.50, 0.60)
        assert ne > 0

    def test_small_move_not_profitable(self):
        # YES goes 0.50 → 0.52 → gross=0.02 < fee(0.035)
        ne = compute_net_edge(0.50, 0.52)
        assert ne < 0

    def test_breakeven_near_fee_threshold(self):
        # At 0.50: fee=0.035, slip=0.001 → costs=0.036
        # need gross > 0.036 → exit price > 0.536
        ne_below = compute_net_edge(0.50, 0.535)   # gross=0.035, net=-0.001
        ne_above = compute_net_edge(0.50, 0.540)   # gross=0.040, net=+0.004
        assert ne_below < 0
        assert ne_above > 0

    def test_adverse_move_negative(self):
        ne = compute_net_edge(0.50, 0.40)
        assert ne < -0.09

    def test_costs_scale_with_slippage(self):
        ne_low  = compute_net_edge(0.50, 0.55, slippage=0.001)
        ne_high = compute_net_edge(0.50, 0.55, slippage=0.010)
        assert ne_low > ne_high


# ---------------------------------------------------------------------------
# percentile
# ---------------------------------------------------------------------------

class TestPercentile:
    def test_median_odd(self):
        assert percentile([1, 2, 3, 4, 5], 50) == pytest.approx(3.0)

    def test_p25(self):
        assert percentile([1, 2, 3, 4], 25) == pytest.approx(1.75)

    def test_p100(self):
        assert percentile([10, 20, 30], 100) == pytest.approx(30.0)

    def test_single_element(self):
        assert percentile([42.0], 50) == pytest.approx(42.0)

    def test_empty_returns_nan(self):
        assert math.isnan(percentile([], 50))

    def test_sorted_invariant(self):
        vals = [5, 1, 3, 2, 4]
        assert percentile(vals, 0)  == pytest.approx(1.0)
        assert percentile(vals, 100) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# follow_rate
# ---------------------------------------------------------------------------

class TestFollowRate:
    def test_all_follow_1s(self):
        events = [_event(lag_ms=500), _event(lag_ms=800), _event(lag_ms=1000)]
        assert follow_rate(events, 1_000) == pytest.approx(1.0)

    def test_none_follow_1s(self):
        events = [_event(lag_ms=2000), _event(lag_ms=5000)]
        assert follow_rate(events, 1_000) == pytest.approx(0.0)

    def test_partial(self):
        events = [_event(lag_ms=500), _event(lag_ms=4000), _event(is_false_signal=True)]
        assert follow_rate(events, 3_000) == pytest.approx(1 / 3)

    def test_false_signals_excluded(self):
        events = [_event(is_false_signal=True)] * 5
        assert follow_rate(events, 30_000) == pytest.approx(0.0)

    def test_empty(self):
        assert follow_rate([], 1_000) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# classify_edge
# ---------------------------------------------------------------------------

class TestClassifyEdge:
    def test_insufficient_data(self):
        result = classify_edge(0.05, 0.8, 5.0, n_events=3)
        assert "insufficient" in result.lower()

    def test_no_edge_low_follow_rate(self):
        result = classify_edge(0.05, 0.10, 5.0, n_events=20)
        assert result == "NO EDGE"

    def test_no_edge_negative_net_edge(self):
        result = classify_edge(-0.01, 0.80, 5.0, n_events=20)
        assert result == "NO EDGE"

    def test_weak_edge(self):
        result = classify_edge(0.002, 0.50, 5.0, n_events=20)
        assert result == "WEAK EDGE"

    def test_paper_edge_only_low_opp(self):
        # good edge but few opportunities per hour
        result = classify_edge(0.010, 0.70, 0.5, n_events=20)
        assert result == "PAPER EDGE ONLY"

    def test_paper_edge_only_marginal_edge(self):
        result = classify_edge(0.010, 0.70, 5.0, n_events=20)
        assert result == "PAPER EDGE ONLY"

    def test_execution_worthy(self):
        result = classify_edge(0.040, 0.75, 3.0, n_events=30)
        assert result == "EXECUTION-WORTHY EDGE"

    def test_zero_edge_is_no_edge(self):
        result = classify_edge(0.0, 0.90, 10.0, n_events=50)
        assert result == "NO EDGE"


# ---------------------------------------------------------------------------
# derive_event_stats
# ---------------------------------------------------------------------------

class TestDeriveEventStats:
    def _base_event(self, direction: str = "up", trigger: float = 0.50) -> LagEvent:
        ts = int(time.time() * 1000) - 400_000
        return LagEvent(
            event_id=1, ts_utc="2026-01-01T00:00:00Z",
            asset="BTC-USDT", market_id="m1", market_title="test",
            ttl_s=300.0, cex_sources_agreed=["okx"],
            cex_move_ts_ms=ts, cex_direction=direction, cex_move_pct=0.003,
            cex_price_before=78000.0, cex_price_after=78234.0,
            poly_yes_at_trigger=trigger,
        )

    def test_reprice_detected(self):
        ev = self._base_event(trigger=0.50)
        # Poly jumps to 0.54 at 3s (>0.3% move)
        ev.poly_samples = {3_000: 0.54, 5_000: 0.56, 60_000: 0.57}
        derive_event_stats(ev)
        assert ev.lag_ms == 3_000
        assert ev.followed_3s is True
        assert ev.is_false_signal is False

    def test_no_reprice_is_false_signal(self):
        ev = self._base_event(trigger=0.50)
        # Move < POLY_FOLLOW_THRESHOLD (0.003 = 0.3%) → no reprice detected
        # 0.501 is 0.2% from 0.50 and 0.5014 is 0.28% — both below 0.3%
        ev.poly_samples = {3_000: 0.501, 60_000: 0.5014}
        derive_event_stats(ev)
        assert ev.is_false_signal is True
        assert ev.lag_ms is None

    def test_direction_match_up(self):
        ev = self._base_event(direction="up", trigger=0.50)
        ev.poly_samples = {3_000: 0.55, 60_000: 0.57}
        derive_event_stats(ev)
        assert ev.direction_match is True

    def test_direction_mismatch(self):
        ev = self._base_event(direction="up", trigger=0.50)
        ev.poly_samples = {3_000: 0.44, 60_000: 0.42}   # moved DOWN despite UP signal
        derive_event_stats(ev)
        assert ev.direction_match is False

    def test_gross_edge_computed(self):
        ev = self._base_event(direction="up", trigger=0.50)
        ev.poly_samples = {60_000: 0.58, 300_000: 0.62}
        derive_event_stats(ev)
        assert ev.gross_edge is not None
        assert ev.gross_edge == pytest.approx(0.62 - 0.50, abs=0.001)

    def test_net_edge_positive_large_move(self):
        ev = self._base_event(direction="up", trigger=0.50)
        ev.poly_samples = {60_000: 0.60}   # 10 cent gross > 3.5 cent fee
        derive_event_stats(ev)
        assert ev.net_edge is not None
        assert ev.net_edge > 0

    def test_net_edge_none_when_no_gain(self):
        ev = self._base_event(direction="up", trigger=0.50)
        ev.poly_samples = {60_000: 0.50}   # no move
        derive_event_stats(ev)
        assert ev.net_edge is None

    def test_follow_flags(self):
        ev = self._base_event(trigger=0.50)
        ev.poly_samples = {2_000: 0.52, 60_000: 0.55}
        derive_event_stats(ev)
        assert ev.followed_1s is False     # lag=2000ms > 1000ms
        assert ev.followed_3s is True
        assert ev.followed_5s is True
