"""
tests/test_mvp_runner_stop_loss.py

Unit tests for the relative stop-loss logic added in Step 10
(_manage_positions + STOP_LOSS_PCT in mvp_runner.py).

Tests use the module-level ``state`` object directly (reset via fixture).
No network, no async, no real trading.
"""
from __future__ import annotations

import time

import pytest

import mvp_runner
from mvp_runner import (
    HOLD_WINDOW_S,
    INITIAL_CASH,
    PolyMarket,
    Position,
    STOP_LOSS_PCT,
    _manage_positions,
    state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_state():
    """Reset relevant global state before (and after) every test."""
    state.open_positions.clear()
    state.closed_positions.clear()
    state.poly_latest.clear()
    state.cash = INITIAL_CASH
    state.stop_loss_count = 0
    yield
    state.open_positions.clear()
    state.closed_positions.clear()
    state.poly_latest.clear()
    state.cash = INITIAL_CASH
    state.stop_loss_count = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_position(
    market_id: str = "poly-sol-001",
    asset: str = "SOL",
    entry_yes_price: float = 0.50,
    opened_ms_ago: int = 0,
    notional: float = 20.0,
    quantity: float = 40.0,
    fees: float = 0.04,
) -> Position:
    now_ms = int(time.time() * 1000)
    return Position(
        opened_ts_ms=now_ms - opened_ms_ago,
        asset=asset,
        okx_market_id="SOL-USDT",
        poly_market_id=market_id,
        poly_symbol="Solana Up or Down Test",
        entry_yes_price=entry_yes_price,
        raw_yes_price=round(entry_yes_price / 1.002, 4),
        notional=notional,
        quantity=quantity,
        fees=fees,
        signal_pct_move=0.0015,
    )


def _make_pm(market_id: str, yes_price: float) -> PolyMarket:
    return PolyMarket(
        market_id=market_id,
        symbol="Solana Up or Down Test",
        yes_price=yes_price,
        no_price=round(1.0 - yes_price, 6),
        ts_ms=int(time.time() * 1000),
    )


# ---------------------------------------------------------------------------
# STOP_LOSS_PCT constant value
# ---------------------------------------------------------------------------

class TestStopLossPctConstant:
    def test_default_value(self):
        assert STOP_LOSS_PCT == pytest.approx(0.12)

    def test_positive(self):
        assert STOP_LOSS_PCT > 0.0

    def test_less_than_one(self):
        assert STOP_LOSS_PCT < 1.0


# ---------------------------------------------------------------------------
# Relative stop-loss NOT triggered
# ---------------------------------------------------------------------------

class TestStopLossNotTriggered:
    def test_small_drop_stays_open(self):
        """10% drop (< 12% threshold): position must remain open."""
        pos = _make_position(entry_yes_price=0.50, opened_ms_ago=0)
        state.open_positions.append(pos)
        # 10% drop → 0.45, threshold is 0.50 * 0.88 = 0.44
        state.poly_latest[pos.poly_market_id] = _make_pm(pos.poly_market_id, 0.450)

        _manage_positions()

        assert len(state.open_positions) == 1, "position should still be open"
        assert len(state.closed_positions) == 0
        assert state.stop_loss_count == 0

    def test_price_above_entry_stays_open(self):
        """Price above entry (profit territory): no stop loss."""
        pos = _make_position(entry_yes_price=0.50, opened_ms_ago=0)
        state.open_positions.append(pos)
        state.poly_latest[pos.poly_market_id] = _make_pm(pos.poly_market_id, 0.55)

        _manage_positions()

        assert len(state.open_positions) == 1
        assert state.stop_loss_count == 0

    def test_just_above_threshold_stays_open(self):
        """Price just above 12% threshold (11.9% drop): must not trigger."""
        entry = 0.50
        # 0.50 * (1 - 0.119) = 0.4405  >  threshold 0.44
        pos = _make_position(entry_yes_price=entry, opened_ms_ago=0)
        state.open_positions.append(pos)
        state.poly_latest[pos.poly_market_id] = _make_pm(pos.poly_market_id, 0.4405)

        _manage_positions()

        assert len(state.open_positions) == 1
        assert state.stop_loss_count == 0

    def test_no_pm_within_hold_window_stays_open(self):
        """No Polymarket price available and within hold window: stays open (no crash)."""
        pos = _make_position(opened_ms_ago=0)
        state.open_positions.append(pos)
        # intentionally no entry in poly_latest

        _manage_positions()  # must not raise

        assert len(state.open_positions) == 1
        assert len(state.closed_positions) == 0


# ---------------------------------------------------------------------------
# Relative stop-loss triggered
# ---------------------------------------------------------------------------

class TestStopLossTriggered:
    def test_exactly_at_threshold_triggers(self):
        """YES price at exactly 12% below entry triggers stop loss."""
        entry = 0.50
        exit_p = entry * (1.0 - STOP_LOSS_PCT)   # 0.44 exactly
        pos = _make_position(entry_yes_price=entry, opened_ms_ago=0)
        state.open_positions.append(pos)
        state.poly_latest[pos.poly_market_id] = _make_pm(pos.poly_market_id, exit_p)

        _manage_positions()

        assert len(state.open_positions) == 0
        assert len(state.closed_positions) == 1
        closed = state.closed_positions[0]
        assert closed.close_reason == "stop_loss"
        assert closed.exit_yes_price == pytest.approx(exit_p)
        assert closed.pnl is not None
        assert closed.pnl < 0
        assert state.stop_loss_count == 1

    def test_beyond_threshold_triggers(self):
        """15% drop (> 12% threshold) also closes with close_reason='stop_loss'."""
        entry = 0.50
        pos = _make_position(entry_yes_price=entry, opened_ms_ago=0)
        state.open_positions.append(pos)
        state.poly_latest[pos.poly_market_id] = _make_pm(pos.poly_market_id, 0.425)

        _manage_positions()

        assert len(state.closed_positions) == 1
        assert state.closed_positions[0].close_reason == "stop_loss"
        assert state.stop_loss_count == 1

    def test_stop_loss_prevents_hold_window_double_close(self):
        """After stop loss fires (within hold window), position is gone; hold window won't fire."""
        entry = 0.50
        pos = _make_position(entry_yes_price=entry, opened_ms_ago=0)
        state.open_positions.append(pos)
        state.poly_latest[pos.poly_market_id] = _make_pm(pos.poly_market_id, entry * 0.85)

        _manage_positions()

        # exactly one close record
        assert len(state.closed_positions) == 1
        assert len(state.open_positions) == 0

    def test_stop_loss_pnl_is_negative(self):
        """PnL recorded on stop loss close must be negative."""
        entry = 0.50
        exit_p = entry * (1.0 - STOP_LOSS_PCT)
        pos = _make_position(entry_yes_price=entry, quantity=40.0, fees=0.04)
        state.open_positions.append(pos)
        state.poly_latest[pos.poly_market_id] = _make_pm(pos.poly_market_id, exit_p)

        _manage_positions()

        expected_pnl = (exit_p - entry) * 40.0 - 0.04
        assert state.closed_positions[0].pnl == pytest.approx(expected_pnl)

    def test_cash_restored_after_stop_loss(self):
        """Cash is updated correctly: cash += pnl + notional."""
        entry = 0.50
        exit_p = entry * (1.0 - STOP_LOSS_PCT)
        pos = _make_position(entry_yes_price=entry, notional=20.0, quantity=40.0, fees=0.04)
        cash_before_open = state.cash
        state.cash -= (pos.notional + pos.fees)   # simulate deduction at open
        state.open_positions.append(pos)
        state.poly_latest[pos.poly_market_id] = _make_pm(pos.poly_market_id, exit_p)

        _manage_positions()

        expected_pnl = (exit_p - entry) * 40.0 - 0.04
        expected_cash = (cash_before_open - pos.notional - pos.fees) + expected_pnl + pos.notional
        assert state.cash == pytest.approx(expected_cash)

    def test_multiple_positions_only_triggered_one_closes(self):
        """When two positions exist and only one crosses the stop threshold, only that one closes."""
        entry = 0.50
        pos_triggered = _make_position("poly-sol-001", entry_yes_price=entry, opened_ms_ago=0)
        pos_safe = _make_position("poly-btc-001", asset="BTC", entry_yes_price=entry, opened_ms_ago=0)
        state.open_positions.extend([pos_triggered, pos_safe])
        # sol triggers stop, btc stays above threshold
        state.poly_latest["poly-sol-001"] = _make_pm("poly-sol-001", entry * (1.0 - STOP_LOSS_PCT))
        state.poly_latest["poly-btc-001"] = _make_pm("poly-btc-001", 0.48)

        _manage_positions()

        assert len(state.open_positions) == 1
        assert state.open_positions[0].poly_market_id == "poly-btc-001"
        assert len(state.closed_positions) == 1
        assert state.closed_positions[0].close_reason == "stop_loss"


# ---------------------------------------------------------------------------
# exit_yes_price = None (no market price) — must not crash
# ---------------------------------------------------------------------------

class TestNoPriceNoCrash:
    def test_no_pm_hold_window_expired_closes_safely(self):
        """pm=None with expired hold window: close with pnl=None, exit_yes_price=None."""
        hold_ms = int(HOLD_WINDOW_S * 1000)
        pos = _make_position(opened_ms_ago=hold_ms + 5_000)   # well past expiry
        state.open_positions.append(pos)
        # no entry in poly_latest

        _manage_positions()   # must not raise

        assert len(state.closed_positions) == 1
        closed = state.closed_positions[0]
        assert closed.close_reason == "hold_window_expired"
        assert closed.pnl is None
        assert closed.exit_yes_price is None

    def test_no_pm_hold_window_expired_returns_notional(self):
        """With pm=None, cash recovers notional (conservative fallback)."""
        hold_ms = int(HOLD_WINDOW_S * 1000)
        pos = _make_position(opened_ms_ago=hold_ms + 5_000, notional=20.0, fees=0.04)
        state.cash -= (pos.notional + pos.fees)
        cash_after_open = state.cash
        state.open_positions.append(pos)

        _manage_positions()

        assert state.cash == pytest.approx(cash_after_open + pos.notional)
