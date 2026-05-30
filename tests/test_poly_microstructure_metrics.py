"""tests/test_poly_microstructure_metrics.py — pure-function unit tests."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from research.poly_microstructure_metrics import (
    depth_near_mid, depth_usd, hedge_cost, hedge_cost_cv,
    order_imbalance, paired_edge, price_velocity,
    spread, spread_bps, verdict,
)


# ---------------------------------------------------------------------------
# spread / spread_bps
# ---------------------------------------------------------------------------

class TestSpread:
    def test_basic(self):
        assert spread(0.48, 0.52) == pytest.approx(0.04)

    def test_none_bid(self):
        assert spread(None, 0.52) is None

    def test_none_ask(self):
        assert spread(0.48, None) is None

    def test_zero(self):
        assert spread(0.50, 0.50) == pytest.approx(0.0)


class TestSpreadBps:
    def test_basic(self):
        # mid=0.50, spread=0.02 → 400 bps
        assert spread_bps(0.49, 0.51) == pytest.approx(400.0, rel=1e-4)

    def test_none_bid(self):
        assert spread_bps(None, 0.51) is None

    def test_none_ask(self):
        assert spread_bps(0.49, None) is None

    def test_zero_bid(self):
        assert spread_bps(0.0, 0.51) is None

    def test_narrow_spread(self):
        # mid=0.500, spread=0.002 → 0.002/0.500 × 10000 = 40 bps
        result = spread_bps(0.499, 0.501)
        assert result == pytest.approx(40.0, rel=1e-3)


# ---------------------------------------------------------------------------
# depth_usd
# ---------------------------------------------------------------------------

class TestDepthUsd:
    def test_single_level(self):
        assert depth_usd([(0.50, 100)]) == pytest.approx(50.0)

    def test_multi_level(self):
        assert depth_usd([(0.50, 100), (0.49, 200)]) == pytest.approx(50.0 + 98.0)

    def test_n_cap(self):
        levels = [(0.50, 100), (0.49, 200), (0.48, 300)]
        assert depth_usd(levels, n=2) == pytest.approx(50.0 + 98.0)

    def test_empty(self):
        assert depth_usd([]) == pytest.approx(0.0)

    def test_zero_price_excluded(self):
        assert depth_usd([(0.0, 100), (0.50, 100)]) == pytest.approx(50.0)

    def test_zero_size_excluded(self):
        assert depth_usd([(0.50, 0), (0.50, 100)]) == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# depth_near_mid
# ---------------------------------------------------------------------------

class TestDepthNearMid:
    def test_all_in_range(self):
        bids = [(0.49, 100)]
        asks = [(0.51, 200)]
        result = depth_near_mid(bids, asks, 0.50, pct=0.05)
        assert result == pytest.approx(0.49 * 100 + 0.51 * 200, rel=1e-5)

    def test_out_of_range_excluded(self):
        bids = [(0.20, 1000)]   # far below mid
        asks = [(0.51, 100)]
        result = depth_near_mid(bids, asks, 0.50, pct=0.05)
        assert result == pytest.approx(0.51 * 100, rel=1e-5)

    def test_none_mid(self):
        assert depth_near_mid([(0.49, 100)], [(0.51, 100)], None) == 0.0

    def test_zero_mid(self):
        assert depth_near_mid([(0.49, 100)], [(0.51, 100)], 0.0) == 0.0

    def test_empty_books(self):
        assert depth_near_mid([], [], 0.50) == 0.0


# ---------------------------------------------------------------------------
# order_imbalance
# ---------------------------------------------------------------------------

class TestOrderImbalance:
    def test_bid_dominated(self):
        # all volume on bids → +1
        result = order_imbalance([(0.50, 100)], [(0.51, 0)], n=5)
        assert result == pytest.approx(1.0)

    def test_ask_dominated(self):
        result = order_imbalance([(0.50, 0)], [(0.51, 100)], n=5)
        assert result == pytest.approx(-1.0)

    def test_balanced(self):
        # Uses USD volume: bid=0.50×100=50, ask=0.51×100=51 → tiny negative tilt
        result = order_imbalance([(0.50, 100)], [(0.51, 100)])
        assert result == pytest.approx(-1 / 101, rel=1e-4)

    def test_balanced_equal_price(self):
        # Same price on both sides → truly balanced
        result = order_imbalance([(0.50, 100)], [(0.50, 100)])
        assert result == pytest.approx(0.0)

    def test_empty_returns_none(self):
        assert order_imbalance([], []) is None

    def test_range_minus1_to_plus1(self):
        result = order_imbalance([(0.50, 60)], [(0.51, 40)])
        assert -1.0 <= result <= 1.0

    def test_n_cap(self):
        bids = [(0.50, 100), (0.49, 200), (0.48, 300)]
        asks = [(0.51, 100)]
        # with n=1: only top bid level counted
        full = order_imbalance(bids, asks)
        top1 = order_imbalance(bids, asks, n=1)
        assert full != top1   # different because depth changes


# ---------------------------------------------------------------------------
# price_velocity
# ---------------------------------------------------------------------------

class TestPriceVelocity:
    def test_constant_price(self):
        assert price_velocity([100, 100, 100], [0, 1, 2]) == pytest.approx(0.0, abs=1e-9)

    def test_linear_up(self):
        # price goes +10 over 10 seconds → velocity = 1 $/s
        v = price_velocity([100, 105, 110], [0, 5, 10])
        assert v == pytest.approx(1.0, rel=1e-5)

    def test_linear_down(self):
        v = price_velocity([110, 105, 100], [0, 5, 10])
        assert v == pytest.approx(-1.0, rel=1e-5)

    def test_single_point(self):
        assert price_velocity([100], [0]) is None

    def test_empty(self):
        assert price_velocity([], []) is None

    def test_zero_time_span(self):
        assert price_velocity([100, 101], [5, 5]) is None

    def test_two_points(self):
        v = price_velocity([100, 102], [0, 2])
        assert v == pytest.approx(1.0, rel=1e-5)


# ---------------------------------------------------------------------------
# paired_edge / hedge_cost / hedge_cost_cv
# ---------------------------------------------------------------------------

class TestPairedEdge:
    def test_positive_with_rebate(self):
        # hc=1.001, rebate = 0.25*0.05*(2-1.001) = 0.01249
        # net = 0.001 + 0.01249 = 0.01349
        result = paired_edge(0.55, 0.451, 0.25, 0.05)
        assert result is not None and result > 0

    def test_none_yes_ask(self):
        assert paired_edge(None, 0.46, 0.25, 0.05) is None

    def test_none_no_ask(self):
        assert paired_edge(0.55, None, 0.25, 0.05) is None

    def test_zero_rebate_equals_spread_profit(self):
        # no rebate → net = yes_ask + no_ask - 1.0
        result = paired_edge(0.55, 0.46, 0.0, 0.05)
        assert result == pytest.approx(0.55 + 0.46 - 1.0, abs=1e-8)

    def test_breakeven(self):
        # hc=1.0, zero rebate → net=0
        assert paired_edge(0.50, 0.50, 0.0, 0.05) == pytest.approx(0.0, abs=1e-8)


class TestHedgeCost:
    def test_basic(self):
        assert hedge_cost(0.55, 0.46) == pytest.approx(1.01)

    def test_none_yes(self):
        assert hedge_cost(None, 0.46) is None

    def test_none_no(self):
        assert hedge_cost(0.55, None) is None


class TestHedgeCostCv:
    def test_stable(self):
        cv = hedge_cost_cv([1.001, 1.001, 1.001])
        assert cv == pytest.approx(0.0, abs=1e-9)

    def test_variable(self):
        cv = hedge_cost_cv([1.001, 1.010, 1.020])
        assert cv is not None and cv > 0

    def test_single(self):
        assert hedge_cost_cv([1.001]) is None

    def test_empty(self):
        assert hedge_cost_cv([]) is None


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------

def _go(**kw):
    base = dict(net_edge=0.01, avg_depth=500.0, sp_bps=150.0,
                hc_cv=0.005, rewards_avail=0.90, book_avail=0.90)
    base.update(kw)
    return verdict(**base)


class TestVerdict:
    def test_go(self):
        assert _go() == "GO"

    def test_nogo_negative_edge(self):
        assert _go(net_edge=-0.01) == "NO-GO"

    def test_nogo_zero_edge(self):
        assert _go(net_edge=0.0) == "NO-GO"

    def test_nogo_none_edge(self):
        assert _go(net_edge=None) == "NO-GO"

    def test_nogo_low_book_avail(self):
        assert _go(book_avail=0.49) == "NO-GO"

    def test_nogo_low_rewards_avail(self):
        assert _go(rewards_avail=0.49) == "NO-GO"

    def test_watch_depth_zero(self):
        assert _go(avg_depth=0.0) == "WATCH"

    def test_watch_spread_too_wide(self):
        assert _go(sp_bps=301.0) == "WATCH"

    def test_watch_cv_too_high(self):
        assert _go(hc_cv=0.01) == "WATCH"

    def test_watch_cv_none(self):
        assert _go(hc_cv=None) == "WATCH"

    def test_watch_low_book_avail(self):
        assert _go(book_avail=0.60) == "WATCH"

    def test_watch_low_rewards_avail(self):
        assert _go(rewards_avail=0.70) == "WATCH"

    def test_go_boundary_spread(self):
        assert _go(sp_bps=300.0) == "GO"

    def test_go_boundary_cv(self):
        assert _go(hc_cv=0.0099) == "GO"
