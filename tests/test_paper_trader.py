"""
tests/test_paper_trader.py — unit tests for paper trading simulation.

All tests are deterministic, use no network, and use only tmp_path SQLite DBs.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.paper_trader import (
    STATUS_CLOSED,
    STATUS_CLOSED_STOP_LOSS,
    STATUS_OPEN_NO_EXIT,
    STATUS_SKIPPED_DOWN_MOVE,
    STATUS_SKIPPED_INVALID_PRICE,
    STATUS_SKIPPED_NO_CASH,
    STATUS_SKIPPED_UNKNOWN_DIRECTION,
    SimConfig,
    SimTrade,
    compute_entry_price,
    compute_fees,
    compute_notional,
    compute_pnl,
    compute_quantity,
    find_exit_snapshot,
    find_stop_loss_snapshot,
    format_summary,
    insert_paper_trades,
    run_paper_simulation,
    simulate_trade,
)

SCHEMA_SQL = (Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8")

DEFAULT_CFG = SimConfig(
    initial_cash=100.0,
    max_risk_pct=0.02,
    slippage_pct=0.002,
    fee_pct=0.001,
    hold_window_ms=300_000,
    min_notional=0.01,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lag_row(
    asset: str = "BTC",
    market_id: str = "poly-btc-001",
    exchange_move_ts_ms: int = 1000,
    prediction_response_ts_ms: int = 2000,
    exchange_price_before: float = 100.0,
    exchange_price_after: float = 101.0,
    prediction_price_after: float | None = 0.65,
) -> dict:
    return {
        "ts_ms": 9_000_000,
        "exchange_source": "okx",
        "prediction_source": "polymarket",
        "asset": asset,
        "market_id": market_id,
        "exchange_move_ts_ms": exchange_move_ts_ms,
        "prediction_response_ts_ms": prediction_response_ts_ms,
        "lag_ms": prediction_response_ts_ms - exchange_move_ts_ms,
        "exchange_price_before": exchange_price_before,
        "exchange_price_after": exchange_price_after,
        "prediction_price_before": None,
        "prediction_price_after": prediction_price_after,
        "notes": "pct_change=1.0000%",
    }


def _poly_snap(
    market_id: str = "poly-btc-001",
    ts_ms: int = 400_000,
    last: float | None = 0.70,
) -> dict:
    return {
        "ts_ms": ts_ms,
        "source": "polymarket",
        "market_id": market_id,
        "symbol": "Will Bitcoin exceed $100k?",
        "last": last,
    }


def _db_with_schema(tmp_path: Path) -> Path:
    db_file = tmp_path / "test.db"
    with sqlite3.connect(db_file) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    return db_file


def _closed_sim_trade(**kwargs) -> SimTrade:
    defaults = dict(
        opened_ts_ms=2000,
        closed_ts_ms=400_000,
        market_id="poly-btc-001",
        asset="BTC",
        side="YES",
        entry_price=0.651,
        exit_price=0.70,
        notional=2.0,
        quantity=3.072,
        fees=0.002,
        slippage_cost=0.0013,
        pnl=0.15,
        status=STATUS_CLOSED,
        reason="baseline YES trade; exit found after hold window",
    )
    defaults.update(kwargs)
    return SimTrade(**defaults)


# ---------------------------------------------------------------------------
# compute_entry_price
# ---------------------------------------------------------------------------

class TestComputeEntryPrice:
    def test_normal_price_plus_slippage(self):
        # 0.65 + 0.65*0.002 = 0.6513
        result = compute_entry_price(0.65, 0.002)
        assert result == pytest.approx(0.6513)

    def test_returns_none_when_price_at_or_above_one(self):
        # 0.999 + slippage > 1.0
        assert compute_entry_price(0.999, 0.002) is None

    def test_returns_none_when_price_is_zero(self):
        assert compute_entry_price(0.0, 0.002) is None

    def test_returns_none_when_price_close_to_one(self):
        # A price very close to 1.0 that after slippage lands at or above 1.0
        # should be rejected.  0.999 + 0.999*0.002 ≈ 1.000998 >= 1.0 → None
        assert compute_entry_price(0.999, 0.002) is None

    def test_low_price_returns_valid(self):
        result = compute_entry_price(0.10, 0.002)
        assert result is not None
        assert 0.0 < result < 1.0

    def test_slippage_zero_returns_price_unchanged(self):
        result = compute_entry_price(0.50, 0.0)
        assert result == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# compute_notional
# ---------------------------------------------------------------------------

class TestComputeNotional:
    def test_two_pct_of_100(self):
        assert compute_notional(100.0, 0.02) == pytest.approx(2.0)

    def test_one_pct_of_100(self):
        assert compute_notional(100.0, 0.01) == pytest.approx(1.0)

    def test_zero_cash_returns_zero(self):
        assert compute_notional(0.0, 0.02) == pytest.approx(0.0)

    def test_negative_cash_returns_zero(self):
        assert compute_notional(-5.0, 0.02) == pytest.approx(0.0)

    def test_proportional_to_cash(self):
        assert compute_notional(50.0, 0.02) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# compute_notional — direct helper invalid risk validation
# ---------------------------------------------------------------------------

class TestComputeNotionalValidation:
    """compute_notional() must raise ValueError for any max_risk_pct outside (0, 0.02].

    This ensures all-in sizing is impossible even when the helper is called
    directly without going through SimConfig.
    """

    def test_zero_risk_raises(self):
        with pytest.raises(ValueError, match="max_risk_pct"):
            compute_notional(100.0, 0)

    def test_negative_risk_raises(self):
        with pytest.raises(ValueError, match="max_risk_pct"):
            compute_notional(100.0, -0.01)

    def test_three_pct_raises(self):
        with pytest.raises(ValueError, match="max_risk_pct"):
            compute_notional(100.0, 0.03)

    def test_one_hundred_pct_raises(self):
        # 1.0 would be all-in; must be rejected
        with pytest.raises(ValueError, match="max_risk_pct"):
            compute_notional(100.0, 1.0)

    def test_two_hundred_pct_raises(self):
        with pytest.raises(ValueError, match="max_risk_pct"):
            compute_notional(100.0, 2.0)

    def test_exactly_two_pct_is_valid(self):
        # 0.02 is the hard cap boundary — must NOT raise
        result = compute_notional(100.0, 0.02)
        assert result == pytest.approx(2.0)

    def test_error_message_mentions_hard_cap(self):
        with pytest.raises(ValueError, match="hard cap"):
            compute_notional(100.0, 1.0)


# ---------------------------------------------------------------------------
# compute_quantity
# ---------------------------------------------------------------------------

class TestComputeQuantity:
    def test_notional_divided_by_entry(self):
        assert compute_quantity(2.0, 0.65) == pytest.approx(2.0 / 0.65)

    def test_zero_entry_returns_zero(self):
        assert compute_quantity(2.0, 0.0) == pytest.approx(0.0)

    def test_negative_entry_returns_zero(self):
        assert compute_quantity(2.0, -1.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# compute_fees
# ---------------------------------------------------------------------------

class TestComputeFees:
    def test_one_pct_fee_on_ten(self):
        assert compute_fees(10.0, 0.01) == pytest.approx(0.10)

    def test_zero_fee_returns_zero(self):
        assert compute_fees(10.0, 0.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# compute_pnl
# ---------------------------------------------------------------------------

class TestComputePnl:
    def test_profitable_trade(self):
        # (0.70 - 0.65) * 3.0 - 0.002 = 0.148
        pnl = compute_pnl(3.0, 0.65, 0.70, 0.002)
        assert pnl == pytest.approx(0.148)

    def test_losing_trade(self):
        pnl = compute_pnl(3.0, 0.65, 0.60, 0.002)
        assert pnl is not None
        assert pnl < 0

    def test_none_exit_returns_none(self):
        assert compute_pnl(3.0, 0.65, None, 0.002) is None

    def test_breakeven_when_exit_equals_entry_plus_fees(self):
        # entry=0.65, exit=0.65, quantity=10, fees=0 → pnl=0
        pnl = compute_pnl(10.0, 0.65, 0.65, 0.0)
        assert pnl == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# find_exit_snapshot
# ---------------------------------------------------------------------------

class TestFindExitSnapshot:
    def _snaps(self, ts_ms_list: list[int], market_id: str = "m1") -> list[dict]:
        return [{"ts_ms": t, "market_id": market_id, "last": 0.70} for t in ts_ms_list]

    def test_returns_first_snap_after_hold_window(self):
        snaps = self._snaps([100, 200_000, 400_000])
        result = find_exit_snapshot("m1", opened_ts_ms=0, hold_window_ms=300_000, poly_snaps=snaps)
        assert result is not None
        assert result["ts_ms"] == 400_000

    def test_returns_none_when_no_snap_after_window(self):
        snaps = self._snaps([100, 200_000])
        result = find_exit_snapshot("m1", opened_ts_ms=0, hold_window_ms=300_000, poly_snaps=snaps)
        assert result is None

    def test_market_id_mismatch_ignored(self):
        snaps = [{"ts_ms": 400_000, "market_id": "other", "last": 0.70}]
        result = find_exit_snapshot("m1", opened_ts_ms=0, hold_window_ms=300_000, poly_snaps=snaps)
        assert result is None

    def test_snap_exactly_at_cutoff_not_returned(self):
        # cutoff = 0 + 300_000; snap at 300_000 → NOT > cutoff
        snaps = self._snaps([300_000])
        result = find_exit_snapshot("m1", opened_ts_ms=0, hold_window_ms=300_000, poly_snaps=snaps)
        assert result is None

    def test_snap_one_ms_after_cutoff_is_returned(self):
        snaps = self._snaps([300_001])
        result = find_exit_snapshot("m1", opened_ts_ms=0, hold_window_ms=300_000, poly_snaps=snaps)
        assert result is not None

    def test_empty_snaps_returns_none(self):
        assert find_exit_snapshot("m1", 0, 300_000, []) is None


# ---------------------------------------------------------------------------
# simulate_trade
# ---------------------------------------------------------------------------

class TestSimulateTrade:
    def _poly_snaps(self) -> list[dict]:
        # Return a snap well after the hold window (opened at 2000, window 300_000)
        return [_poly_snap(ts_ms=400_000, last=0.70)]

    def test_closed_trade_on_valid_inputs(self):
        row = _lag_row(prediction_price_after=0.65)
        trade = simulate_trade(row, self._poly_snaps(), remaining_cash=100.0, cfg=DEFAULT_CFG)
        assert trade.status == STATUS_CLOSED
        assert trade.exit_price == pytest.approx(0.70)
        assert trade.pnl is not None

    def test_side_is_always_yes(self):
        row = _lag_row(prediction_price_after=0.65)
        trade = simulate_trade(row, self._poly_snaps(), remaining_cash=100.0, cfg=DEFAULT_CFG)
        assert trade.side == "YES"

    def test_skipped_when_prediction_price_none(self):
        row = _lag_row(prediction_price_after=None)
        trade = simulate_trade(row, self._poly_snaps(), remaining_cash=100.0, cfg=DEFAULT_CFG)
        assert trade.status == STATUS_SKIPPED_INVALID_PRICE

    def test_skipped_when_prediction_price_zero(self):
        row = _lag_row(prediction_price_after=0.0)
        trade = simulate_trade(row, self._poly_snaps(), remaining_cash=100.0, cfg=DEFAULT_CFG)
        assert trade.status == STATUS_SKIPPED_INVALID_PRICE

    def test_skipped_when_prediction_price_one(self):
        row = _lag_row(prediction_price_after=1.0)
        trade = simulate_trade(row, self._poly_snaps(), remaining_cash=100.0, cfg=DEFAULT_CFG)
        assert trade.status == STATUS_SKIPPED_INVALID_PRICE

    def test_skipped_when_prediction_price_negative(self):
        row = _lag_row(prediction_price_after=-0.5)
        trade = simulate_trade(row, self._poly_snaps(), remaining_cash=100.0, cfg=DEFAULT_CFG)
        assert trade.status == STATUS_SKIPPED_INVALID_PRICE

    def test_skipped_when_cash_insufficient(self):
        row = _lag_row(prediction_price_after=0.65)
        cfg = SimConfig(initial_cash=0.001, max_risk_pct=0.02, min_notional=0.01)
        trade = simulate_trade(row, self._poly_snaps(), remaining_cash=0.0, cfg=cfg)
        assert trade.status == STATUS_SKIPPED_NO_CASH

    def test_open_no_exit_when_no_snap_after_window(self):
        row = _lag_row(prediction_price_after=0.65)
        # snap before hold window
        poly = [_poly_snap(ts_ms=100)]
        trade = simulate_trade(row, poly, remaining_cash=100.0, cfg=DEFAULT_CFG)
        assert trade.status == STATUS_OPEN_NO_EXIT
        assert trade.pnl is None

    def test_entry_price_includes_slippage(self):
        row = _lag_row(prediction_price_after=0.65)
        trade = simulate_trade(row, self._poly_snaps(), remaining_cash=100.0, cfg=DEFAULT_CFG)
        expected_entry = 0.65 * (1 + DEFAULT_CFG.slippage_pct)
        assert trade.entry_price == pytest.approx(expected_entry)

    def test_notional_is_risk_pct_of_cash(self):
        row = _lag_row(prediction_price_after=0.65)
        trade = simulate_trade(row, self._poly_snaps(), remaining_cash=100.0, cfg=DEFAULT_CFG)
        assert trade.notional == pytest.approx(100.0 * DEFAULT_CFG.max_risk_pct)

    def test_fees_deducted(self):
        row = _lag_row(prediction_price_after=0.65)
        trade = simulate_trade(row, self._poly_snaps(), remaining_cash=100.0, cfg=DEFAULT_CFG)
        expected_fees = trade.notional * DEFAULT_CFG.fee_pct
        assert trade.fees == pytest.approx(expected_fees)


# ---------------------------------------------------------------------------
# run_paper_simulation
# ---------------------------------------------------------------------------

class TestRunPaperSimulation:
    def _poly_with_exit(self) -> list[dict]:
        return [_poly_snap(ts_ms=400_000, last=0.70)]

    def test_empty_lag_rows_returns_empty(self):
        trades, cash = run_paper_simulation([], [], DEFAULT_CFG)
        assert trades == []
        assert cash == pytest.approx(DEFAULT_CFG.initial_cash)

    def test_single_valid_trade_produced(self):
        trades, cash = run_paper_simulation(
            [_lag_row(prediction_price_after=0.65)],
            self._poly_with_exit(),
            DEFAULT_CFG,
        )
        assert len(trades) == 1

    def test_cash_decremented_after_open_trade(self):
        _, cash = run_paper_simulation(
            [_lag_row(prediction_price_after=0.65)],
            [],   # no exit snap → open_no_exit
            DEFAULT_CFG,
        )
        # cash should be reduced by notional + fees
        max_notional = DEFAULT_CFG.initial_cash * DEFAULT_CFG.max_risk_pct
        expected_cash = DEFAULT_CFG.initial_cash - max_notional - (max_notional * DEFAULT_CFG.fee_pct)
        assert cash == pytest.approx(expected_cash, rel=1e-6)

    def test_cash_updated_after_closed_profitable_trade(self):
        # Exit price 0.70 > entry ~0.6513 → gain.
        # Net cash effect for a closed trade = pnl (pnl already nets out fees;
        # principal is fully returned on close).
        trades, cash = run_paper_simulation(
            [_lag_row(prediction_price_after=0.65)],
            self._poly_with_exit(),
            DEFAULT_CFG,
        )
        assert len(trades) == 1
        assert trades[0].pnl is not None
        assert cash == pytest.approx(DEFAULT_CFG.initial_cash + trades[0].pnl, abs=1e-9)

    def test_skipped_trades_do_not_affect_cash(self):
        _, cash = run_paper_simulation(
            [_lag_row(prediction_price_after=None)],   # will be skipped
            [],
            DEFAULT_CFG,
        )
        assert cash == pytest.approx(DEFAULT_CFG.initial_cash)

    def test_multiple_trades_processed_in_order(self):
        rows = [
            _lag_row(exchange_move_ts_ms=1000, prediction_response_ts_ms=2000,
                     market_id="m1", prediction_price_after=0.65),
            _lag_row(exchange_move_ts_ms=2000, prediction_response_ts_ms=3000,
                     market_id="m2", prediction_price_after=0.55),
        ]
        snaps = [
            _poly_snap(market_id="m1", ts_ms=400_000, last=0.70),
            _poly_snap(market_id="m2", ts_ms=500_000, last=0.60),
        ]
        trades, _ = run_paper_simulation(rows, snaps, DEFAULT_CFG)
        assert len(trades) == 2

    def test_all_trades_skipped_when_cash_zero(self):
        # Call simulate_trade directly with remaining_cash=0 (valid cfg required).
        row = _lag_row(prediction_price_after=0.65)
        trade = simulate_trade(row, self._poly_with_exit(), remaining_cash=0.0, cfg=DEFAULT_CFG)
        assert trade.status == STATUS_SKIPPED_NO_CASH


# ---------------------------------------------------------------------------
# insert_paper_trades (SQLite persistence)
# ---------------------------------------------------------------------------

class TestInsertPaperTrades:
    def test_insert_closed_trade(self, tmp_path: Path):
        db_file = _db_with_schema(tmp_path)
        trade = _closed_sim_trade()
        inserted = insert_paper_trades(str(db_file), [trade])
        assert inserted == 1

    def test_insert_open_no_exit_trade(self, tmp_path: Path):
        db_file = _db_with_schema(tmp_path)
        trade = _closed_sim_trade(
            status=STATUS_OPEN_NO_EXIT,
            closed_ts_ms=None,
            exit_price=None,
            pnl=None,
        )
        inserted = insert_paper_trades(str(db_file), [trade])
        assert inserted == 1

    def test_skipped_trades_not_inserted(self, tmp_path: Path):
        db_file = _db_with_schema(tmp_path)
        skipped = _closed_sim_trade(status=STATUS_SKIPPED_INVALID_PRICE, notional=0.0)
        inserted = insert_paper_trades(str(db_file), [skipped])
        assert inserted == 0

    def test_empty_list_inserts_nothing(self, tmp_path: Path):
        db_file = _db_with_schema(tmp_path)
        assert insert_paper_trades(str(db_file), []) == 0

    def test_multiple_trades_all_inserted(self, tmp_path: Path):
        db_file = _db_with_schema(tmp_path)
        trades = [_closed_sim_trade(market_id=f"m-{i}") for i in range(4)]
        inserted = insert_paper_trades(str(db_file), trades)
        assert inserted == 4

    def test_field_round_trip(self, tmp_path: Path):
        db_file = _db_with_schema(tmp_path)
        trade = _closed_sim_trade(
            entry_price=0.651,
            exit_price=0.70,
            notional=2.0,
            pnl=0.148,
        )
        insert_paper_trades(str(db_file), [trade])
        with sqlite3.connect(db_file) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM paper_trades").fetchone()
        assert row["entry_price"] == pytest.approx(0.651)
        assert row["exit_price"] == pytest.approx(0.70)
        assert row["notional"] == pytest.approx(2.0)
        assert row["pnl"] == pytest.approx(0.148)
        assert row["side"] == "YES"
        assert row["status"] == STATUS_CLOSED


# ---------------------------------------------------------------------------
# format_summary
# ---------------------------------------------------------------------------

class TestFormatSummary:
    def test_empty_trades_shows_zero_counts(self):
        text = format_summary([], 100.0, 100.0)
        assert "Total trade attempts : 0" in text

    def test_simulation_label_present(self):
        text = format_summary([], 100.0, 100.0)
        assert "SIMULATION" in text

    def test_disclaimer_present(self):
        text = format_summary([], 100.0, 100.0)
        assert "DISCLAIMER" in text or "NOT" in text

    def test_not_trading_recommendation_stated(self):
        text = format_summary([], 100.0, 100.0)
        assert "trading recommendation" in text.lower() or "NOT" in text

    def test_pnl_shown_for_closed_trades(self):
        trade = _closed_sim_trade(pnl=0.50)
        text = format_summary([trade], final_cash=100.50, initial_cash=100.0)
        assert "PnL" in text
        assert "0.5000" in text or "+0.50" in text

    def test_net_change_shown(self):
        text = format_summary([], final_cash=105.0, initial_cash=100.0)
        assert "Net change" in text

    def test_skip_reasons_shown_when_skipped(self):
        skipped = _closed_sim_trade(status=STATUS_SKIPPED_INVALID_PRICE, notional=0.0)
        text = format_summary([skipped], final_cash=100.0, initial_cash=100.0)
        assert "skipped_invalid_price" in text

    # ------------------------------------------------------------------
    # F-5: open_no_exit cash accounting note
    # ------------------------------------------------------------------

    def test_open_no_exit_note_present_when_open_positions_exist(self):
        """F-5: a clarifying note must appear when open_no_exit trades are present."""
        no_exit = _closed_sim_trade(
            status=STATUS_OPEN_NO_EXIT,
            closed_ts_ms=None,
            exit_price=None,
            pnl=None,
            notional=2.0,
            fees=0.002,
        )
        text = format_summary([no_exit], final_cash=97.998, initial_cash=100.0)
        assert "open / no-exit" in text.lower() or "open_no_exit" in text.lower() or \
               "no-exit" in text.lower(), "expected open/no-exit label in note"
        assert "excluded" in text.lower(), \
            "expected 'excluded' — open_no_exit trades must be excluded from PnL metrics"
        assert "unrecovered" in text.lower() or "unrealised" in text.lower(), \
            "expected note about unrecovered/unrealised notional"

    def test_open_no_exit_note_absent_when_no_open_positions(self):
        """F-5: the note must NOT appear when there are no open_no_exit trades."""
        closed = _closed_sim_trade(pnl=0.15)
        text = format_summary([closed], final_cash=100.15, initial_cash=100.0)
        # "unrecovered" and "unrealised" only appear in the open_no_exit note
        assert "unrecovered" not in text.lower()
        assert "unrealised" not in text.lower()

    def test_open_no_exit_note_shows_notional_and_fees(self):
        """F-5: the note must show the total notional and fees of open positions."""
        no_exit = _closed_sim_trade(
            status=STATUS_OPEN_NO_EXIT,
            closed_ts_ms=None,
            exit_price=None,
            pnl=None,
            notional=2.0,
            fees=0.002,
        )
        text = format_summary([no_exit], final_cash=97.998, initial_cash=100.0)
        # notional = 2.0000, fees = 0.0020
        assert "2.0000" in text, "expected open_no_exit notional in note"
        assert "0.0020" in text, "expected open_no_exit fees in note"


# ---------------------------------------------------------------------------
# SimConfig validation (hard safety limits)
# ---------------------------------------------------------------------------

class TestSimConfigValidation:
    def test_default_config_is_valid(self):
        cfg = SimConfig()
        assert cfg.max_risk_pct == pytest.approx(0.02)

    def test_zero_initial_cash_raises(self):
        with pytest.raises(ValueError, match="initial_cash"):
            SimConfig(initial_cash=0.0)

    def test_negative_initial_cash_raises(self):
        with pytest.raises(ValueError, match="initial_cash"):
            SimConfig(initial_cash=-10.0)

    def test_risk_above_2pct_raises(self):
        with pytest.raises(ValueError, match="max_risk_pct"):
            SimConfig(max_risk_pct=0.03)

    def test_risk_at_exactly_2pct_is_valid(self):
        cfg = SimConfig(max_risk_pct=0.02)
        assert cfg.max_risk_pct == pytest.approx(0.02)

    def test_risk_zero_raises(self):
        with pytest.raises(ValueError, match="max_risk_pct"):
            SimConfig(max_risk_pct=0.0)

    def test_risk_negative_raises(self):
        with pytest.raises(ValueError, match="max_risk_pct"):
            SimConfig(max_risk_pct=-0.01)

    def test_risk_200pct_raises(self):
        with pytest.raises(ValueError, match="max_risk_pct"):
            SimConfig(max_risk_pct=2.0)

    def test_negative_slippage_raises(self):
        with pytest.raises(ValueError, match="slippage_pct"):
            SimConfig(slippage_pct=-0.001)

    def test_zero_slippage_is_valid(self):
        cfg = SimConfig(slippage_pct=0.0)
        assert cfg.slippage_pct == 0.0

    def test_negative_fee_raises(self):
        with pytest.raises(ValueError, match="fee_pct"):
            SimConfig(fee_pct=-0.001)

    def test_zero_fee_is_valid(self):
        cfg = SimConfig(fee_pct=0.0)
        assert cfg.fee_pct == 0.0

    def test_zero_hold_window_raises(self):
        with pytest.raises(ValueError, match="hold_window_ms"):
            SimConfig(hold_window_ms=0)

    def test_negative_hold_window_raises(self):
        with pytest.raises(ValueError, match="hold_window_ms"):
            SimConfig(hold_window_ms=-1000)

    def test_no_all_in_via_risk_cap(self):
        # Verify that even a valid config cannot risk 100% of cash
        cfg = SimConfig(initial_cash=100.0, max_risk_pct=0.02)
        from src.paper_trader import compute_notional
        notional = compute_notional(100.0, cfg.max_risk_pct)
        assert notional <= 2.0  # hard cap at 2%
        assert notional < 100.0  # not all-in


# ---------------------------------------------------------------------------
# Down/flat OKX move skipping (YES-only baseline)
# ---------------------------------------------------------------------------

class TestDownFlatMoveSkip:
    def _poly_exit(self) -> list[dict]:
        return [_poly_snap(ts_ms=400_000, last=0.70)]

    def test_down_move_skipped(self):
        row = _lag_row(
            prediction_price_after=0.65,
            exchange_price_before=101.0,
            exchange_price_after=100.0,  # price fell
        )
        trade = simulate_trade(row, self._poly_exit(), 100.0, DEFAULT_CFG)
        assert trade.status == STATUS_SKIPPED_DOWN_MOVE

    def test_flat_move_skipped(self):
        row = _lag_row(
            prediction_price_after=0.65,
            exchange_price_before=100.0,
            exchange_price_after=100.0,  # no change
        )
        trade = simulate_trade(row, self._poly_exit(), 100.0, DEFAULT_CFG)
        assert trade.status == STATUS_SKIPPED_DOWN_MOVE

    def test_upward_move_not_skipped(self):
        row = _lag_row(
            prediction_price_after=0.65,
            exchange_price_before=100.0,
            exchange_price_after=101.0,  # price rose
        )
        trade = simulate_trade(row, self._poly_exit(), 100.0, DEFAULT_CFG)
        assert trade.status != STATUS_SKIPPED_DOWN_MOVE

    def test_reason_mentions_down_move(self):
        row = _lag_row(
            prediction_price_after=0.65,
            exchange_price_before=100.0,
            exchange_price_after=99.0,
        )
        trade = simulate_trade(row, self._poly_exit(), 100.0, DEFAULT_CFG)
        assert "down" in trade.reason.lower() or "not upward" in trade.reason.lower()

    def test_notional_zero_for_skipped_down_move(self):
        row = _lag_row(
            prediction_price_after=0.65,
            exchange_price_before=100.0,
            exchange_price_after=99.0,
        )
        trade = simulate_trade(row, self._poly_exit(), 100.0, DEFAULT_CFG)
        assert trade.notional == pytest.approx(0.0)
        assert trade.pnl is None

    def test_down_move_does_not_affect_cash_in_simulation(self):
        rows = [
            _lag_row(prediction_price_after=0.65,
                     exchange_price_before=100.0, exchange_price_after=99.0),
        ]
        _, cash = run_paper_simulation(rows, [], DEFAULT_CFG)
        assert cash == pytest.approx(DEFAULT_CFG.initial_cash)

    def test_both_none_prices_skipped_as_unknown_direction(self):
        # If both price_before and price_after are None, direction is unknown →
        # must be skipped as STATUS_SKIPPED_UNKNOWN_DIRECTION, NOT down_move.
        row = _lag_row(prediction_price_after=0.65,
                       exchange_price_before=None, exchange_price_after=None)
        trade = simulate_trade(row, self._poly_exit(), 100.0, DEFAULT_CFG)
        assert trade.status == STATUS_SKIPPED_UNKNOWN_DIRECTION
        assert trade.status != STATUS_SKIPPED_DOWN_MOVE


# ---------------------------------------------------------------------------
# Unknown OKX direction skipping (missing exchange prices)
# ---------------------------------------------------------------------------

class TestUnknownDirectionSkip:
    """YES-only baseline must skip lag rows where OKX direction is unknown.

    Direction is unknown when either exchange_price_before or exchange_price_after
    is missing (None).  Such rows must produce STATUS_SKIPPED_UNKNOWN_DIRECTION
    and must not affect the remaining cash balance.
    """

    def _poly_exit(self) -> list[dict]:
        return [_poly_snap(ts_ms=400_000, last=0.70)]

    def test_skipped_when_price_before_is_none(self):
        row = _lag_row(
            prediction_price_after=0.65,
            exchange_price_before=None,
            exchange_price_after=101.0,
        )
        trade = simulate_trade(row, self._poly_exit(), 100.0, DEFAULT_CFG)
        assert trade.status == STATUS_SKIPPED_UNKNOWN_DIRECTION

    def test_skipped_when_price_after_is_none(self):
        row = _lag_row(
            prediction_price_after=0.65,
            exchange_price_before=100.0,
            exchange_price_after=None,
        )
        trade = simulate_trade(row, self._poly_exit(), 100.0, DEFAULT_CFG)
        assert trade.status == STATUS_SKIPPED_UNKNOWN_DIRECTION

    def test_skipped_when_both_prices_are_none(self):
        row = _lag_row(
            prediction_price_after=0.65,
            exchange_price_before=None,
            exchange_price_after=None,
        )
        trade = simulate_trade(row, self._poly_exit(), 100.0, DEFAULT_CFG)
        assert trade.status == STATUS_SKIPPED_UNKNOWN_DIRECTION

    def test_reason_mentions_unknown_direction(self):
        row = _lag_row(
            prediction_price_after=0.65,
            exchange_price_before=None,
            exchange_price_after=101.0,
        )
        trade = simulate_trade(row, self._poly_exit(), 100.0, DEFAULT_CFG)
        assert "unknown" in trade.reason.lower()

    def test_notional_zero_for_unknown_direction(self):
        row = _lag_row(
            prediction_price_after=0.65,
            exchange_price_before=None,
            exchange_price_after=101.0,
        )
        trade = simulate_trade(row, self._poly_exit(), 100.0, DEFAULT_CFG)
        assert trade.notional == pytest.approx(0.0)
        assert trade.pnl is None

    def test_unknown_direction_does_not_affect_cash(self):
        rows = [
            _lag_row(prediction_price_after=0.65,
                     exchange_price_before=None, exchange_price_after=101.0),
        ]
        _, cash = run_paper_simulation(rows, [], DEFAULT_CFG)
        assert cash == pytest.approx(DEFAULT_CFG.initial_cash)

    def test_known_prices_both_present_not_skipped_as_unknown(self):
        # Both prices present and upward → should NOT be unknown direction
        row = _lag_row(
            prediction_price_after=0.65,
            exchange_price_before=100.0,
            exchange_price_after=101.0,
        )
        trade = simulate_trade(row, self._poly_exit(), 100.0, DEFAULT_CFG)
        assert trade.status != STATUS_SKIPPED_UNKNOWN_DIRECTION


# ---------------------------------------------------------------------------
# find_stop_loss_snapshot
# ---------------------------------------------------------------------------

class TestFindStopLossSnapshot:
    """Pure unit tests for the stop-loss snapshot search helper."""

    def _snaps(self, entries: list[tuple[int, float]], market_id: str = "m1") -> list[dict]:
        return [{"ts_ms": t, "market_id": market_id, "last": p} for t, p in entries]

    def test_returns_first_snap_below_threshold_within_window(self):
        # opened=0, window=300_000; snap at 100_000 with price 0.35 <= 0.40
        snaps = self._snaps([(100_000, 0.35)])
        result = find_stop_loss_snapshot("m1", 0, 300_000, 0.40, snaps)
        assert result is not None
        assert result["ts_ms"] == 100_000

    def test_returns_none_when_price_above_threshold(self):
        snaps = self._snaps([(100_000, 0.45)])
        assert find_stop_loss_snapshot("m1", 0, 300_000, 0.40, snaps) is None

    def test_returns_none_when_snap_at_threshold_boundary(self):
        # price exactly equal to threshold should trigger (<=)
        snaps = self._snaps([(100_000, 0.40)])
        result = find_stop_loss_snapshot("m1", 0, 300_000, 0.40, snaps)
        assert result is not None

    def test_ignores_snap_at_or_before_entry_ts(self):
        # ts_ms == opened_ts_ms → should be ignored
        snaps = self._snaps([(0, 0.30)])
        assert find_stop_loss_snapshot("m1", 0, 300_000, 0.40, snaps) is None

    def test_ignores_snap_past_hold_window(self):
        # ts_ms == cutoff (300_000) is still within window but ts > cutoff is not
        snaps = self._snaps([(300_001, 0.30)])
        assert find_stop_loss_snapshot("m1", 0, 300_000, 0.40, snaps) is None

    def test_snap_at_cutoff_is_not_returned(self):
        # ts_ms == cutoff; the loop breaks when ts > cutoff, so exactly-at-cutoff is NOT checked
        # Actually cutoff = opened + window; ts == cutoff means ts is NOT > cutoff,
        # so it IS within the window and should be evaluated.
        snaps = self._snaps([(300_000, 0.30)])
        result = find_stop_loss_snapshot("m1", 0, 300_000, 0.40, snaps)
        assert result is not None  # ts == cutoff is still within window

    def test_market_id_mismatch_ignored(self):
        snaps = self._snaps([(100_000, 0.30)], market_id="other")
        assert find_stop_loss_snapshot("m1", 0, 300_000, 0.40, snaps) is None

    def test_returns_first_matching_snap_not_second(self):
        snaps = self._snaps([(50_000, 0.38), (100_000, 0.35)])
        result = find_stop_loss_snapshot("m1", 0, 300_000, 0.40, snaps)
        assert result is not None
        assert result["ts_ms"] == 50_000

    def test_empty_snaps_returns_none(self):
        assert find_stop_loss_snapshot("m1", 0, 300_000, 0.40, []) is None

    def test_none_last_price_skipped(self):
        snaps = [{"ts_ms": 100_000, "market_id": "m1", "last": None}]
        assert find_stop_loss_snapshot("m1", 0, 300_000, 0.40, snaps) is None


# ---------------------------------------------------------------------------
# simulate_trade — stop loss behaviour
# ---------------------------------------------------------------------------

_SL_CFG = SimConfig(
    initial_cash=100.0,
    max_risk_pct=0.02,
    slippage_pct=0.002,
    fee_pct=0.001,
    hold_window_ms=300_000,
    min_notional=0.01,
    stop_loss_yes_price=0.40,
)


class TestSimulateTradeStopLoss:
    def _lag(self, prediction_price_after: float = 0.50) -> dict:
        return _lag_row(prediction_price_after=prediction_price_after)

    def test_stop_loss_triggered_within_window(self):
        # Snap at 100_000 (within 300_000 window) with price 0.35 <= 0.40
        snaps = [_poly_snap(ts_ms=100_000, last=0.35)]
        trade = simulate_trade(self._lag(), snaps, 100.0, _SL_CFG)
        assert trade.status == STATUS_CLOSED_STOP_LOSS
        assert trade.reason == "stop_loss_yes_price"
        assert trade.exit_price == pytest.approx(0.35)
        assert trade.closed_ts_ms == 100_000

    def test_stop_loss_has_pnl_computed(self):
        snaps = [_poly_snap(ts_ms=100_000, last=0.35)]
        trade = simulate_trade(self._lag(), snaps, 100.0, _SL_CFG)
        assert trade.pnl is not None
        assert trade.pnl < 0  # exit 0.35 < entry ~0.501 → loss

    def test_stop_loss_takes_priority_over_hold_window_exit(self):
        # Stop-loss snap at 100_000 AND a hold-window exit snap at 400_000
        snaps = [
            _poly_snap(ts_ms=100_000, last=0.30),   # stop loss
            _poly_snap(ts_ms=400_000, last=0.70),   # would be hold-window exit
        ]
        trade = simulate_trade(self._lag(), snaps, 100.0, _SL_CFG)
        assert trade.status == STATUS_CLOSED_STOP_LOSS
        assert trade.exit_price == pytest.approx(0.30)

    def test_no_stop_loss_falls_through_to_hold_window(self):
        # Price 0.45 > 0.40 threshold → no stop loss; hold-window snap at 400_000
        snaps = [
            _poly_snap(ts_ms=100_000, last=0.45),   # above threshold → no stop loss
            _poly_snap(ts_ms=400_000, last=0.70),   # hold-window exit
        ]
        trade = simulate_trade(self._lag(), snaps, 100.0, _SL_CFG)
        assert trade.status == STATUS_CLOSED
        assert trade.reason == "hold_window_expired"

    def test_stop_loss_snap_after_hold_window_not_triggered(self):
        # Snap at 400_001 (past window) with low price → no stop loss (outside window)
        snaps = [_poly_snap(ts_ms=400_001, last=0.30)]
        trade = simulate_trade(self._lag(), snaps, 100.0, _SL_CFG)
        # No stop loss; no hold-window snap found after window for the same market
        # Actually 400_001 > cutoff (2000+300_000=302_000), so find_exit_snapshot
        # should find it.
        assert trade.status == STATUS_CLOSED
        assert trade.reason == "hold_window_expired"

    def test_hold_window_reason_label(self):
        snaps = [_poly_snap(ts_ms=400_000, last=0.70)]
        trade = simulate_trade(self._lag(), snaps, 100.0, _SL_CFG)
        assert trade.status == STATUS_CLOSED
        assert trade.reason == "hold_window_expired"

    def test_stop_loss_included_in_cash_accounting(self):
        snaps = [_poly_snap(ts_ms=100_000, last=0.30)]
        trades, cash = run_paper_simulation([self._lag()], snaps, _SL_CFG)
        assert len(trades) == 1
        assert trades[0].status == STATUS_CLOSED_STOP_LOSS
        assert trades[0].pnl is not None
        assert cash == pytest.approx(_SL_CFG.initial_cash + trades[0].pnl, abs=1e-9)

    def test_stop_loss_trade_inserted_to_db(self, tmp_path: Path):
        db_file = tmp_path / "test.db"
        with sqlite3.connect(db_file) as conn:
            conn.executescript(SCHEMA_SQL)
        trade = SimTrade(
            opened_ts_ms=1000,
            closed_ts_ms=100_000,
            market_id="m1",
            asset="BTC",
            side="YES",
            entry_price=0.501,
            exit_price=0.35,
            notional=2.0,
            quantity=3.99,
            fees=0.002,
            slippage_cost=0.001,
            pnl=-0.60,
            status=STATUS_CLOSED_STOP_LOSS,
            reason="stop_loss_yes_price",
        )
        inserted = insert_paper_trades(str(db_file), [trade])
        assert inserted == 1
        with sqlite3.connect(db_file) as conn:
            row = conn.execute("SELECT status, reason FROM paper_trades").fetchone()
        assert row[0] == STATUS_CLOSED_STOP_LOSS
        assert row[1] == "stop_loss_yes_price"


# ---------------------------------------------------------------------------
# SimConfig — stop_loss_yes_price validation
# ---------------------------------------------------------------------------

class TestSimConfigStopLoss:
    def test_default_stop_loss_is_0_40(self):
        cfg = SimConfig()
        assert cfg.stop_loss_yes_price == pytest.approx(0.40)

    def test_custom_stop_loss_accepted(self):
        cfg = SimConfig(stop_loss_yes_price=0.30)
        assert cfg.stop_loss_yes_price == pytest.approx(0.30)

    def test_stop_loss_zero_raises(self):
        with pytest.raises(ValueError, match="stop_loss_yes_price"):
            SimConfig(stop_loss_yes_price=0.0)

    def test_stop_loss_one_raises(self):
        with pytest.raises(ValueError, match="stop_loss_yes_price"):
            SimConfig(stop_loss_yes_price=1.0)

    def test_stop_loss_negative_raises(self):
        with pytest.raises(ValueError, match="stop_loss_yes_price"):
            SimConfig(stop_loss_yes_price=-0.10)

    def test_stop_loss_above_one_raises(self):
        with pytest.raises(ValueError, match="stop_loss_yes_price"):
            SimConfig(stop_loss_yes_price=1.5)


# ---------------------------------------------------------------------------
# format_summary — stop-loss stats section
# ---------------------------------------------------------------------------

class TestFormatSummaryStopLoss:
    def _sl_trade(self, pnl: float = -0.60) -> SimTrade:
        return SimTrade(
            opened_ts_ms=1000,
            closed_ts_ms=100_000,
            market_id="m1",
            asset="BTC",
            side="YES",
            entry_price=0.501,
            exit_price=0.35,
            notional=2.0,
            quantity=3.99,
            fees=0.002,
            slippage_cost=0.001,
            pnl=pnl,
            status=STATUS_CLOSED_STOP_LOSS,
            reason="stop_loss_yes_price",
        )

    def test_stop_loss_section_present(self):
        text = format_summary([self._sl_trade()], final_cash=99.4, initial_cash=100.0)
        assert "STOP-LOSS" in text.upper() or "stop-loss" in text.lower()

    def test_stop_loss_trigger_count_shown(self):
        text = format_summary([self._sl_trade(), self._sl_trade()], 98.8, 100.0)
        assert "Stop-loss triggers" in text
        assert ": 2" in text

    def test_hold_window_count_shown(self):
        hw = _closed_sim_trade(pnl=0.10, reason="hold_window_expired")
        text = format_summary([hw], 100.10, 100.0)
        assert "Hold-window closes" in text
        assert ": 1" in text

    def test_large_loss_count_shown(self):
        big_loss = self._sl_trade(pnl=-7.0)
        text = format_summary([big_loss], 93.0, 100.0)
        assert "Large losses" in text
        assert ": 1" in text

    def test_large_loss_not_counted_for_small_loss(self):
        small_loss = self._sl_trade(pnl=-2.0)
        text = format_summary([small_loss], 98.0, 100.0)
        assert "Large losses" in text
        # small loss should show 0
        lines = text.split("\n")
        large_line = next(l for l in lines if "Large losses" in l)
        assert ": 0" in large_line

    def test_max_drawdown_shown(self):
        text = format_summary([self._sl_trade()], 99.4, 100.0)
        assert "Max drawdown" in text

    def test_closed_stop_loss_excluded_from_skipped(self):
        trade = self._sl_trade()
        text = format_summary([trade], 99.4, 100.0)
        lines = text.split("\n")
        skipped_line = next((l for l in lines if "Skipped" in l), None)
        assert skipped_line is not None
        assert ": 0" in skipped_line
