"""
tests/test_paper_quote_simulator.py — pure-function tests for paper_quote_simulator.

All tests are offline (no network). Covers:
  fill_probability, simulate_fill, fill_scenario, compute_rebate,
  unrealized_pnl, inventory_var, hedge_cost_one_side, is_toxic_move,
  quote_inside_spread, within_rewards_band, net_pnl_after_hedge,
  round_trip_pnl, inventory_within_cap, SimState methods.
"""

from __future__ import annotations

import math
import random
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from research.paper_quote_simulator import (
    SimState,
    compute_rebate,
    fill_probability,
    fill_scenario,
    hedge_cost_one_side,
    inventory_var,
    inventory_within_cap,
    is_toxic_move,
    net_pnl_after_hedge,
    quote_inside_spread,
    round_trip_pnl,
    simulate_fill,
    unrealized_pnl,
    within_rewards_band,
    TICK,
)


# ---------------------------------------------------------------------------
# fill_probability
# ---------------------------------------------------------------------------

class TestFillProbability:
    def test_improves_bid_high_prob(self):
        # Improving bid (higher than best) → front of queue → ~0.80
        p = fill_probability(0.065, 0.064, 500.0, 200.0, is_bid=True)
        assert p == pytest.approx(0.80)

    def test_at_best_bid_behind_queue(self):
        # At best bid, behind 500 USD → lower prob
        p = fill_probability(0.064, 0.064, 500.0, 200.0, is_bid=True)
        assert 0.0 < p < 0.80

    def test_behind_best_bid_low_prob(self):
        p = fill_probability(0.063, 0.064, 500.0, 200.0, is_bid=True)
        assert p <= 0.05

    def test_improves_ask_high_prob(self):
        # Improving ask (lower than best) → front of queue
        p = fill_probability(0.064, 0.065, 500.0, 200.0, is_bid=False)
        assert p == pytest.approx(0.80)

    def test_zero_quote_size(self):
        assert fill_probability(0.065, 0.064, 500.0, 0.0, is_bid=True) == 0.0

    def test_zero_queue_ahead(self):
        # No queue → higher fill probability than when queue is large
        p_no_q = fill_probability(0.064, 0.064, 0.0, 200.0, is_bid=True)
        p_big_q = fill_probability(0.064, 0.064, 5000.0, 200.0, is_bid=True)
        assert p_no_q >= p_big_q

    def test_result_bounded(self):
        for _ in range(20):
            p = fill_probability(
                round(random.uniform(0.01, 0.99), 3),
                round(random.uniform(0.01, 0.99), 3),
                random.uniform(0, 5000),
                random.uniform(5, 500),
                is_bid=random.choice([True, False]),
            )
            assert 0.0 <= p <= 1.0


# ---------------------------------------------------------------------------
# simulate_fill
# ---------------------------------------------------------------------------

class TestSimulateFill:
    def test_prob_zero_never_fills(self):
        rng = random.Random(42)
        fills = [simulate_fill(0.0, rng) for _ in range(100)]
        assert not any(fills)

    def test_prob_one_always_fills(self):
        rng = random.Random(42)
        fills = [simulate_fill(1.0, rng) for _ in range(100)]
        assert all(fills)

    def test_prob_half_approx(self):
        rng = random.Random(0)
        fills = [simulate_fill(0.5, rng) for _ in range(1000)]
        rate = sum(fills) / len(fills)
        assert 0.40 < rate < 0.60


# ---------------------------------------------------------------------------
# fill_scenario
# ---------------------------------------------------------------------------

class TestFillScenario:
    def test_both(self):
        assert fill_scenario(True, True) == "both"

    def test_yes_only(self):
        assert fill_scenario(True, False) == "yes_only"

    def test_no_only(self):
        assert fill_scenario(False, True) == "no_only"

    def test_none(self):
        assert fill_scenario(False, False) == "none"


# ---------------------------------------------------------------------------
# compute_rebate
# ---------------------------------------------------------------------------

class TestComputeRebate:
    def test_standard(self):
        # price=0.064, size=200, fee=0.04, rebate=0.25
        # = 0.25 * 0.04 * (1-0.064) * 200 = 0.01 * 0.936 * 200 = 1.872
        r = compute_rebate(0.064, 200.0, 0.04, 0.25)
        assert r == pytest.approx(1.872, rel=1e-5)

    def test_zero_size(self):
        assert compute_rebate(0.064, 0.0, 0.04, 0.25) == pytest.approx(0.0)

    def test_zero_rebate_rate(self):
        assert compute_rebate(0.064, 200.0, 0.04, 0.0) == pytest.approx(0.0)

    def test_price_near_one(self):
        # Price close to 1.0 → tiny implied fee → tiny rebate
        r = compute_rebate(0.999, 200.0, 0.04, 0.25)
        assert r < 0.01

    def test_positive(self):
        r = compute_rebate(0.5, 100.0, 0.05, 0.25)
        assert r > 0


# ---------------------------------------------------------------------------
# unrealized_pnl
# ---------------------------------------------------------------------------

class TestUnrealizedPnl:
    def test_no_inventory(self):
        assert unrealized_pnl(0, 0, 0, 0, 0.064, 0.936) == pytest.approx(0.0)

    def test_price_rises(self):
        # Bought 200 YES at 0.064, now 0.070 → gain
        pnl = unrealized_pnl(200, 0, 0.064, 0, 0.070, 0.930)
        assert pnl > 0

    def test_price_falls(self):
        pnl = unrealized_pnl(200, 0, 0.070, 0, 0.064, 0.936)
        assert pnl < 0

    def test_symmetric_yes_no(self):
        # Long YES + Long NO at same cost → if prices unchanged → zero pnl
        pnl = unrealized_pnl(100, 100, 0.064, 0.936, 0.064, 0.936)
        assert pnl == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# inventory_var
# ---------------------------------------------------------------------------

class TestInventoryVar:
    def test_zero_inventory(self):
        assert inventory_var(0, 0, 0.064) == pytest.approx(0.0)

    def test_positive_inventory(self):
        var = inventory_var(200, 0, 0.064)
        assert var > 0

    def test_balanced_inventory_lower_var(self):
        # Both YES and NO → net exposure lower → lower VaR than one-sided
        var_unilateral = inventory_var(200, 0, 0.5)
        var_balanced   = inventory_var(200, 200, 0.5)
        assert var_balanced < var_unilateral

    def test_result_nonnegative(self):
        var = inventory_var(100, 50, 0.3)
        assert var >= 0


# ---------------------------------------------------------------------------
# hedge_cost_one_side
# ---------------------------------------------------------------------------

class TestHedgeCostOneSide:
    def test_yes_filled_no_not(self):
        cost = hedge_cost_one_side(True, False, no_ask=0.937, yes_ask=None, fill_size=200)
        assert cost == pytest.approx(200 * 0.937)

    def test_no_filled_yes_not(self):
        cost = hedge_cost_one_side(False, True, no_ask=None, yes_ask=0.065, fill_size=200)
        assert cost == pytest.approx(200 * 0.065)

    def test_both_filled_no_hedge_needed(self):
        cost = hedge_cost_one_side(True, True, 0.937, 0.065, 200)
        assert cost is None

    def test_none_filled(self):
        cost = hedge_cost_one_side(False, False, 0.937, 0.065, 200)
        assert cost is None

    def test_missing_hedge_price(self):
        # YES filled but no_ask is None → cannot compute hedge
        cost = hedge_cost_one_side(True, False, no_ask=None, yes_ask=0.065, fill_size=200)
        assert cost is None


# ---------------------------------------------------------------------------
# is_toxic_move
# ---------------------------------------------------------------------------

class TestIsToxicMove:
    def test_not_toxic_small_move(self):
        assert not is_toxic_move(0.064, 0.063, spread=0.001, multiplier=3.0)

    def test_toxic_large_drop(self):
        # Bought at 0.064, price dropped to 0.060 → delta=0.004 > 3*0.001=0.003
        assert is_toxic_move(0.064, 0.060, spread=0.001, multiplier=3.0)

    def test_exact_threshold_not_toxic(self):
        # delta=0.002 < 3*0.001=0.003 → NOT toxic
        assert not is_toxic_move(0.064, 0.062, spread=0.001, multiplier=3.0)

    def test_zero_spread_never_toxic(self):
        assert not is_toxic_move(0.064, 0.060, spread=0.0)

    def test_price_above_buy_not_toxic(self):
        # Price rose → we're in profit, not toxic
        assert not is_toxic_move(0.064, 0.070, spread=0.001)


# ---------------------------------------------------------------------------
# quote_inside_spread
# ---------------------------------------------------------------------------

class TestQuoteInsideSpread:
    def test_normal_spread(self):
        # 2-tick spread → improve by 1 tick each side → bid=0.065, ask=0.065
        # but bid>=ask → fallback to best quotes (0.064, 0.066)
        bid, ask = quote_inside_spread(0.064, 0.066, tick=0.001)
        assert bid < ask
        assert bid >= 0.064
        assert ask <= 0.066

    def test_tight_spread_fallback(self):
        # Spread = 1 tick → can't improve, return best
        bid, ask = quote_inside_spread(0.064, 0.065, tick=0.001)
        assert bid == pytest.approx(0.064)
        assert ask == pytest.approx(0.065)

    def test_wider_spread(self):
        bid, ask = quote_inside_spread(0.060, 0.070, tick=0.001)
        assert bid > 0.060
        assert ask < 0.070
        assert bid < ask

    def test_quotes_do_not_cross(self):
        bid, ask = quote_inside_spread(0.050, 0.055, tick=0.001)
        assert bid < ask


# ---------------------------------------------------------------------------
# within_rewards_band
# ---------------------------------------------------------------------------

class TestWithinRewardsBand:
    def test_inside_band(self):
        # mid=0.065, our_bid=0.064, our_ask=0.066 → ~15 bps from mid
        assert within_rewards_band(0.064, 0.066, 0.065, max_spread_bps=350)

    def test_outside_band(self):
        # mid=0.065, our_bid=0.030 → far outside
        assert not within_rewards_band(0.030, 0.100, 0.065, max_spread_bps=350)

    def test_zero_mid(self):
        assert not within_rewards_band(0.064, 0.066, 0.0, max_spread_bps=350)


# ---------------------------------------------------------------------------
# net_pnl_after_hedge
# ---------------------------------------------------------------------------

class TestNetPnlAfterHedge:
    def test_no_hedge_costs(self):
        assert net_pnl_after_hedge(0.05, 0.02, []) == pytest.approx(0.07)

    def test_with_hedge_costs(self):
        result = net_pnl_after_hedge(0.10, 0.03, [0.05, 0.02])
        assert result == pytest.approx(0.10 + 0.03 - 0.05 - 0.02)

    def test_negative_after_hedge(self):
        result = net_pnl_after_hedge(0.01, 0.005, [0.10])
        assert result < 0


# ---------------------------------------------------------------------------
# round_trip_pnl
# ---------------------------------------------------------------------------

class TestRoundTripPnl:
    def test_profitable(self):
        # Sold YES at 0.065 and NO at 0.937 → received 1.002 > 1.0
        pnl = round_trip_pnl(0.064, 0.936, 0.065, 0.937, fill_size=200)
        assert pnl > 0

    def test_breakeven(self):
        pnl = round_trip_pnl(0.50, 0.50, 0.50, 0.50, fill_size=200)
        assert pnl == pytest.approx(0.0)

    def test_loss(self):
        # Sold combined below 1.0
        pnl = round_trip_pnl(0.50, 0.50, 0.45, 0.45, fill_size=100)
        assert pnl < 0

    def test_scales_with_size(self):
        p1 = round_trip_pnl(0.064, 0.936, 0.065, 0.937, fill_size=100)
        p2 = round_trip_pnl(0.064, 0.936, 0.065, 0.937, fill_size=200)
        assert pytest.approx(p2) == 2 * p1


# ---------------------------------------------------------------------------
# inventory_within_cap
# ---------------------------------------------------------------------------

class TestInventoryWithinCap:
    def test_no_cap(self):
        inv, capped = inventory_within_cap(100.0, 200.0, 600.0)
        assert inv == pytest.approx(300.0)
        assert not capped

    def test_at_cap(self):
        inv, capped = inventory_within_cap(500.0, 200.0, 600.0)
        assert inv == pytest.approx(600.0)
        assert capped

    def test_zero_add(self):
        inv, capped = inventory_within_cap(100.0, 0.0, 600.0)
        assert inv == pytest.approx(100.0)
        assert not capped

    def test_exactly_at_cap(self):
        inv, capped = inventory_within_cap(400.0, 200.0, 600.0)
        assert inv == pytest.approx(600.0)
        assert not capped   # exactly at cap, not over


# ---------------------------------------------------------------------------
# SimState
# ---------------------------------------------------------------------------

class TestSimState:
    def test_absorb_yes_fill_updates_inventory(self):
        s = SimState()
        capped = s.absorb_yes_fill(0.064, 200.0, 600.0)
        assert not capped
        assert s.yes_inventory == pytest.approx(200.0)
        assert s.yes_avg_cost  == pytest.approx(0.064)

    def test_absorb_at_cap(self):
        s = SimState()
        s.absorb_yes_fill(0.064, 600.0, 600.0)
        capped = s.absorb_yes_fill(0.064, 1.0, 600.0)
        assert capped

    def test_avg_cost_updates_correctly(self):
        s = SimState()
        s.absorb_yes_fill(0.060, 100.0, 600.0)
        s.absorb_yes_fill(0.070, 100.0, 600.0)
        assert s.yes_avg_cost == pytest.approx(0.065, rel=1e-4)

    def test_flush_round_trip_books_pnl(self):
        s = SimState()
        s.absorb_yes_fill(0.064, 200.0, 600.0)
        s.absorb_no_fill(0.936, 200.0, 600.0)
        s.flush_round_trip(0.065, 0.937, 200.0, 600.0)
        assert s.realized_pnl > 0

    def test_flush_reduces_inventory(self):
        s = SimState()
        s.absorb_yes_fill(0.064, 200.0, 600.0)
        s.absorb_no_fill(0.936, 200.0, 600.0)
        s.flush_round_trip(0.065, 0.937, 200.0, 600.0)
        assert s.yes_inventory < 200.0

    def test_total_fills_increments(self):
        s = SimState()
        s.absorb_yes_fill(0.064, 200.0, 600.0)
        s.absorb_no_fill(0.936, 200.0, 600.0)
        assert s.total_fills == 2
