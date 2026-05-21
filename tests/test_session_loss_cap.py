"""
tests/test_session_loss_cap.py

Unit tests for:
  Blocker 2 — session loss cap (_get_session_equity, _check_session_loss_cap)
  Blocker 3 — safety gate enforcement in main()

No network, no async, no real trading.
"""
from __future__ import annotations

import pytest

import mvp_runner
from mvp_runner import (
    INITIAL_CASH,
    MAX_SESSION_LOSS_PCT,
    PolyMarket,
    Position,
    RunState,
    _check_session_loss_cap,
    _get_session_equity,
    state,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset() -> None:
    """Reset module-level state before each test."""
    import asyncio
    mvp_runner.state.cash = INITIAL_CASH
    mvp_runner.state.open_positions.clear()
    mvp_runner.state.closed_positions.clear()
    mvp_runner.state.signals.clear()
    mvp_runner.state.no_trade_reasons.clear()
    mvp_runner.state.shutdown = asyncio.Event()


def _make_position(
    entry: float,
    notional: float,
    market_id: str = "test-market",
    asset: str = "BTC",
) -> Position:
    import time
    return Position(
        opened_ts_ms=int(time.time() * 1000),
        asset=asset,
        okx_market_id=f"{asset}-USDT",
        poly_market_id=market_id,
        poly_symbol=f"{asset} test",
        entry_yes_price=entry,
        raw_yes_price=entry,
        notional=notional,
        quantity=notional / entry,
        fees=notional * 0.002,
        signal_pct_move=0.002,
    )


def _make_poly(market_id: str, yes_price: float) -> PolyMarket:
    import time
    return PolyMarket(
        market_id=market_id, symbol="test",
        yes_price=yes_price, no_price=round(1.0 - yes_price, 4),
        ts_ms=int(time.time() * 1000),
    )


# ---------------------------------------------------------------------------
# _get_session_equity
# ---------------------------------------------------------------------------

class TestGetSessionEquity:
    def setup_method(self):
        _reset()

    def test_no_positions_equity_equals_cash(self):
        assert _get_session_equity() == pytest.approx(INITIAL_CASH)

    def test_profitable_position_increases_equity(self):
        # Bought at 0.50, now worth 0.60 → gain
        pos = _make_position(entry=0.50, notional=20.0, market_id="m1")
        mvp_runner.state.open_positions.append(pos)
        mvp_runner.state.poly_latest["m1"] = _make_poly("m1", yes_price=0.60)
        mvp_runner.state.cash -= 20.0
        equity = _get_session_equity()
        # cash reduced by 20, but position worth 0.60 * (20/0.50) = 24.0
        assert equity > INITIAL_CASH - 20.0
        assert equity == pytest.approx(INITIAL_CASH - 20.0 + 24.0)

    def test_losing_position_reduces_equity(self):
        pos = _make_position(entry=0.50, notional=20.0, market_id="m2")
        mvp_runner.state.open_positions.append(pos)
        mvp_runner.state.poly_latest["m2"] = _make_poly("m2", yes_price=0.35)
        mvp_runner.state.cash -= 20.0
        equity = _get_session_equity()
        # position worth 0.35 * 40 = 14.0 → loss of 6.0
        assert equity < INITIAL_CASH
        assert equity == pytest.approx(INITIAL_CASH - 20.0 + 14.0)

    def test_no_poly_price_uses_entry_as_fallback(self):
        # No poly_latest entry → falls back to entry_yes_price (conservative)
        pos = _make_position(entry=0.50, notional=20.0, market_id="missing")
        mvp_runner.state.open_positions.append(pos)
        mvp_runner.state.cash -= 20.0
        equity = _get_session_equity()
        # fallback: 0.50 * 40 = 20.0 — same as cash deduction → no change
        assert equity == pytest.approx(INITIAL_CASH)

    def test_multiple_positions(self):
        pos1 = _make_position(entry=0.50, notional=10.0, market_id="p1")
        pos2 = _make_position(entry=0.50, notional=10.0, market_id="p2")
        mvp_runner.state.open_positions.extend([pos1, pos2])
        mvp_runner.state.poly_latest["p1"] = _make_poly("p1", yes_price=0.60)
        mvp_runner.state.poly_latest["p2"] = _make_poly("p2", yes_price=0.40)
        mvp_runner.state.cash -= 20.0
        equity = _get_session_equity()
        # p1: worth 0.60 * 20 = 12, p2: worth 0.40 * 20 = 8, total positions = 20
        assert equity == pytest.approx(INITIAL_CASH)   # net flat


# ---------------------------------------------------------------------------
# _check_session_loss_cap
# ---------------------------------------------------------------------------

class TestCheckSessionLossCap:
    def setup_method(self):
        _reset()

    def test_no_loss_does_not_trigger(self):
        # equity == INITIAL_CASH, no loss → should return False
        assert _check_session_loss_cap() is False
        assert not mvp_runner.state.shutdown.is_set()

    def test_small_loss_does_not_trigger(self):
        # 10% loss — below 20% cap
        mvp_runner.state.cash = INITIAL_CASH * 0.90
        assert _check_session_loss_cap() is False
        assert not mvp_runner.state.shutdown.is_set()

    def test_loss_exactly_at_cap_triggers(self):
        # Exactly 20% loss → should trigger
        mvp_runner.state.cash = INITIAL_CASH * (1.0 - MAX_SESSION_LOSS_PCT)
        triggered = _check_session_loss_cap()
        assert triggered is True
        assert mvp_runner.state.shutdown.is_set()

    def test_loss_above_cap_triggers(self):
        mvp_runner.state.cash = INITIAL_CASH * 0.70   # 30% loss
        assert _check_session_loss_cap() is True
        assert mvp_runner.state.shutdown.is_set()

    def test_total_wipeout_triggers(self):
        mvp_runner.state.cash = 0.0
        assert _check_session_loss_cap() is True
        assert mvp_runner.state.shutdown.is_set()

    def test_unrealized_loss_counted_in_equity(self):
        # Position opened at 0.50 is now worth 0.05 (large drop)
        pos = _make_position(entry=0.50, notional=500.0, market_id="big")
        mvp_runner.state.open_positions.append(pos)
        mvp_runner.state.cash -= 500.0   # cash = 500
        mvp_runner.state.poly_latest["big"] = _make_poly("big", yes_price=0.05)
        # mark: 0.05 * 1000 = 50; equity = 500 + 50 = 550; loss = 450 (45%)
        assert _check_session_loss_cap() is True
        assert mvp_runner.state.shutdown.is_set()

    def test_unrealized_gain_prevents_triggering(self):
        # Even though cash is low, open position is profitable
        pos = _make_position(entry=0.50, notional=500.0, market_id="winner")
        mvp_runner.state.open_positions.append(pos)
        mvp_runner.state.cash -= 500.0   # cash = 500
        mvp_runner.state.poly_latest["winner"] = _make_poly("winner", yes_price=0.80)
        # mark: 0.80 * 1000 = 800; equity = 500 + 800 = 1300 → no loss
        assert _check_session_loss_cap() is False
        assert not mvp_runner.state.shutdown.is_set()

    def test_just_below_cap_does_not_trigger(self):
        # 19.9% loss — just below the 20% cap
        mvp_runner.state.cash = INITIAL_CASH * (1.0 - MAX_SESSION_LOSS_PCT + 0.001)
        assert _check_session_loss_cap() is False
        assert not mvp_runner.state.shutdown.is_set()


# ---------------------------------------------------------------------------
# MAX_SESSION_LOSS_PCT constant
# ---------------------------------------------------------------------------

class TestConstants:
    def test_cap_is_twenty_percent(self):
        assert MAX_SESSION_LOSS_PCT == pytest.approx(0.20)

    def test_initial_cash_positive(self):
        assert INITIAL_CASH > 0


# ---------------------------------------------------------------------------
# Blocker 3 — safety gate
# ---------------------------------------------------------------------------

class TestSafetyGate:
    def test_enforce_phase_one_called_from_main_module(self):
        """
        Verify that main() contains a call to enforce_phase_one via get_settings().
        We inspect the source rather than executing main() (which would start the runner).
        """
        import inspect
        source = inspect.getsource(mvp_runner.main)
        assert "get_settings" in source, (
            "main() must call get_settings() to enforce the safety gate"
        )
        assert "enforce_phase_one" in source, (
            "main() must call enforce_phase_one() as the first startup action"
        )

    def test_enforce_phase_one_raises_on_unsafe_flag(self):
        """SafetyBoundaryError is raised when allow_real_trading=True."""
        from src.safety import SafetyBoundaryError, SafetyFlags
        flags = SafetyFlags(allow_real_trading=True)
        with pytest.raises(SafetyBoundaryError):
            flags.enforce_phase_one()

    def test_enforce_phase_one_passes_with_default_flags(self):
        """Default flags (all False) must not raise."""
        from src.safety import SafetyFlags
        SafetyFlags().enforce_phase_one()   # should not raise

    def test_enforce_phase_one_raises_on_private_keys(self):
        from src.safety import SafetyBoundaryError, SafetyFlags
        with pytest.raises(SafetyBoundaryError):
            SafetyFlags(allow_private_keys=True).enforce_phase_one()

    def test_get_settings_enforces_phase_one_by_default(self, monkeypatch):
        """
        get_settings() (with no unsafe env vars) must not raise —
        confirming the safety gate will pass on a normal launch.
        """
        # Clear the lru_cache so we get a fresh Settings object
        from src.config import get_settings
        get_settings.cache_clear()
        # Remove any env vars that might be set
        for var in ("ALLOW_REAL_TRADING", "ALLOW_PRIVATE_KEYS",
                    "ALLOW_WITHDRAWALS", "ALLOW_BROWSER_AUTOMATION"):
            monkeypatch.delenv(var, raising=False)
        get_settings.cache_clear()
        settings = get_settings()           # must not raise
        assert settings.allow_real_trading is False

    def test_get_settings_raises_when_real_trading_env_set(self, monkeypatch):
        """Setting ALLOW_REAL_TRADING=true must cause get_settings() to raise."""
        from src.config import get_settings
        from src.safety import SafetyBoundaryError
        get_settings.cache_clear()
        monkeypatch.setenv("ALLOW_REAL_TRADING", "true")
        with pytest.raises(SafetyBoundaryError):
            get_settings()
        get_settings.cache_clear()   # clean up for other tests
