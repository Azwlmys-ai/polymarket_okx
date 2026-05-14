"""
tests/test_evaluator.py — unit tests for the paper-trading profitability evaluator.

All tests are deterministic, use no network connections, and use only in-memory
or tmp_path SQLite databases.  No API keys, no real trading.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.evaluator import (
    DrawdownStats,
    PaperEvalMetrics,
    _DISCLAIMER,
    _median,
    build_data_quality_notes,
    compute_drawdown,
    compute_eval_metrics,
    format_eval_report,
    load_paper_trades,
    run_evaluation,
    write_eval_report_json,
    write_eval_report_markdown,
)

# ---------------------------------------------------------------------------
# Schema helper (creates only the paper_trades table needed for these tests)
# ---------------------------------------------------------------------------

_SCHEMA_SQL = (Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8")


def _make_db(tmp_path: Path) -> Path:
    """Create a minimal SQLite DB with the full schema and return its path."""
    db = tmp_path / "test.db"
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(_SCHEMA_SQL)
    return db


def _insert_trades(db: Path, trades: list[dict]) -> None:
    """Insert rows into paper_trades."""
    with sqlite3.connect(str(db)) as conn:
        conn.executemany(
            """
            INSERT INTO paper_trades
                (opened_ts_ms, closed_ts_ms, market_id, asset, side,
                 entry_price, exit_price, notional, quantity,
                 fees, slippage, pnl, status, reason)
            VALUES (:opened_ts_ms, :closed_ts_ms, :market_id, :asset, :side,
                    :entry_price, :exit_price, :notional, :quantity,
                    :fees, :slippage, :pnl, :status, :reason)
            """,
            trades,
        )


def _trade(
    *,
    opened_ts_ms: int = 1_000_000,
    closed_ts_ms: int | None = 2_000_000,
    market_id: str = "poly-btc-001",
    asset: str = "BTC",
    side: str = "YES",
    entry_price: float = 0.50,
    exit_price: float | None = 0.60,
    notional: float = 2.0,
    quantity: float = 4.0,
    fees: float = 0.002,
    slippage: float = 0.001,
    pnl: float | None = 0.398,  # (0.60−0.50)*4 − 0.002
    status: str = "closed",
    reason: str = "test trade",
) -> dict:
    return {
        "opened_ts_ms": opened_ts_ms,
        "closed_ts_ms": closed_ts_ms,
        "market_id": market_id,
        "asset": asset,
        "side": side,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "notional": notional,
        "quantity": quantity,
        "fees": fees,
        "slippage": slippage,
        "pnl": pnl,
        "status": status,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# _median helper
# ---------------------------------------------------------------------------

class TestMedian:
    def test_single_element(self) -> None:
        assert _median([5.0]) == 5.0

    def test_even_count(self) -> None:
        assert _median([1.0, 3.0]) == pytest.approx(2.0)

    def test_odd_count(self) -> None:
        assert _median([1.0, 2.0, 10.0]) == pytest.approx(2.0)

    def test_sorted_and_unsorted_same(self) -> None:
        assert _median([3.0, 1.0, 2.0]) == _median([1.0, 2.0, 3.0])

    def test_negatives(self) -> None:
        assert _median([-3.0, -1.0, -2.0]) == pytest.approx(-2.0)


# ---------------------------------------------------------------------------
# compute_drawdown
# ---------------------------------------------------------------------------

class TestComputeDrawdown:
    def test_empty_returns_none(self) -> None:
        dd = compute_drawdown([])
        assert dd.max_drawdown is None
        assert dd.max_drawdown_pct is None

    def test_all_positive_no_drawdown(self) -> None:
        dd = compute_drawdown([1.0, 2.0, 3.0])
        assert dd.max_drawdown == pytest.approx(0.0)
        assert dd.max_drawdown_pct == pytest.approx(0.0)

    def test_single_loss(self) -> None:
        # cumulative: 2 → 2 → 1 (drop of 1 from peak 2)
        dd = compute_drawdown([2.0, -1.0])
        assert dd.max_drawdown == pytest.approx(1.0)
        assert dd.max_drawdown_pct == pytest.approx(0.5)

    def test_larger_drawdown_later(self) -> None:
        # cumulative: 1 → 2 → 0 (peak=2, trough=0, dd=2)
        dd = compute_drawdown([1.0, 1.0, -2.0])
        assert dd.max_drawdown == pytest.approx(2.0)
        assert dd.max_drawdown_pct == pytest.approx(1.0)

    def test_partial_recovery(self) -> None:
        # cumulative: 3 → 1 → 2 → 0   peak=3, max_dd=3
        dd = compute_drawdown([3.0, -2.0, 1.0, -2.0])
        assert dd.max_drawdown == pytest.approx(3.0)

    def test_all_losses_drawdown_from_zero_baseline(self) -> None:
        # All negative: peak stays at 0 (starting baseline); cumulative drops to -6.
        # Max drawdown = 0 - (-6) = 6; pct undefined because peak == 0.
        dd = compute_drawdown([-1.0, -2.0, -3.0])
        assert dd.max_drawdown == pytest.approx(6.0)
        assert dd.max_drawdown_pct is None  # peak <= 0 → pct undefined

    def test_single_trade_win(self) -> None:
        dd = compute_drawdown([5.0])
        assert dd.max_drawdown == pytest.approx(0.0)

    def test_single_trade_loss(self) -> None:
        # Cumulative drops to -5 from peak of 0; drawdown = 5.
        dd = compute_drawdown([-5.0])
        assert dd.max_drawdown == pytest.approx(5.0)
        assert dd.max_drawdown_pct is None  # peak == 0 → pct undefined


# ---------------------------------------------------------------------------
# compute_eval_metrics — empty input
# ---------------------------------------------------------------------------

class TestComputeEvalMetricsEmpty:
    def test_empty_rows_total_zero(self) -> None:
        m = compute_eval_metrics([], db_path="test.db")
        assert m.total_rows == 0

    def test_empty_rows_closed_zero(self) -> None:
        m = compute_eval_metrics([], db_path="test.db")
        assert m.closed_count == 0
        assert m.open_no_exit_count == 0
        assert m.skipped_count == 0

    def test_empty_rows_pnl_none(self) -> None:
        m = compute_eval_metrics([], db_path="test.db")
        assert m.gross_pnl is None
        assert m.net_pnl is None
        assert m.avg_pnl is None
        assert m.median_pnl is None

    def test_empty_rows_win_rate_none(self) -> None:
        m = compute_eval_metrics([], db_path="test.db")
        assert m.win_rate is None
        assert m.wins == 0
        assert m.losses == 0

    def test_empty_rows_drawdown_none(self) -> None:
        m = compute_eval_metrics([], db_path="test.db")
        assert m.drawdown.max_drawdown is None

    def test_empty_rows_notional_zero(self) -> None:
        m = compute_eval_metrics([], db_path="test.db")
        assert m.open_no_exit_notional == pytest.approx(0.0)

    def test_empty_rows_disclaimer_present(self) -> None:
        m = compute_eval_metrics([], db_path="test.db")
        assert "NOT proof" in m.disclaimer

    def test_empty_rows_by_status_empty(self) -> None:
        m = compute_eval_metrics([], db_path="test.db")
        assert m.by_status == {}


# ---------------------------------------------------------------------------
# compute_eval_metrics — all winning trades
# ---------------------------------------------------------------------------

class TestComputeEvalMetricsAllWins:
    def _make_win(self, pnl: float, opened_ts_ms: int = 1_000_000) -> dict:
        """A closed trade with positive PnL."""
        return _trade(
            opened_ts_ms=opened_ts_ms,
            exit_price=0.70,
            quantity=4.0,
            entry_price=0.50,
            fees=0.002,
            pnl=pnl,
            status="closed",
        )

    def test_all_wins_win_rate_one(self) -> None:
        rows = [
            self._make_win(0.5, 1_000_000),
            self._make_win(0.3, 2_000_000),
            self._make_win(0.2, 3_000_000),
        ]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.wins == 3
        assert m.losses == 0
        assert m.win_rate == pytest.approx(1.0)

    def test_all_wins_net_pnl(self) -> None:
        rows = [self._make_win(0.5), self._make_win(0.3)]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.net_pnl == pytest.approx(0.8)

    def test_all_wins_avg_pnl(self) -> None:
        rows = [self._make_win(0.4), self._make_win(0.6)]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.avg_pnl == pytest.approx(0.5)

    def test_all_wins_no_drawdown(self) -> None:
        rows = [self._make_win(1.0, 1_000), self._make_win(2.0, 2_000)]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.drawdown.max_drawdown == pytest.approx(0.0)

    def test_all_wins_closed_count(self) -> None:
        rows = [self._make_win(0.1)] * 5
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.closed_count == 5


# ---------------------------------------------------------------------------
# compute_eval_metrics — all losing trades
# ---------------------------------------------------------------------------

class TestComputeEvalMetricsAllLosses:
    def _make_loss(self, pnl: float, opened_ts_ms: int = 1_000_000) -> dict:
        return _trade(
            opened_ts_ms=opened_ts_ms,
            exit_price=0.40,
            entry_price=0.50,
            quantity=4.0,
            fees=0.002,
            pnl=pnl,
            status="closed",
        )

    def test_all_losses_win_rate_zero(self) -> None:
        rows = [self._make_loss(-0.2), self._make_loss(-0.3)]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.wins == 0
        assert m.losses == 2
        assert m.win_rate == pytest.approx(0.0)

    def test_all_losses_net_pnl_negative(self) -> None:
        rows = [self._make_loss(-0.5), self._make_loss(-0.3)]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.net_pnl == pytest.approx(-0.8)

    def test_all_losses_avg_pnl_negative(self) -> None:
        rows = [self._make_loss(-0.4), self._make_loss(-0.6)]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.avg_pnl == pytest.approx(-0.5)

    def test_all_losses_drawdown_from_zero_baseline(self) -> None:
        # All PnL negative → cumulative drops from starting baseline 0.
        # Two trades with pnl=-1.0 each → cumulative goes -1, -2 → max_dd = 2.
        rows = [self._make_loss(-1.0, 1_000), self._make_loss(-1.0, 2_000)]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.drawdown.max_drawdown == pytest.approx(2.0)
        assert m.drawdown.max_drawdown_pct is None  # peak == 0 → pct undefined

    def test_all_losses_win_loss_counts(self) -> None:
        rows = [self._make_loss(-0.1)] * 4
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.wins == 0
        assert m.losses == 4


# ---------------------------------------------------------------------------
# compute_eval_metrics — open_no_exit handling
# ---------------------------------------------------------------------------

class TestOpenNoExitHandling:
    def _open_trade(self, notional: float = 2.0, fees: float = 0.002) -> dict:
        return _trade(
            notional=notional,
            fees=fees,
            exit_price=None,
            pnl=None,
            closed_ts_ms=None,
            status="open_no_exit",
        )

    def test_open_no_exit_count(self) -> None:
        rows = [self._open_trade()] * 3
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.open_no_exit_count == 3

    def test_open_no_exit_notional_sum(self) -> None:
        rows = [self._open_trade(notional=2.0), self._open_trade(notional=3.0)]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.open_no_exit_notional == pytest.approx(5.0)

    def test_open_no_exit_fees_sum(self) -> None:
        rows = [self._open_trade(fees=0.01), self._open_trade(fees=0.02)]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.open_no_exit_fees_at_risk == pytest.approx(0.03)

    def test_open_no_exit_not_counted_as_closed(self) -> None:
        rows = [self._open_trade()] * 5
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.closed_count == 0
        assert m.net_pnl is None

    def test_open_no_exit_not_in_drawdown(self) -> None:
        rows = [self._open_trade()] * 2
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.drawdown.max_drawdown is None

    def test_open_no_exit_counted_in_by_status(self) -> None:
        rows = [self._open_trade()] * 2
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.by_status.get("open_no_exit") == 2

    def test_open_no_exit_counted_in_skipped_count_is_zero(self) -> None:
        rows = [self._open_trade()]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.skipped_count == 0  # open_no_exit is not "skipped"


# ---------------------------------------------------------------------------
# compute_eval_metrics — mixed statuses
# ---------------------------------------------------------------------------

class TestMixedStatuses:
    def test_skipped_count_correct(self) -> None:
        rows = [
            _trade(status="closed", pnl=0.1),
            _trade(status="open_no_exit", pnl=None, exit_price=None),
            _trade(status="skipped_down_move", notional=0.0, pnl=None, exit_price=None),
            _trade(status="skipped_invalid_price", notional=0.0, pnl=None, exit_price=None),
        ]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.closed_count == 1
        assert m.open_no_exit_count == 1
        assert m.skipped_count == 2
        assert m.total_rows == 4

    def test_by_status_all_keys_present(self) -> None:
        rows = [
            _trade(status="closed", pnl=0.1),
            _trade(status="skipped_no_cash", notional=0.0, pnl=None, exit_price=None),
        ]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert "closed" in m.by_status
        assert "skipped_no_cash" in m.by_status

    def test_mixed_gross_pnl_excludes_open(self) -> None:
        # 1 closed trade: (0.60 - 0.50) * 4.0 = 0.40 gross
        rows = [
            _trade(status="closed", entry_price=0.50, exit_price=0.60, quantity=4.0, fees=0.002, pnl=0.398),
            _trade(status="open_no_exit", pnl=None, exit_price=None),
        ]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.gross_pnl == pytest.approx(0.40)


# ---------------------------------------------------------------------------
# build_data_quality_notes
# ---------------------------------------------------------------------------

class TestBuildDataQualityNotes:
    def test_empty_rows_mentions_no_data(self) -> None:
        notes = build_data_quality_notes([])
        assert any("No paper_trades" in n for n in notes)

    def test_small_sample_mentioned(self) -> None:
        rows = [_trade()] * 3
        notes = build_data_quality_notes(rows)
        assert any("small" in n.lower() for n in notes)

    def test_no_real_execution_caveat_always_present(self) -> None:
        rows = [_trade()] * 5
        notes = build_data_quality_notes(rows)
        joined = " ".join(notes)
        assert "real" in joined.lower()

    def test_open_no_exit_mentioned_when_present(self) -> None:
        rows = [
            _trade(status="open_no_exit", pnl=None, exit_price=None, closed_ts_ms=None)
        ]
        notes = build_data_quality_notes(rows)
        assert any("open_no_exit" in n for n in notes)

    def test_null_pnl_on_closed_mentioned(self) -> None:
        rows = [_trade(status="closed", pnl=None)]
        notes = build_data_quality_notes(rows)
        assert any("NULL pnl" in n or "null pnl" in n.lower() for n in notes)


# ---------------------------------------------------------------------------
# format_eval_report — disclaimer presence
# ---------------------------------------------------------------------------

class TestFormatEvalReport:
    def test_disclaimer_in_empty_report(self) -> None:
        m = compute_eval_metrics([], db_path="test.db")
        text = format_eval_report(m)
        assert "NOT proof" in text
        assert "NOT a trading recommendation" in text.upper() or "not a trading recommendation" in text.lower()

    def test_disclaimer_in_non_empty_report(self) -> None:
        rows = [_trade(status="closed", pnl=0.1)]
        m = compute_eval_metrics(rows, db_path="test.db")
        text = format_eval_report(m)
        assert "SIMULATION DISCLAIMER" in text

    def test_no_trades_message_when_empty(self) -> None:
        m = compute_eval_metrics([], db_path="test.db")
        text = format_eval_report(m)
        assert "No paper_trades rows found" in text

    def test_win_rate_shown_for_wins(self) -> None:
        rows = [
            _trade(status="closed", pnl=0.5),
            _trade(status="closed", pnl=-0.1),
        ]
        m = compute_eval_metrics(rows, db_path="test.db")
        text = format_eval_report(m)
        assert "win rate" in text.lower()
        assert "50.0%" in text

    def test_open_no_exit_section_present(self) -> None:
        rows = [_trade(status="open_no_exit", pnl=None, exit_price=None, closed_ts_ms=None)]
        m = compute_eval_metrics(rows, db_path="test.db")
        text = format_eval_report(m)
        assert "OPEN" in text.upper()
        assert "open_no_exit" in text

    def test_drawdown_na_for_no_closed(self) -> None:
        rows = [_trade(status="open_no_exit", pnl=None, exit_price=None, closed_ts_ms=None)]
        m = compute_eval_metrics(rows, db_path="test.db")
        text = format_eval_report(m)
        assert "N/A" in text

    def test_simulated_label_in_output(self) -> None:
        rows = [_trade(status="closed", pnl=0.2)]
        m = compute_eval_metrics(rows, db_path="test.db")
        text = format_eval_report(m)
        assert "simulated" in text.lower()


# ---------------------------------------------------------------------------
# load_paper_trades — SQLite integration (uses tmp_path)
# ---------------------------------------------------------------------------

class TestLoadPaperTrades:
    def test_empty_db_returns_empty_list(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        rows = load_paper_trades(db)
        assert rows == []

    def test_loaded_rows_match_inserted(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        _insert_trades(db, [_trade(status="closed", pnl=0.3)])
        rows = load_paper_trades(db)
        assert len(rows) == 1
        assert rows[0]["status"] == "closed"
        assert rows[0]["pnl"] == pytest.approx(0.3)

    def test_missing_table_returns_empty(self, tmp_path: Path) -> None:
        db = tmp_path / "empty.db"
        db.write_bytes(b"")  # empty file — no schema
        rows = load_paper_trades(db)
        assert rows == []

    def test_multiple_rows_loaded(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        trades = [
            _trade(status="closed", pnl=0.1, opened_ts_ms=1_000),
            _trade(status="open_no_exit", pnl=None, exit_price=None, closed_ts_ms=None, opened_ts_ms=2_000),
            _trade(status="skipped_down_move", pnl=None, exit_price=None, opened_ts_ms=3_000),
        ]
        _insert_trades(db, trades)
        rows = load_paper_trades(db)
        assert len(rows) == 3

    def test_rows_ordered_by_opened_ts_ms(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        _insert_trades(db, [
            _trade(opened_ts_ms=3_000, pnl=0.3),
            _trade(opened_ts_ms=1_000, pnl=0.1),
            _trade(opened_ts_ms=2_000, pnl=0.2),
        ])
        rows = load_paper_trades(db)
        ts_list = [r["opened_ts_ms"] for r in rows]
        assert ts_list == sorted(ts_list)


# ---------------------------------------------------------------------------
# run_evaluation — integration (uses tmp_path)
# ---------------------------------------------------------------------------

class TestRunEvaluation:
    def test_returns_metrics_object(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        metrics = run_evaluation(db)
        assert isinstance(metrics, PaperEvalMetrics)

    def test_empty_db_no_error(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        metrics = run_evaluation(db)
        assert metrics.total_rows == 0

    def test_with_trades_computes_closed_count(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        _insert_trades(db, [_trade(status="closed", pnl=0.2)] * 3)
        metrics = run_evaluation(db)
        assert metrics.closed_count == 3

    def test_writes_json_file(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        _insert_trades(db, [_trade(status="closed", pnl=0.2)])
        reports_dir = tmp_path / "reports"
        metrics = run_evaluation(db, reports_dir=reports_dir, output_formats=["json"])
        json_files = list(reports_dir.glob("paper_eval_*.json"))
        assert len(json_files) == 1
        data = json.loads(json_files[0].read_text())
        assert data["closed_count"] == 1

    def test_writes_markdown_file(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        _insert_trades(db, [_trade(status="closed", pnl=0.2)])
        reports_dir = tmp_path / "reports"
        run_evaluation(db, reports_dir=reports_dir, output_formats=["markdown"])
        md_files = list(reports_dir.glob("paper_eval_*.md"))
        assert len(md_files) == 1
        content = md_files[0].read_text()
        assert "SIMULATION DISCLAIMER" in content

    def test_no_output_formats_no_files(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        reports_dir = tmp_path / "reports"
        run_evaluation(db, reports_dir=reports_dir, output_formats=None)
        assert not reports_dir.exists()

    def test_disclaimer_always_in_metrics(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        metrics = run_evaluation(db)
        assert "NOT proof" in metrics.disclaimer


# ---------------------------------------------------------------------------
# write_eval_report_json / write_eval_report_markdown
# ---------------------------------------------------------------------------

class TestWriteReportFiles:
    def _base_metrics(self) -> PaperEvalMetrics:
        return compute_eval_metrics([], db_path="test.db")

    def test_json_file_is_valid_json(self, tmp_path: Path) -> None:
        metrics = self._base_metrics()
        out = write_eval_report_json(metrics, tmp_path / "rpt")
        data = json.loads(out.read_text())
        assert isinstance(data, dict)

    def test_json_file_contains_disclaimer(self, tmp_path: Path) -> None:
        metrics = self._base_metrics()
        out = write_eval_report_json(metrics, tmp_path / "rpt")
        data = json.loads(out.read_text())
        assert "NOT proof" in data["disclaimer"]

    def test_markdown_file_contains_header(self, tmp_path: Path) -> None:
        metrics = self._base_metrics()
        out = write_eval_report_markdown(metrics, tmp_path / "rpt")
        content = out.read_text()
        assert "Profitability Evaluation" in content

    def test_markdown_file_contains_disclaimer(self, tmp_path: Path) -> None:
        metrics = self._base_metrics()
        out = write_eval_report_markdown(metrics, tmp_path / "rpt")
        content = out.read_text()
        assert "SIMULATION DISCLAIMER" in content

    def test_json_reports_dir_created(self, tmp_path: Path) -> None:
        metrics = self._base_metrics()
        reports_dir = tmp_path / "new" / "reports"
        write_eval_report_json(metrics, reports_dir)
        assert reports_dir.is_dir()

    def test_markdown_reports_dir_created(self, tmp_path: Path) -> None:
        metrics = self._base_metrics()
        reports_dir = tmp_path / "new" / "reports"
        write_eval_report_markdown(metrics, reports_dir)
        assert reports_dir.is_dir()


# ---------------------------------------------------------------------------
# Gross PnL calculation correctness
# ---------------------------------------------------------------------------

class TestGrossPnl:
    def test_gross_pnl_calculation(self) -> None:
        # (exit - entry) * qty = (0.60 - 0.50) * 4.0 = 0.40
        rows = [_trade(entry_price=0.50, exit_price=0.60, quantity=4.0, pnl=0.398, status="closed")]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.gross_pnl == pytest.approx(0.40)

    def test_gross_pnl_negative_for_loss(self) -> None:
        rows = [_trade(entry_price=0.60, exit_price=0.40, quantity=4.0, pnl=-0.802, status="closed")]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.gross_pnl == pytest.approx(-0.80)

    def test_net_less_than_gross_due_to_fees(self) -> None:
        rows = [_trade(entry_price=0.50, exit_price=0.60, quantity=4.0, fees=0.01, pnl=0.39, status="closed")]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.net_pnl is not None
        assert m.gross_pnl is not None
        assert m.gross_pnl > m.net_pnl

    def test_gross_pnl_none_when_exit_price_missing(self) -> None:
        # Closed trade with no exit_price — shouldn't contribute to gross
        rows = [_trade(status="closed", exit_price=None, pnl=None)]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.gross_pnl is None

    def test_net_pnl_excludes_open_no_exit(self) -> None:
        rows = [
            _trade(status="closed", pnl=1.0),
            _trade(status="open_no_exit", pnl=None, exit_price=None),
        ]
        m = compute_eval_metrics(rows, db_path="x.db")
        assert m.net_pnl == pytest.approx(1.0)
