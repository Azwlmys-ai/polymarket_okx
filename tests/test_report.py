"""
tests/test_report.py — unit tests for the lag distribution report module.

All tests are deterministic, use no network, and use only in-memory or
tmp_path SQLite databases.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.report import (
    DataQuality,
    LagReport,
    PercentileStats,
    _percentile,
    build_data_quality,
    build_report,
    compute_avg_move_pct,
    compute_per_asset_trade_stats,
    compute_percentile_stats,
    format_per_asset_trade_table,
    format_report,
    load_lag_records,
    write_report_json,
    write_report_markdown,
)

SCHEMA_SQL = (Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lag_row(
    lag_ms: int = 1000,
    asset: str = "BTC",
    market_id: str = "poly-btc-001",
    exchange_move_ts_ms: int = 5000,
    prediction_response_ts_ms: int = 6000,
    exchange_price_before: float | None = 100.0,
    exchange_price_after: float | None = 101.0,
    prediction_price_before: float | None = None,
    prediction_price_after: float | None = 0.65,
    notes: str | None = "pct_change=1.0000%",
) -> dict:
    return {
        "ts_ms": 9_000_000,
        "exchange_source": "okx",
        "prediction_source": "polymarket",
        "asset": asset,
        "market_id": market_id,
        "exchange_move_ts_ms": exchange_move_ts_ms,
        "prediction_response_ts_ms": prediction_response_ts_ms,
        "lag_ms": lag_ms,
        "exchange_price_before": exchange_price_before,
        "exchange_price_after": exchange_price_after,
        "prediction_price_before": prediction_price_before,
        "prediction_price_after": prediction_price_after,
        "notes": notes,
    }


def _db_with_lag_records(tmp_path: Path, rows: list[dict]) -> Path:
    """Create a SQLite DB in tmp_path with the project schema and insert rows."""
    db_file = tmp_path / "test.db"
    with sqlite3.connect(db_file) as conn:
        conn.executescript(SCHEMA_SQL)
        for r in rows:
            conn.execute(
                """INSERT INTO lag_records
                   (ts_ms, exchange_source, prediction_source, asset, market_id,
                    exchange_move_ts_ms, prediction_response_ts_ms, lag_ms,
                    exchange_price_before, exchange_price_after,
                    prediction_price_before, prediction_price_after, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    r["ts_ms"], r["exchange_source"], r["prediction_source"],
                    r["asset"], r["market_id"], r["exchange_move_ts_ms"],
                    r["prediction_response_ts_ms"], r["lag_ms"],
                    r["exchange_price_before"], r["exchange_price_after"],
                    r["prediction_price_before"], r["prediction_price_after"],
                    r["notes"],
                ),
            )
        conn.commit()
    return db_file


class _ClosedPositionStub:
    def __init__(
        self,
        asset: str,
        pnl: float,
        hold_s: float,
        entry_yes_price: float,
        exit_yes_price: float,
    ) -> None:
        self.pos = type(
            "PositionStub",
            (),
            {
                "asset": asset,
                "opened_ts_ms": 1_000_000,
                "entry_yes_price": entry_yes_price,
            },
        )()
        self.closed_ts_ms = 1_000_000 + int(hold_s * 1000)
        self.exit_yes_price = exit_yes_price
        self.pnl = pnl


# ---------------------------------------------------------------------------
# _percentile
# ---------------------------------------------------------------------------

class TestPercentile:
    def test_single_value_returns_itself(self):
        assert _percentile([5.0], 50) == pytest.approx(5.0)

    def test_p50_of_sorted_pair(self):
        assert _percentile([0.0, 100.0], 50) == pytest.approx(50.0)

    def test_p0_is_min(self):
        assert _percentile([10.0, 20.0, 30.0], 0) == pytest.approx(10.0)

    def test_p100_is_max(self):
        assert _percentile([10.0, 20.0, 30.0], 100) == pytest.approx(30.0)

    def test_p90_interpolated(self):
        # 10 values: p90 index = 0.9 * 9 = 8.1 → between [8] and [9]
        values = [float(i) for i in range(1, 11)]  # 1..10
        result = _percentile(values, 90)
        assert result == pytest.approx(9.1)

    def test_empty_raises_value_error(self):
        with pytest.raises(ValueError, match="empty"):
            _percentile([], 50)


# ---------------------------------------------------------------------------
# compute_percentile_stats
# ---------------------------------------------------------------------------

class TestComputePercentileStats:
    def test_single_record(self):
        s = compute_percentile_stats([500.0])
        assert s.count == 1
        assert s.min_ms == pytest.approx(500.0)
        assert s.max_ms == pytest.approx(500.0)
        assert s.mean_ms == pytest.approx(500.0)
        assert s.median_ms == pytest.approx(500.0)

    def test_count_matches_input(self):
        s = compute_percentile_stats([1000.0, 2000.0, 3000.0])
        assert s.count == 3

    def test_mean_is_arithmetic_mean(self):
        s = compute_percentile_stats([100.0, 200.0, 300.0])
        assert s.mean_ms == pytest.approx(200.0)

    def test_min_max_correct(self):
        s = compute_percentile_stats([500.0, 100.0, 9000.0, 200.0])
        assert s.min_ms == pytest.approx(100.0)
        assert s.max_ms == pytest.approx(9000.0)

    def test_p90_ge_median(self):
        values = [float(i * 100) for i in range(1, 21)]
        s = compute_percentile_stats(values)
        assert s.p90_ms >= s.median_ms

    def test_p95_ge_p90(self):
        values = [float(i * 100) for i in range(1, 21)]
        s = compute_percentile_stats(values)
        assert s.p95_ms >= s.p90_ms

    def test_empty_raises_value_error(self):
        with pytest.raises(ValueError):
            compute_percentile_stats([])


# ---------------------------------------------------------------------------
# compute_avg_move_pct
# ---------------------------------------------------------------------------

class TestComputeAvgMovePct:
    def test_single_record_1pct(self):
        row = _lag_row(exchange_price_before=100.0, exchange_price_after=101.0)
        result = compute_avg_move_pct([row])
        assert result == pytest.approx(0.01)

    def test_average_of_multiple_moves(self):
        rows = [
            _lag_row(exchange_price_before=100.0, exchange_price_after=101.0),  # 1%
            _lag_row(exchange_price_before=200.0, exchange_price_after=201.0),  # 0.5%
        ]
        result = compute_avg_move_pct(rows)
        assert result == pytest.approx(0.0075)

    def test_none_prices_skipped(self):
        rows = [
            _lag_row(exchange_price_before=None, exchange_price_after=101.0),
            _lag_row(exchange_price_before=100.0, exchange_price_after=None),
        ]
        assert compute_avg_move_pct(rows) is None

    def test_empty_list_returns_none(self):
        assert compute_avg_move_pct([]) is None

    def test_zero_before_price_skipped(self):
        rows = [_lag_row(exchange_price_before=0.0, exchange_price_after=101.0)]
        assert compute_avg_move_pct(rows) is None

    def test_down_move_absolute(self):
        row = _lag_row(exchange_price_before=100.0, exchange_price_after=99.0)
        result = compute_avg_move_pct([row])
        assert result == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# build_data_quality
# ---------------------------------------------------------------------------

class TestBuildDataQuality:
    def test_empty_records(self):
        dq = build_data_quality([])
        assert dq.total_records == 0
        assert dq.snapshot_density_warning is True

    def test_counts_missing_prediction_before(self):
        rows = [_lag_row(prediction_price_before=None)] * 3
        dq = build_data_quality(rows)
        assert dq.missing_prediction_price_before == 3

    def test_counts_missing_prediction_after(self):
        rows = [
            _lag_row(prediction_price_after=None),
            _lag_row(prediction_price_after=0.65),
        ]
        dq = build_data_quality(rows)
        assert dq.missing_prediction_price_after == 1

    def test_unique_market_ids(self):
        rows = [
            _lag_row(market_id="m1"),
            _lag_row(market_id="m2"),
            _lag_row(market_id="m1"),
        ]
        dq = build_data_quality(rows)
        assert dq.unique_polymarket_market_ids == 2

    def test_possible_duplicate_responses_when_more_moves_than_markets(self):
        # 3 moves, 2 unique market_ids → 1 possible dup
        rows = [
            _lag_row(market_id="m1", exchange_move_ts_ms=1000),
            _lag_row(market_id="m1", exchange_move_ts_ms=2000),
            _lag_row(market_id="m2", exchange_move_ts_ms=3000),
        ]
        dq = build_data_quality(rows)
        assert dq.possible_duplicate_responses == 1

    def test_density_warning_suppressed_when_enough_records(self):
        rows = [_lag_row() for _ in range(10)]
        dq = build_data_quality(rows)
        assert dq.snapshot_density_warning is False


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------

class TestBuildReport:
    def test_empty_records_returns_zero_total(self):
        r = build_report([], db_path="test.db")
        assert r.total_records == 0
        assert r.overall is None
        assert r.by_asset == []

    def test_disclaimer_present(self):
        r = build_report([], db_path="test.db")
        assert "NOT" in r.disclaimer

    def test_single_record(self):
        rows = [_lag_row(lag_ms=1500)]
        r = build_report(rows, db_path="test.db")
        assert r.total_records == 1
        assert r.overall is not None
        assert r.overall.count == 1
        assert r.overall.mean_ms == pytest.approx(1500.0)

    def test_by_asset_grouping(self):
        rows = [
            _lag_row(lag_ms=1000, asset="BTC"),
            _lag_row(lag_ms=2000, asset="ETH"),
            _lag_row(lag_ms=3000, asset="BTC"),
        ]
        r = build_report(rows, db_path="test.db")
        assets = {a.asset: a for a in r.by_asset}
        assert "BTC" in assets
        assert "ETH" in assets
        assert assets["BTC"].count == 2
        assert assets["ETH"].count == 1

    def test_overall_stats_across_all_assets(self):
        rows = [_lag_row(lag_ms=1000), _lag_row(lag_ms=3000)]
        r = build_report(rows, db_path="test.db")
        assert r.overall is not None
        assert r.overall.mean_ms == pytest.approx(2000.0)

    def test_data_quality_attached(self):
        rows = [_lag_row()]
        r = build_report(rows, db_path="test.db")
        assert r.data_quality is not None

    def test_avg_move_pct_in_asset_stats(self):
        rows = [_lag_row(exchange_price_before=100.0, exchange_price_after=101.0)]
        r = build_report(rows, db_path="test.db")
        assert len(r.by_asset) == 1
        assert r.by_asset[0].avg_move_pct == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------

class TestFormatReport:
    def test_empty_report_contains_no_records_message(self):
        r = build_report([], db_path="test.db")
        text = format_report(r)
        assert "No lag records found" in text

    def test_non_empty_report_contains_stats(self):
        rows = [_lag_row(lag_ms=1000), _lag_row(lag_ms=2000)]
        r = build_report(rows, db_path="test.db")
        text = format_report(r)
        assert "OVERALL LAG STATISTICS" in text
        assert "PER-ASSET BREAKDOWN" in text

    def test_disclaimer_in_output(self):
        r = build_report([], db_path="test.db")
        text = format_report(r)
        assert "DISCLAIMER" in text

    def test_not_trading_signal_in_output(self):
        r = build_report([_lag_row()], db_path="test.db")
        text = format_report(r)
        assert "NOT" in text

    def test_data_quality_notes_in_output(self):
        r = build_report([_lag_row()], db_path="test.db")
        text = format_report(r)
        assert "DATA QUALITY" in text

    def test_density_warning_shown_for_few_records(self):
        r = build_report([_lag_row()], db_path="test.db")
        text = format_report(r)
        assert "Fewer than 5" in text

    def test_density_warning_absent_for_many_records(self):
        rows = [_lag_row(lag_ms=i * 100) for i in range(1, 11)]
        r = build_report(rows, db_path="test.db")
        text = format_report(r)
        assert "Fewer than 5" not in text


# ---------------------------------------------------------------------------
# load_lag_records (SQLite I/O)
# ---------------------------------------------------------------------------

class TestLoadLagRecords:
    def test_empty_db_returns_empty_list(self, tmp_path: Path):
        db_file = _db_with_lag_records(tmp_path, [])
        records = load_lag_records(db_file)
        assert records == []

    def test_nonexistent_db_returns_empty_list(self, tmp_path: Path):
        records = load_lag_records(tmp_path / "missing.db")
        assert records == []

    def test_loaded_records_match_inserted(self, tmp_path: Path):
        db_file = _db_with_lag_records(tmp_path, [_lag_row(lag_ms=2500, asset="ETH")])
        records = load_lag_records(db_file)
        assert len(records) == 1
        assert records[0]["lag_ms"] == 2500
        assert records[0]["asset"] == "ETH"

    def test_multiple_records_loaded(self, tmp_path: Path):
        rows = [_lag_row(lag_ms=i * 100) for i in range(1, 6)]
        db_file = _db_with_lag_records(tmp_path, rows)
        records = load_lag_records(db_file)
        assert len(records) == 5

    def test_records_sorted_by_ts_ms(self, tmp_path: Path):
        rows = [
            {**_lag_row(), "ts_ms": 3000},
            {**_lag_row(), "ts_ms": 1000},
            {**_lag_row(), "ts_ms": 2000},
        ]
        db_file = _db_with_lag_records(tmp_path, rows)
        records = load_lag_records(db_file)
        ts_values = [r["ts_ms"] for r in records]
        assert ts_values == sorted(ts_values)


# ---------------------------------------------------------------------------
# write_report_json / write_report_markdown
# ---------------------------------------------------------------------------

class TestReportWriters:
    def _sample_report(self) -> LagReport:
        return build_report([_lag_row(lag_ms=1500)], db_path="test.db")

    def test_json_file_created(self, tmp_path: Path):
        out_dir = tmp_path / "reports"
        path = write_report_json(self._sample_report(), out_dir)
        assert path.exists()
        assert path.suffix == ".json"

    def test_json_is_valid(self, tmp_path: Path):
        out_dir = tmp_path / "reports"
        path = write_report_json(self._sample_report(), out_dir)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["total_records"] == 1
        assert "disclaimer" in data

    def test_markdown_file_created(self, tmp_path: Path):
        out_dir = tmp_path / "reports"
        path = write_report_markdown(self._sample_report(), out_dir)
        assert path.exists()
        assert path.suffix == ".md"

    def test_markdown_contains_disclaimer(self, tmp_path: Path):
        out_dir = tmp_path / "reports"
        path = write_report_markdown(self._sample_report(), out_dir)
        text = path.read_text(encoding="utf-8")
        assert "DISCLAIMER" in text

    def test_reports_dir_created_if_missing(self, tmp_path: Path):
        out_dir = tmp_path / "new" / "reports"
        assert not out_dir.exists()
        write_report_json(self._sample_report(), out_dir)
        assert out_dir.exists()


# ---------------------------------------------------------------------------
# per-asset closed position statistics
# ---------------------------------------------------------------------------

class TestPerAssetTradeStats:
    def test_empty_closed_positions_outputs_notice(self):
        stats = compute_per_asset_trade_stats([])
        text = format_per_asset_trade_table(stats)

        assert stats == []
        assert "## Per-Asset 统计" in text
        assert "暂无已平仓交易，无法生成 per-asset 统计。" in text

    def test_single_asset_single_trade_percentiles_equal_value(self):
        trades = [_ClosedPositionStub("BTC", pnl=1.25, hold_s=30, entry_yes_price=0.51, exit_yes_price=0.54)]

        stats = compute_per_asset_trade_stats(trades)
        text = format_per_asset_trade_table(stats)

        assert len(stats) == 1
        btc = stats[0]
        assert btc.asset == "BTC"
        assert btc.trade_count == 1
        assert btc.pnl_p25 == pytest.approx(1.25)
        assert btc.pnl_p50 == pytest.approx(1.25)
        assert btc.pnl_p75 == pytest.approx(1.25)
        assert btc.hold_s_p25 == pytest.approx(30.0)
        assert btc.hold_s_p50 == pytest.approx(30.0)
        assert btc.hold_s_p75 == pytest.approx(30.0)
        assert btc.entry_p25 == pytest.approx(0.51)
        assert btc.entry_p50 == pytest.approx(0.51)
        assert btc.entry_p75 == pytest.approx(0.51)
        assert btc.exit_p25 == pytest.approx(0.54)
        assert btc.exit_p50 == pytest.approx(0.54)
        assert btc.exit_p75 == pytest.approx(0.54)
        assert "| BTC | 1 | +1.2500 | +1.2500 | +1.2500 | 30 | 30 | 30 | 0.5100 | 0.5100 | 0.5100 | 0.5400 | 0.5400 | 0.5400 |" in text

    def test_multiple_assets_multiple_trades(self):
        trades = [
            _ClosedPositionStub("BTC", pnl=-1.0, hold_s=10, entry_yes_price=0.48, exit_yes_price=0.47),
            _ClosedPositionStub("BTC", pnl=3.0, hold_s=30, entry_yes_price=0.52, exit_yes_price=0.55),
            _ClosedPositionStub("ETH", pnl=2.0, hold_s=20, entry_yes_price=0.50, exit_yes_price=0.53),
            _ClosedPositionStub("SOL", pnl=4.0, hold_s=40, entry_yes_price=0.49, exit_yes_price=0.57),
            _ClosedPositionStub("SOL", pnl=8.0, hold_s=80, entry_yes_price=0.51, exit_yes_price=0.59),
        ]

        stats = compute_per_asset_trade_stats(trades)
        by_asset = {s.asset: s for s in stats}
        text = format_per_asset_trade_table(stats)

        assert [s.asset for s in stats] == ["BTC", "ETH", "SOL"]
        assert by_asset["BTC"].trade_count == 2
        assert by_asset["BTC"].pnl_p25 == pytest.approx(0.0)
        assert by_asset["BTC"].pnl_p50 == pytest.approx(1.0)
        assert by_asset["BTC"].pnl_p75 == pytest.approx(2.0)
        assert by_asset["BTC"].hold_s_p50 == pytest.approx(20.0)
        assert by_asset["ETH"].trade_count == 1
        assert by_asset["ETH"].entry_p25 == pytest.approx(0.50)
        assert by_asset["SOL"].trade_count == 2
        assert by_asset["SOL"].exit_p75 == pytest.approx(0.585)
        assert "| asset | trades | pnl_p25 | pnl_p50 | pnl_p75" in text
        assert "| ETH | 1 | +2.0000 | +2.0000 | +2.0000" in text
