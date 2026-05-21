"""
tests/test_rewards_mm_timeseries.py — pure-function tests for the timeseries module.

All tests are offline (no network calls). They cover:
- compute_net_edge / compute_estimated_rebate
- compute_depth_near_mid
- round_stability
- hedge_cost_cv
- stability_score
- aggregate_samples / MarketStats
- verdict_for
- Edge cases: empty data, None values, single round
"""

from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from statistics import mean

from research.rewards_mm_timeseries import (
    Sample,
    MarketStats,
    aggregate_samples,
    compute_depth_near_mid,
    compute_estimated_rebate,
    compute_net_edge,
    hedge_cost_cv,
    round_stability,
    stability_score,
    verdict_for,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sample(
    slug: str = "test",
    round_n: int = 0,
    yes_bid: float | None = 0.52,
    yes_ask: float | None = 0.54,
    no_bid: float | None = 0.44,
    no_ask: float | None = 0.46,
    rebate_rate: float = 0.25,
    taker_fee_rate: float = 0.05,
    has_rewards: bool = True,
    neg_risk: bool = False,
) -> Sample:
    mid = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else None
    sp_bps = (yes_ask - yes_bid) / mid * 10_000 if mid and yes_bid and yes_ask else None
    hc = (yes_ask + no_ask) if (yes_ask is not None and no_ask is not None) else None
    locked = round(hc - 1.0, 7) if hc is not None else None
    est_reb = compute_estimated_rebate(hc, rebate_rate, taker_fee_rate)
    net = compute_net_edge(hc, rebate_rate, taker_fee_rate)
    return Sample(
        ts="2026-01-01T00:00:00Z",
        slug=slug,
        round_n=round_n,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        midpoint=mid,
        spread_bps=sp_bps,
        hedge_cost=hc,
        locked_profit=locked,
        estimated_rebate=est_reb,
        net_edge=net,
        depth_near_mid=0.0,
        has_yes_book=yes_bid is not None,
        has_no_book=no_bid is not None,
        rewards_min_size=50.0,
        rewards_max_spread=2.0,
        rebate_rate=rebate_rate,
        taker_fee_rate=taker_fee_rate,
        has_rewards=has_rewards,
        neg_risk=neg_risk,
    )


# ---------------------------------------------------------------------------
# compute_net_edge
# ---------------------------------------------------------------------------

class TestComputeNetEdge:
    def test_positive_with_rebate(self):
        # hedge_cost=1.001, rate=0.05, rebate=0.25
        # rebate_income = 0.25 * 0.05 * (2 - 1.001) = 0.0125 * 0.999 = 0.0124875
        # net = (1.001-1.0) + 0.0124875 = 0.0134875
        result = compute_net_edge(1.001, 0.25, 0.05)
        assert result is not None
        assert result > 0

    def test_breakeven_hedge(self):
        # hedge_cost=1.00 → spread_loss=0, all rebate is profit
        result = compute_net_edge(1.00, 0.25, 0.05)
        assert result is not None
        assert result > 0   # rebate always positive

    def test_zero_rebate(self):
        # With no rebate, net_edge = hedge_cost - 1.0
        result = compute_net_edge(1.001, 0.0, 0.05)
        assert result == pytest.approx(0.001, abs=1e-7)

    def test_expensive_hedge_still_positive_with_rebate(self):
        # hedge_cost=1.01, rate=0.05, rebate=0.25
        # rebate = 0.25 * 0.05 * (2 - 1.01) = 0.0125 * 0.99 = 0.012375
        # net = 0.01 + 0.012375 = negative? wait:
        # net = (1.01 - 1.0) + 0.012375 = 0.01 + 0.012375... wait:
        # spread_loss = hedge_cost - 1.0 = 0.01 (we LOSE 0.01 per round trip)
        # net = -(-0.01) + 0.012375 = wait I need to recheck
        # net_edge = (hedge_cost - 1.0) + rebate
        #          = (1.01 - 1.0) + 0.012375 = 0.01 + 0.012375 = 0.022375?
        # No: spread loss = YES_ask + NO_ask - 1.0 = 1.01 - 1.0 = 0.01
        # That's a LOSS of 0.01. But the formula is:
        # net = spread_loss + rebate_income = 0.01 + 0.012375 = 0.022375?? That can't be right.
        # Wait: spread_loss = hedge_cost - 1.0 = +0.01 means we RECEIVE 1.01 and pay 1.0 → profit 0.01
        # Yes! If hedge_cost = 1.01, it means YES_ask + NO_ask = 1.01
        # We SELL YES at YES_ask and NO at NO_ask → receive 1.01 total → pay 1.0 at settlement → profit 0.01
        # Plus rebates → net even more positive!
        result = compute_net_edge(1.01, 0.25, 0.05)
        assert result is not None
        assert result > 0

    def test_none_hedge_cost(self):
        assert compute_net_edge(None, 0.25, 0.05) is None

    def test_symmetry(self):
        # hedge_cost=1.0 → spread P&L = 0; rebate = rate*taker*(2-1.0) = 0.25*0.05*1.0
        result = compute_net_edge(1.0, 0.25, 0.05)
        expected = 0.25 * 0.05 * 1.0   # NOT *2 — formula uses (2 - hedge_cost) = 1.0
        assert result == pytest.approx(expected, rel=1e-5)

    def test_very_low_hedge_cost_is_loss(self):
        # hedge_cost=0.90 means MM receives 0.90 but owes 1.0 at settlement → net loss
        # spread_loss = 0.90 - 1.0 = -0.10, rebate ≈ 0.0125 * 1.10 = 0.01375
        # net ≈ -0.10 + 0.01375 = -0.086 (loss)
        result = compute_net_edge(0.90, 0.25, 0.05)
        assert result is not None
        assert result < 0   # net loss; rebate cannot cover the -0.10 spread loss


# ---------------------------------------------------------------------------
# compute_estimated_rebate
# ---------------------------------------------------------------------------

class TestComputeEstimatedRebate:
    def test_standard(self):
        # rebate = 0.25 * 0.05 * (2 - 1.001) = 0.0125 * 0.999 = 0.0124875
        r = compute_estimated_rebate(1.001, 0.25, 0.05)
        assert r == pytest.approx(0.0124875, rel=1e-4)

    def test_no_rebate(self):
        assert compute_estimated_rebate(1.001, 0.0, 0.05) == pytest.approx(0.0)

    def test_none(self):
        assert compute_estimated_rebate(None, 0.25, 0.05) is None

    def test_positive(self):
        r = compute_estimated_rebate(1.0, 0.25, 0.05)
        assert r is not None and r > 0


# ---------------------------------------------------------------------------
# compute_depth_near_mid
# ---------------------------------------------------------------------------

class TestDepthNearMid:
    def test_all_within_range(self):
        bids = [{"price": "0.50", "size": "100"}]
        asks = [{"price": "0.52", "size": "200"}]
        mid = 0.51
        result = compute_depth_near_mid(bids, asks, mid, pct=0.10)
        assert result == pytest.approx(0.50 * 100 + 0.52 * 200, rel=0.01)

    def test_outside_range_excluded(self):
        bids = [{"price": "0.30", "size": "1000"}]  # far from mid
        asks = [{"price": "0.52", "size": "200"}]
        mid = 0.51
        result = compute_depth_near_mid(bids, asks, mid, pct=0.05)
        # 0.30 is outside ±5% of 0.51 (range: 0.4845–0.5355)
        assert result == pytest.approx(0.52 * 200, rel=0.01)

    def test_empty_books(self):
        assert compute_depth_near_mid([], [], 0.50) == 0.0

    def test_none_midpoint(self):
        bids = [{"price": "0.50", "size": "100"}]
        assert compute_depth_near_mid(bids, [], None) == 0.0

    def test_malformed_entries_skipped(self):
        bids = [{"price": "bad", "size": "100"}, {"price": "0.50", "size": "100"}]
        asks = [{"price": "0.52", "size": "200"}]
        result = compute_depth_near_mid(bids, asks, 0.51, pct=0.10)
        # Only valid entries counted
        assert result > 0

    def test_zero_midpoint(self):
        bids = [{"price": "0.50", "size": "100"}]
        assert compute_depth_near_mid(bids, [], 0.0) == 0.0


# ---------------------------------------------------------------------------
# round_stability
# ---------------------------------------------------------------------------

class TestRoundStability:
    def test_first_round_is_stable(self):
        assert round_stability(0.54, 0.52, None, None) == pytest.approx(1.0)

    def test_identical_quotes(self):
        assert round_stability(0.54, 0.52, 0.54, 0.52) == pytest.approx(1.0)

    def test_small_change(self):
        # 1% change → 1 - 0.01/0.10 = 0.9
        s = round_stability(0.541, 0.521, 0.540, 0.520)
        assert 0.85 <= s <= 1.0

    def test_large_change(self):
        # 20% change → score = 0
        s = round_stability(0.60, 0.50, 0.50, 0.40)
        assert s == pytest.approx(0.0)

    def test_missing_current_ask(self):
        assert round_stability(None, 0.52, 0.54, 0.52) == pytest.approx(0.0)

    def test_missing_current_bid(self):
        assert round_stability(0.54, None, 0.54, 0.52) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# hedge_cost_cv
# ---------------------------------------------------------------------------

class TestHedgeCostCV:
    def test_stable(self):
        cv = hedge_cost_cv([1.001, 1.001, 1.001])
        assert cv == pytest.approx(0.0, abs=1e-9)

    def test_variable(self):
        cv = hedge_cost_cv([1.001, 1.005, 1.010])
        assert cv is not None
        assert cv > 0

    def test_single_element(self):
        assert hedge_cost_cv([1.001]) is None

    def test_empty(self):
        assert hedge_cost_cv([]) is None

    def test_higher_variance_higher_cv(self):
        cv_low  = hedge_cost_cv([1.001, 1.001, 1.002])
        cv_high = hedge_cost_cv([1.001, 1.005, 1.015])
        assert cv_high > cv_low


# ---------------------------------------------------------------------------
# stability_score
# ---------------------------------------------------------------------------

class TestStabilityScore:
    def test_perfect_stability(self):
        score = stability_score([1.001, 1.001, 1.001], [150.0, 150.0, 150.0])
        assert score == pytest.approx(100.0)

    def test_empty_returns_zero(self):
        assert stability_score([], []) == pytest.approx(0.0)

    def test_high_variance_low_score(self):
        score = stability_score(
            [1.001, 1.010, 1.020, 1.005],
            [100, 500, 1000, 200],
        )
        assert score < 60

    def test_single_sample(self):
        # Only 1 sample → no variance possible → high stability
        score = stability_score([1.001], [150.0])
        assert score >= 80

    def test_capped_at_100(self):
        score = stability_score([1.001] * 10, [100.0] * 10)
        assert score <= 100.0

    def test_monotone_with_variance(self):
        s_low  = stability_score([1.001, 1.001, 1.002], [100, 100, 101])
        s_high = stability_score([1.001, 1.010, 1.020], [100, 500, 900])
        assert s_low > s_high


# ---------------------------------------------------------------------------
# verdict_for
# ---------------------------------------------------------------------------

def _go_kwargs(**overrides) -> dict:
    """Return all-GO kwargs, apply any overrides."""
    base = dict(
        net_edge_mean=0.01,
        availability=0.90,
        stab=70.0,
        n_rounds=8,
        avg_depth_near_mid=500.0,
        spread_bps_mean=150.0,
        hc_cv=0.005,
        rewards_avail=0.90,
    )
    base.update(overrides)
    return base


class TestVerdictFor:
    # ---- core GO path ----

    def test_go_all_conditions_met(self):
        assert verdict_for(**_go_kwargs()) == "GO"

    # ---- False-GO guardrails: depth ----

    def test_depth_zero_blocks_go(self):
        assert verdict_for(**_go_kwargs(avg_depth_near_mid=0.0)) == "WATCH"

    def test_depth_positive_allows_go(self):
        assert verdict_for(**_go_kwargs(avg_depth_near_mid=1.0)) == "GO"

    # ---- False-GO guardrails: spread ----

    def test_spread_bps_300_allows_go(self):
        assert verdict_for(**_go_kwargs(spread_bps_mean=300.0)) == "GO"

    def test_spread_bps_301_blocks_go(self):
        assert verdict_for(**_go_kwargs(spread_bps_mean=301.0)) == "WATCH"

    def test_spread_bps_1000_blocks_go(self):
        assert verdict_for(**_go_kwargs(spread_bps_mean=1000.0)) == "WATCH"

    def test_spread_bps_6667_blocks_go(self):
        """FIFA-style ultra-wide spread must not produce GO."""
        assert verdict_for(**_go_kwargs(spread_bps_mean=6667.0)) == "WATCH"

    # ---- False-GO guardrails: hedge_cost_cv ----

    def test_cv_none_blocks_go(self):
        """Single-round run → cv=None → cannot confirm GO."""
        assert verdict_for(**_go_kwargs(hc_cv=None)) == "WATCH"

    def test_cv_zero_allows_go(self):
        assert verdict_for(**_go_kwargs(hc_cv=0.0)) == "GO"

    def test_cv_below_threshold_allows_go(self):
        assert verdict_for(**_go_kwargs(hc_cv=0.009)) == "GO"

    def test_cv_at_threshold_blocks_go(self):
        assert verdict_for(**_go_kwargs(hc_cv=0.01)) == "WATCH"

    def test_cv_above_threshold_blocks_go(self):
        assert verdict_for(**_go_kwargs(hc_cv=0.05)) == "WATCH"

    # ---- rewards_availability ----

    def test_low_rewards_avail_blocks_go(self):
        assert verdict_for(**_go_kwargs(rewards_avail=0.79)) == "WATCH"

    def test_low_rewards_avail_triggers_nogo(self):
        assert verdict_for(**_go_kwargs(rewards_avail=0.49)) == "NO-GO"

    # ---- hard NO-GO gates ----

    def test_nogo_negative_edge(self):
        assert verdict_for(**_go_kwargs(net_edge_mean=-0.01)) == "NO-GO"

    def test_nogo_zero_edge(self):
        assert verdict_for(**_go_kwargs(net_edge_mean=0.0)) == "NO-GO"

    def test_nogo_low_availability(self):
        assert verdict_for(**_go_kwargs(availability=0.30)) == "NO-GO"

    def test_nogo_none_edge(self):
        assert verdict_for(**_go_kwargs(net_edge_mean=None)) == "NO-GO"

    def test_nogo_zero_rounds(self):
        assert verdict_for(**_go_kwargs(n_rounds=0)) == "NO-GO"

    # ---- WATCH paths ----

    def test_watch_low_stability(self):
        assert verdict_for(**_go_kwargs(stab=50.0)) == "WATCH"

    def test_watch_low_book_availability(self):
        assert verdict_for(**_go_kwargs(availability=0.60)) == "WATCH"

    def test_watch_depth_zero_spread_ok(self):
        """depth=0 forces WATCH even when spread and cv are good."""
        assert verdict_for(**_go_kwargs(avg_depth_near_mid=0.0, spread_bps_mean=100.0)) == "WATCH"

    def test_watch_spread_high_depth_ok(self):
        """Wide spread forces WATCH even when depth is present."""
        assert verdict_for(**_go_kwargs(avg_depth_near_mid=5000.0, spread_bps_mean=500.0)) == "WATCH"


# ---------------------------------------------------------------------------
# aggregate_samples
# ---------------------------------------------------------------------------

class TestAggregateSamples:
    def _make_samples(self, n: int, hedge_cost_vals: list[float]) -> list[Sample]:
        return [
            _sample(slug="test-mkt", round_n=i,
                    yes_ask=hc - 0.50, no_ask=0.50)
            for i, hc in enumerate(zip(range(n), hedge_cost_vals))
        ]

    def test_basic(self):
        samples = [_sample(slug="s", round_n=i) for i in range(3)]
        stats = aggregate_samples("s", samples)
        assert stats.n_rounds == 3
        assert stats.availability == pytest.approx(1.0)
        assert stats.hedge_cost_mean is not None

    def test_availability(self):
        good = _sample(slug="s", round_n=0)
        bad  = _sample(slug="s", round_n=1, yes_bid=None, yes_ask=None)
        stats = aggregate_samples("s", [good, bad, good])
        assert stats.availability == pytest.approx(2/3, rel=0.01)

    def test_verdict_in_output(self):
        samples = [_sample(slug="s", round_n=i) for i in range(5)]
        stats = aggregate_samples("s", samples)
        assert stats.verdict in ("GO", "WATCH", "NO-GO")

    def test_empty_samples(self):
        stats = aggregate_samples("empty", [])
        assert stats.n_rounds == 0
        assert stats.availability == 0.0
        assert stats.verdict == "NO-GO"

    def test_stability_score_range(self):
        samples = [_sample(slug="s", round_n=i) for i in range(5)]
        stats = aggregate_samples("s", samples)
        assert 0.0 <= stats.stability_score <= 100.0

    def test_rewards_availability(self):
        s_yes = _sample(slug="s", round_n=0, has_rewards=True)
        s_no  = _sample(slug="s", round_n=1, has_rewards=False)
        stats = aggregate_samples("s", [s_yes, s_no])
        assert stats.rewards_availability == pytest.approx(0.5)

    def test_net_edge_in_output(self):
        samples = [_sample(slug="s", round_n=i, rebate_rate=0.25, taker_fee_rate=0.05) for i in range(3)]
        stats = aggregate_samples("s", samples)
        assert stats.net_edge_mean is not None
        assert stats.net_edge_mean > 0   # rebate > hedge cost for typical values

    def test_single_round_no_cv(self):
        # Only 1 round → cv must be None (need ≥2 points for stdev)
        stats = aggregate_samples("s", [_sample(slug="s", round_n=0)])
        assert stats.hedge_cost_cv is None
