"""tests/test_paper_anchor_executor.py — offline unit tests for pure functions."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from research.paper_anchor_executor import (
    classify_skip_reason,
    compute_paper_pnl,
    enforce_one_trade_per_window,
    should_enter_trade,
    summarize_trades,
    DIST_DEFAULT,
    DIST_BASELINE,
    FEE_RATE,
    OFFSET_ALLOWED,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cp(offset=180, dist=130.0, spread=0.01, direction="UP", triggered=True, error=None):
    return {
        "offset_s": offset, "distance": dist, "poly_spread": spread,
        "direction": direction, "triggered": triggered, "error": error,
        "poly_ask": 0.51, "poly_bid": 0.49, "btc_live": 77000.0, "ts_utc": "2026-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# should_enter_trade
# ---------------------------------------------------------------------------

class TestShouldEnterTrade:
    def test_enter_valid(self):
        ok, reason = should_enter_trade(180, 130.0, 0.01, DIST_DEFAULT)
        assert ok is True
        assert reason == "ok"

    def test_t90_blocked(self):
        ok, reason = should_enter_trade(90, 200.0, 0.01, DIST_DEFAULT)
        assert ok is False
        assert "offset_blocked" in reason
        assert "90" in reason

    def test_t120_blocked(self):
        ok, reason = should_enter_trade(120, 200.0, 0.01, DIST_DEFAULT)
        assert ok is False
        assert "offset_blocked" in reason

    def test_dist_too_small(self):
        ok, reason = should_enter_trade(180, 50.0, 0.01, DIST_DEFAULT)
        assert ok is False
        assert "dist_too_small" in reason

    def test_dist_exactly_threshold(self):
        ok, _ = should_enter_trade(180, DIST_DEFAULT, 0.01, DIST_DEFAULT)
        assert ok is True

    def test_spread_too_wide(self):
        ok, reason = should_enter_trade(180, 130.0, 0.04, DIST_DEFAULT)
        assert ok is False
        assert "spread_wide" in reason

    def test_spread_at_limit(self):
        ok, _ = should_enter_trade(180, 130.0, 0.03, DIST_DEFAULT)
        assert ok is True

    def test_spread_none_blocked(self):
        ok, reason = should_enter_trade(180, 130.0, None, DIST_DEFAULT)
        assert ok is False

    def test_baseline_dist(self):
        ok, _ = should_enter_trade(180, DIST_BASELINE, 0.01, DIST_BASELINE)
        assert ok is True

    def test_below_baseline_dist(self):
        ok, _ = should_enter_trade(180, DIST_BASELINE - 1, 0.01, DIST_BASELINE)
        assert ok is False

    def test_only_t180_allowed(self):
        for off in (60, 90, 120, 240, 300):
            ok, reason = should_enter_trade(off, 200.0, 0.01, DIST_DEFAULT)
            assert ok is False, f"offset {off} should be blocked"
        ok, _ = should_enter_trade(180, 200.0, 0.01, DIST_DEFAULT)
        assert ok is True


# ---------------------------------------------------------------------------
# compute_paper_pnl
# ---------------------------------------------------------------------------

class TestComputePaperPnl:
    def test_win_up(self):
        # entry at 0.51, fee=0.07*(1-0.51)=0.0343, cost=0.5443
        pnl = compute_paper_pnl("UP", "UP", 0.51)
        assert pnl == pytest.approx(1.0 - (0.51 + FEE_RATE * (1 - 0.51)), abs=1e-5)
        assert pnl > 0

    def test_loss_up(self):
        pnl = compute_paper_pnl("UP", "DOWN", 0.51)
        expected = 0.0 - (0.51 + FEE_RATE * (1 - 0.51))
        assert pnl == pytest.approx(expected, abs=1e-5)
        assert pnl < 0

    def test_win_down(self):
        pnl = compute_paper_pnl("DOWN", "DOWN", 0.49)
        assert pnl > 0

    def test_loss_down(self):
        pnl = compute_paper_pnl("DOWN", "UP", 0.49)
        assert pnl < 0

    def test_fee_reduces_win(self):
        pnl_win = compute_paper_pnl("UP", "UP", 0.50)
        assert pnl_win < 1.0 - 0.50   # fee bites into gross

    def test_fee_increases_loss(self):
        pnl_loss = compute_paper_pnl("UP", "DOWN", 0.50)
        assert pnl_loss < -0.50        # total loss > entry price

    def test_break_even_price(self):
        # at entry=0.535, cost≈1.0 → win pnl ≈ 0
        be_price = 1.0 / (1.0 + FEE_RATE * (1.0 / 0.535 - 1.0))  # rough
        # just verify sign
        pnl = compute_paper_pnl("UP", "UP", 0.535)
        assert pnl > -0.05  # near breakeven

    def test_higher_entry_lower_win(self):
        pnl_cheap = compute_paper_pnl("UP", "UP", 0.49)
        pnl_dear  = compute_paper_pnl("UP", "UP", 0.55)
        assert pnl_cheap > pnl_dear


# ---------------------------------------------------------------------------
# enforce_one_trade_per_window
# ---------------------------------------------------------------------------

class TestEnforceOneTradePerWindow:
    def test_picks_t180(self):
        cps = [_cp(90, 200.0), _cp(120, 200.0), _cp(180, 130.0)]
        result = enforce_one_trade_per_window(cps, DIST_DEFAULT)
        assert result is not None
        assert result["offset_s"] == 180

    def test_returns_none_if_no_t180(self):
        cps = [_cp(90, 200.0), _cp(120, 200.0)]
        assert enforce_one_trade_per_window(cps, DIST_DEFAULT) is None

    def test_returns_none_if_dist_below(self):
        cps = [_cp(180, 50.0)]
        assert enforce_one_trade_per_window(cps, DIST_DEFAULT) is None

    def test_returns_none_if_spread_wide(self):
        cps = [_cp(180, 130.0, spread=0.05)]
        assert enforce_one_trade_per_window(cps, DIST_DEFAULT) is None

    def test_skips_errored_checkpoints(self):
        cps = [_cp(180, 130.0, error="timeout"), _cp(180, 125.0)]
        result = enforce_one_trade_per_window(cps, DIST_DEFAULT)
        assert result is not None
        assert result["distance"] == 125.0

    def test_skips_not_triggered(self):
        cps = [_cp(180, 130.0, triggered=False)]
        assert enforce_one_trade_per_window(cps, DIST_DEFAULT) is None

    def test_empty_list(self):
        assert enforce_one_trade_per_window([], DIST_DEFAULT) is None

    def test_one_trade_max_from_multiple_valid(self):
        # two valid T+180 checkpoints — should return first (lowest offset_s, tie-broken by order)
        cps = [_cp(180, 200.0), _cp(180, 300.0)]
        result = enforce_one_trade_per_window(cps, DIST_DEFAULT)
        assert result is not None
        # both valid; just confirms one is returned
        assert result["offset_s"] == 180

    def test_baseline_threshold(self):
        cps = [_cp(180, 105.0)]   # passes baseline but not default
        assert enforce_one_trade_per_window(cps, DIST_DEFAULT) is None
        assert enforce_one_trade_per_window(cps, DIST_BASELINE) is not None


# ---------------------------------------------------------------------------
# summarize_trades
# ---------------------------------------------------------------------------

def _trade(pnl, skipped=False):
    return {"pnl": pnl, "skipped": skipped, "direction": "UP", "outcome": "UP" if pnl > 0 else "DOWN"}


class TestSummarizeTrades:
    def test_empty(self):
        s = summarize_trades([])
        assert s["total_trades"] == 0
        assert s["win_rate"] is None

    def test_all_wins(self):
        trades = [_trade(0.45), _trade(0.45), _trade(0.45)]
        s = summarize_trades(trades)
        assert s["total_trades"] == 3
        assert s["win_rate"] == pytest.approx(1.0)
        assert s["cumulative_pnl"] == pytest.approx(1.35, abs=1e-5)

    def test_all_losses(self):
        trades = [_trade(-0.54), _trade(-0.54)]
        s = summarize_trades(trades)
        assert s["win_rate"] == pytest.approx(0.0)
        assert s["longest_loss_streak"] == 2

    def test_skipped_not_counted(self):
        trades = [_trade(0.45), _trade(-0.54, skipped=True)]
        s = summarize_trades(trades)
        assert s["total_trades"] == 1
        assert s["skipped_count"] == 1

    def test_max_drawdown(self):
        # profit then loss: peak=0.45, then -0.54 → dd=0.54
        trades = [_trade(0.45), _trade(-0.54)]
        s = summarize_trades(trades)
        assert s["max_drawdown"] == pytest.approx(0.54, abs=1e-5)

    def test_loss_streak(self):
        trades = [_trade(0.45), _trade(-0.54), _trade(-0.54), _trade(-0.54), _trade(0.45)]
        s = summarize_trades(trades)
        assert s["longest_loss_streak"] == 3

    def test_mean_pnl(self):
        trades = [_trade(0.40), _trade(0.60)]
        s = summarize_trades(trades)
        assert s["mean_pnl"] == pytest.approx(0.50, abs=1e-5)

    def test_all_skipped(self):
        trades = [_trade(0.45, skipped=True), _trade(0.45, skipped=True)]
        s = summarize_trades(trades)
        assert s["total_trades"] == 0
        assert s["skipped_count"] == 2


# ---------------------------------------------------------------------------
# classify_skip_reason
# ---------------------------------------------------------------------------

class TestClassifySkipReason:
    def test_t90_blocked(self):
        r = classify_skip_reason(90, 200.0, 0.01)
        assert "offset_blocked" in r

    def test_t120_blocked(self):
        r = classify_skip_reason(120, 200.0, 0.01)
        assert "offset_blocked" in r

    def test_below_baseline(self):
        r = classify_skip_reason(180, DIST_BASELINE - 1, 0.01)
        assert r == "below_baseline"

    def test_below_default_above_baseline(self):
        r = classify_skip_reason(180, DIST_BASELINE + 5, 0.01)
        assert r == "below_default_above_baseline"

    def test_spread_too_wide(self):
        r = classify_skip_reason(180, DIST_DEFAULT + 10, 0.05)
        assert r == "spread_too_wide"

    def test_spread_none(self):
        r = classify_skip_reason(180, DIST_DEFAULT + 10, None)
        assert r == "spread_too_wide"
