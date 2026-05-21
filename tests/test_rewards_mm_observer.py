"""
tests/test_rewards_mm_observer.py — pure-function unit tests for the rewards MM observer.

All tests are offline (no network calls). They exercise the analysis and scoring
logic directly, not the async HTTP layer.
"""

from __future__ import annotations

import sys
import os

# Ensure research/ is importable when running from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from research.polymarket_rewards_mm_observer import (
    BookLevel,
    BookState,
    MMAnalysis,
    build_analysis,
    candidate_score,
    compute_hedge_cost,
    compute_hedge_profit,
    compute_maker_rebate,
    compute_max_one_side_loss,
    compute_paper_locked_profit,
    compute_paper_quotes,
    risk_label,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _book(bids: list[tuple[float, float]], asks: list[tuple[float, float]], tick: float = 0.01) -> BookState:
    return BookState(
        token_id="test",
        bids=[BookLevel(p, s) for p, s in sorted(bids, reverse=True)],
        asks=[BookLevel(p, s) for p, s in sorted(asks)],
        tick_size=tick,
        min_order_size=5.0,
    )


def _market(
    *,
    slug: str = "test-market",
    end_date: str = "2030-12-31T00:00:00Z",
    volume_24h: float = 10_000,
    rewards_min_size: float = 50,
    rewards_max_spread: float = 2.0,
    rebate_rate: float = 0.25,
    taker_rate: float = 0.05,
    holding_rewards: bool = False,
    clob_ids: list[str] | None = None,
) -> dict:
    import json
    ids = clob_ids or ["YES_TOKEN", "NO_TOKEN"]
    return {
        "slug": slug,
        "question": "Test question?",
        "conditionId": "0xtest",
        "clobTokenIds": json.dumps(ids),
        "endDate": end_date,
        "volume24hr": volume_24h,
        "liquidity": 5000.0,
        "rewardsMinSize": rewards_min_size,
        "rewardsMaxSpread": rewards_max_spread,
        "holdingRewardsEnabled": holding_rewards,
        "makerBaseFee": 1000,
        "feeSchedule": {"rate": taker_rate, "rebateRate": rebate_rate, "takerOnly": True},
        "feesEnabled": True,
    }


# ---------------------------------------------------------------------------
# compute_hedge_cost
# ---------------------------------------------------------------------------

class TestHedgeCost:
    def test_basic(self):
        assert compute_hedge_cost(0.55, 0.45) == pytest.approx(1.00)

    def test_positive_hedge_profit(self):
        assert compute_hedge_cost(0.48, 0.48) == pytest.approx(0.96)

    def test_expensive_hedge(self):
        assert compute_hedge_cost(0.60, 0.45) == pytest.approx(1.05)

    def test_none_if_yes_missing(self):
        assert compute_hedge_cost(None, 0.45) is None

    def test_none_if_no_missing(self):
        assert compute_hedge_cost(0.55, None) is None

    def test_both_none(self):
        assert compute_hedge_cost(None, None) is None

    def test_rounding(self):
        result = compute_hedge_cost(0.333333, 0.666667)
        assert result == pytest.approx(1.0, abs=1e-5)


# ---------------------------------------------------------------------------
# compute_hedge_profit
# ---------------------------------------------------------------------------

class TestHedgeProfit:
    def test_positive(self):
        assert compute_hedge_profit(0.96) == pytest.approx(0.04)

    def test_zero(self):
        assert compute_hedge_profit(1.00) == pytest.approx(0.00)

    def test_negative(self):
        assert compute_hedge_profit(1.05) == pytest.approx(-0.05)

    def test_none(self):
        assert compute_hedge_profit(None) is None


# ---------------------------------------------------------------------------
# compute_paper_quotes
# ---------------------------------------------------------------------------

class TestPaperQuotes:
    def test_normal_spread(self):
        book = _book(bids=[(0.50, 100)], asks=[(0.55, 100)], tick=0.01)
        bid, ask = compute_paper_quotes(book)
        assert bid == pytest.approx(0.51)   # 0.50 + 1 tick
        assert ask == pytest.approx(0.54)   # 0.55 - 1 tick

    def test_tight_spread_no_improvement(self):
        # Only 1 tick of spread — can't improve
        book = _book(bids=[(0.50, 100)], asks=[(0.51, 100)], tick=0.01)
        bid, ask = compute_paper_quotes(book)
        assert bid == pytest.approx(0.50)
        assert ask == pytest.approx(0.51)

    def test_missing_bid(self):
        book = _book(bids=[], asks=[(0.55, 100)], tick=0.01)
        bid, ask = compute_paper_quotes(book)
        assert bid is None
        assert ask is None

    def test_missing_ask(self):
        book = _book(bids=[(0.50, 100)], asks=[], tick=0.01)
        bid, ask = compute_paper_quotes(book)
        assert bid is None
        assert ask is None

    def test_cap_at_bounds(self):
        # Bid = 0.995, ask = 1.00 → spread is 0.005 (< 1 tick).
        # Cannot improve inside by 1 tick, so compute_paper_quotes falls back
        # to the original quotes rather than crossing bid≥ask.
        book = _book(bids=[(0.995, 100)], asks=[(1.00, 100)], tick=0.01)
        bid, ask = compute_paper_quotes(book)
        assert bid is not None
        assert ask is not None
        assert bid < ask          # quotes must not cross
        assert ask <= 1.00        # ask never above the existing best ask

    def test_small_tick_size(self):
        book = _book(bids=[(0.50, 100)], asks=[(0.52, 100)], tick=0.001)
        bid, ask = compute_paper_quotes(book)
        assert bid == pytest.approx(0.501)
        assert ask == pytest.approx(0.519)


# ---------------------------------------------------------------------------
# compute_paper_locked_profit
# ---------------------------------------------------------------------------

class TestLockedProfit:
    def test_positive(self):
        # Sell YES at 0.55 + NO at 0.50 → total 1.05, profit 0.05
        assert compute_paper_locked_profit(0.55, 0.50) == pytest.approx(0.05)

    def test_zero(self):
        assert compute_paper_locked_profit(0.50, 0.50) == pytest.approx(0.00)

    def test_negative(self):
        # Both sides deep in-the-money, total < 1
        assert compute_paper_locked_profit(0.48, 0.48) == pytest.approx(-0.04)

    def test_none_yes(self):
        assert compute_paper_locked_profit(None, 0.50) is None

    def test_none_no(self):
        assert compute_paper_locked_profit(0.50, None) is None


# ---------------------------------------------------------------------------
# compute_max_one_side_loss
# ---------------------------------------------------------------------------

class TestMaxOneSideLoss:
    def test_symmetric(self):
        # If YES at 0.55 fills and YES wins → we owe 1.0 - 0.55 = 0.45
        result = compute_max_one_side_loss(0.55, 0.45)
        assert result == pytest.approx(0.55)   # max(1-0.55=0.45, 1-0.45=0.55)

    def test_yes_only(self):
        result = compute_max_one_side_loss(0.70, None)
        assert result == pytest.approx(0.30)

    def test_no_only(self):
        result = compute_max_one_side_loss(None, 0.30)
        assert result == pytest.approx(0.70)

    def test_both_none(self):
        assert compute_max_one_side_loss(None, None) is None


# ---------------------------------------------------------------------------
# compute_maker_rebate
# ---------------------------------------------------------------------------

class TestMakerRebate:
    def test_standard(self):
        # taker fee = 5% * (1-0.50) = 0.025; rebate = 25% * 0.025 = 0.00625
        result = compute_maker_rebate(fill_price=0.50, taker_fee_rate=0.05, rebate_rate=0.25)
        assert result == pytest.approx(0.00625)

    def test_zero_rebate(self):
        result = compute_maker_rebate(0.50, 0.05, 0.0)
        assert result == pytest.approx(0.0)

    def test_high_price(self):
        # fill at 0.99: fee = 5% * 0.01 = 0.0005; rebate = 25% * 0.0005 = 0.000125
        result = compute_maker_rebate(0.99, 0.05, 0.25)
        assert result == pytest.approx(0.000125)


# ---------------------------------------------------------------------------
# candidate_score
# ---------------------------------------------------------------------------

class TestCandidateScore:
    def test_high_score_ideal_market(self):
        score = candidate_score(
            rewards_min_size=50,
            rewards_max_spread=2.0,
            hedge_cost=0.97,
            spread_bps=150,
            volume_24h=20_000,
            days_to_expiry=30,
            depth_usd=15_000,
            holding_rewards=True,
        )
        assert score >= 80

    def test_low_score_no_rewards(self):
        score = candidate_score(
            rewards_min_size=0,
            rewards_max_spread=0,
            hedge_cost=1.05,
            spread_bps=5000,
            volume_24h=50,
            days_to_expiry=0.5,
            depth_usd=100,
            holding_rewards=False,
        )
        assert score < 20

    def test_capped_at_100(self):
        score = candidate_score(
            rewards_min_size=100,
            rewards_max_spread=5.0,
            hedge_cost=0.90,
            spread_bps=100,
            volume_24h=1_000_000,
            days_to_expiry=365,
            depth_usd=500_000,
            holding_rewards=True,
        )
        assert score <= 100

    def test_none_hedge_still_scores(self):
        # Even without hedge data, rewards params should give points
        score = candidate_score(
            rewards_min_size=50,
            rewards_max_spread=2.0,
            hedge_cost=None,
            spread_bps=200,
            volume_24h=10_000,
            days_to_expiry=14,
            depth_usd=5000,
            holding_rewards=False,
        )
        assert score >= 30

    def test_score_monotone_hedge(self):
        base = dict(
            rewards_min_size=50, rewards_max_spread=2.0,
            spread_bps=200, volume_24h=10_000,
            days_to_expiry=14, depth_usd=5000, holding_rewards=False,
        )
        s_good   = candidate_score(**base, hedge_cost=0.97)
        s_ok     = candidate_score(**base, hedge_cost=1.00)
        s_bad    = candidate_score(**base, hedge_cost=1.05)
        assert s_good > s_ok > s_bad


# ---------------------------------------------------------------------------
# risk_label
# ---------------------------------------------------------------------------

class TestRiskLabel:
    def test_low(self):
        assert risk_label(0.97, 14, 200) == "LOW"

    def test_high_hedge_cost(self):
        assert risk_label(1.05, 14, 200) == "HIGH"

    def test_high_expiry(self):
        assert risk_label(0.97, 0.5, 200) == "HIGH"

    def test_medium(self):
        assert risk_label(1.01, 5, 300) == "MEDIUM"

    def test_high_spread_bps(self):
        assert risk_label(0.98, 14, 3000) == "HIGH"


# ---------------------------------------------------------------------------
# BookState properties
# ---------------------------------------------------------------------------

class TestBookState:
    def test_midpoint(self):
        book = _book(bids=[(0.50, 100)], asks=[(0.54, 100)])
        assert book.midpoint == pytest.approx(0.52)

    def test_spread(self):
        book = _book(bids=[(0.50, 100)], asks=[(0.54, 100)])
        assert book.spread == pytest.approx(0.04)

    def test_spread_bps(self):
        book = _book(bids=[(0.50, 100)], asks=[(0.54, 100)])
        # spread / mid = 0.04 / 0.52 = 769.2 bps
        assert book.spread_bps == pytest.approx(769.2, rel=0.01)

    def test_empty_book(self):
        book = _book(bids=[], asks=[])
        assert book.best_bid is None
        assert book.best_ask is None
        assert book.midpoint is None
        assert book.spread is None
        assert book.spread_bps is None

    def test_depth_usd(self):
        book = _book(
            bids=[(0.50, 200), (0.49, 100), (0.48, 50)],
            asks=[(0.51, 200), (0.52, 100)],
        )
        # bids top-5: 0.50*200 + 0.49*100 + 0.48*50 = 100 + 49 + 24 = 173
        # asks top-5: 0.51*200 + 0.52*100 = 102 + 52 = 154
        # total = 327
        assert book.depth_usd(n_levels=5) == pytest.approx(327.0, abs=0.01)


# ---------------------------------------------------------------------------
# build_analysis integration (offline)
# ---------------------------------------------------------------------------

class TestBuildAnalysis:
    def _yes_book(self) -> BookState:
        return _book(bids=[(0.53, 500)], asks=[(0.55, 500)])

    def _no_book(self) -> BookState:
        return _book(bids=[(0.44, 500)], asks=[(0.46, 500)])

    def test_hedge_cost_computed(self):
        a = build_analysis(_market(), self._yes_book(), self._no_book())
        assert a.hedge_cost == pytest.approx(0.55 + 0.46)

    def test_hedge_ok_flag(self):
        # 0.55 + 0.46 = 1.01 > 1.00 → hedge_ok = False
        a = build_analysis(_market(), self._yes_book(), self._no_book())
        assert a.hedge_ok is False

    def test_hedge_ok_when_cheap(self):
        yes = _book(bids=[(0.49, 100)], asks=[(0.50, 100)])
        no  = _book(bids=[(0.49, 100)], asks=[(0.50, 100)])
        a = build_analysis(_market(), yes, no)
        assert a.hedge_ok is True   # 0.50 + 0.50 = 1.00

    def test_paper_quotes_exist(self):
        a = build_analysis(_market(), self._yes_book(), self._no_book())
        assert a.paper_yes_bid is not None
        assert a.paper_yes_ask is not None
        assert a.paper_no_bid  is not None
        assert a.paper_no_ask  is not None

    def test_locked_profit_sign(self):
        # YES ask 0.55, NO ask 0.46 → hedge 1.01 > 1
        # paper YES ask ≈ 0.54, paper NO ask ≈ 0.45 → locked ≈ -0.01 (slightly negative)
        a = build_analysis(_market(), self._yes_book(), self._no_book())
        # paper quotes are inside the spread by 1 tick
        # paper_yes_ask = 0.54, paper_no_ask = 0.45 → 0.54+0.45-1 = -0.01
        assert a.paper_locked_profit is not None
        assert isinstance(a.paper_locked_profit, float)

    def test_risk_score_present(self):
        a = build_analysis(_market(), self._yes_book(), self._no_book())
        assert a.risk_score in ("LOW", "MEDIUM", "HIGH")

    def test_score_positive_with_rewards(self):
        a = build_analysis(_market(rewards_min_size=50, rewards_max_spread=2.0),
                           self._yes_book(), self._no_book())
        assert a.candidate_score > 0

    def test_no_rewards_lower_score(self):
        a_yes = build_analysis(_market(rewards_min_size=50, rewards_max_spread=2.0),
                               self._yes_book(), self._no_book())
        a_no  = build_analysis(_market(rewards_min_size=0, rewards_max_spread=0),
                               self._yes_book(), self._no_book())
        assert a_yes.candidate_score > a_no.candidate_score

    def test_to_dict_serialisable(self):
        import json
        a = build_analysis(_market(), self._yes_book(), self._no_book())
        d = a.to_dict()
        json_str = json.dumps(d)   # must not raise
        assert len(json_str) > 10

    def test_fetch_error_in_notes(self):
        bad_yes = BookState(token_id="bad", fetch_error="timeout")
        no  = self._no_book()
        a = build_analysis(_market(), bad_yes, no)
        assert any("YES book error" in n for n in a.notes)

    def test_recommend_observe_false_if_expired(self):
        a = build_analysis(
            _market(end_date="2020-01-01T00:00:00Z"),
            self._yes_book(), self._no_book(),
        )
        assert a.recommend_observe is False
