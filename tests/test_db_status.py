"""
tests/test_db_status.py — unit tests for src/db_status.py.

Uses file-backed SQLite via pytest tmp_path (which resolves to the OS temp
directory and supports SQLite locking on both macOS and Linux).
No network calls, no real trading, no API keys.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.db_status import DbStatus, SnapshotStats, SourceStats, format_status, query_status
from src.snapshot_store import ensure_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "test_status.db"
    ensure_schema(db)
    return db


def _insert_snapshots(db: Path, rows: list[dict]) -> None:
    with sqlite3.connect(db) as conn:
        conn.executemany(
            """
            INSERT INTO market_snapshots
                (ts_ms, source, market_id, symbol, bid, ask, mid, last,
                 liquidity, volume_24h, raw_json)
            VALUES
                (:ts_ms, :source, :market_id, :symbol, :bid, :ask, :mid, :last,
                 :liquidity, :volume_24h, :raw_json)
            """,
            rows,
        )
        conn.commit()


def _insert_lag_records(db: Path, rows: list[dict]) -> None:
    with sqlite3.connect(db) as conn:
        conn.executemany(
            """
            INSERT INTO lag_records
                (ts_ms, exchange_source, prediction_source, asset, market_id,
                 exchange_move_ts_ms, prediction_response_ts_ms, lag_ms,
                 exchange_price_before, exchange_price_after,
                 prediction_price_before, prediction_price_after, notes)
            VALUES
                (:ts_ms, :exchange_source, :prediction_source, :asset, :market_id,
                 :exchange_move_ts_ms, :prediction_response_ts_ms, :lag_ms,
                 :exchange_price_before, :exchange_price_after,
                 :prediction_price_before, :prediction_price_after, :notes)
            """,
            rows,
        )
        conn.commit()


def _insert_paper_trades(db: Path, rows: list[dict]) -> None:
    with sqlite3.connect(db) as conn:
        conn.executemany(
            """
            INSERT INTO paper_trades
                (opened_ts_ms, closed_ts_ms, market_id, asset, side,
                 entry_price, exit_price, notional, quantity,
                 fees, slippage, pnl, status, reason)
            VALUES
                (:opened_ts_ms, :closed_ts_ms, :market_id, :asset, :side,
                 :entry_price, :exit_price, :notional, :quantity,
                 :fees, :slippage, :pnl, :status, :reason)
            """,
            rows,
        )
        conn.commit()


def _snap(ts_ms: int, source: str, market_id: str = "BTC-USDT") -> dict:
    return {
        "ts_ms": ts_ms,
        "source": source,
        "market_id": market_id,
        "symbol": "BTC-USDT",
        "bid": 37000.0,
        "ask": 37001.0,
        "mid": 37000.5,
        "last": 37000.5,
        "liquidity": 1_000_000.0,
        "volume_24h": 5_000_000.0,
        "raw_json": "{}",
    }


def _lag(ts_ms: int) -> dict:
    return {
        "ts_ms": ts_ms,
        "exchange_source": "okx",
        "prediction_source": "polymarket",
        "asset": "BTC",
        "market_id": "poly-btc-001",
        "exchange_move_ts_ms": ts_ms - 500,
        "prediction_response_ts_ms": ts_ms,
        "lag_ms": 500,
        "exchange_price_before": 37000.0,
        "exchange_price_after": 37300.0,
        "prediction_price_before": None,
        "prediction_price_after": 0.65,
        "notes": "pct_change=0.8108%",
    }


def _paper(status: str, pnl: float | None = None) -> dict:
    return {
        "opened_ts_ms": 2000,
        "closed_ts_ms": 400_000 if status == "closed" else None,
        "market_id": "poly-btc-001",
        "asset": "BTC",
        "side": "YES",
        "entry_price": 0.651,
        "exit_price": 0.75 if status == "closed" else None,
        "notional": 2.0,
        "quantity": 3.07,
        "fees": 0.002,
        "slippage": 0.0013,
        "pnl": pnl,
        "status": status,
        "reason": "test",
    }


# ---------------------------------------------------------------------------
# 1. Empty DB
# ---------------------------------------------------------------------------

class TestEmptyDb:
    def test_empty_db_returns_zero_counts(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        status = query_status(db)
        assert status.snapshots.total == 0
        assert status.lag_record_count == 0
        assert status.paper_trade_count == 0

    def test_empty_db_by_source_is_empty(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        status = query_status(db)
        assert status.snapshots.by_source == {}

    def test_empty_db_paper_by_status_is_empty(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        status = query_status(db)
        assert status.paper_trade_by_status == {}

    def test_nonexistent_db_returns_zeros_not_raises(self, tmp_path: Path) -> None:
        db = tmp_path / "does_not_exist.db"
        status = query_status(db)
        assert status.snapshots.total == 0
        assert status.lag_record_count == 0
        assert status.paper_trade_count == 0

    def test_nonexistent_db_size_is_none(self, tmp_path: Path) -> None:
        db = tmp_path / "does_not_exist.db"
        status = query_status(db)
        assert status.db_size_bytes is None


# ---------------------------------------------------------------------------
# 2. Snapshot counts
# ---------------------------------------------------------------------------

class TestSnapshotStats:
    def test_total_count_correct(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        _insert_snapshots(db, [
            _snap(1000, "okx"),
            _snap(2000, "okx"),
            _snap(3000, "polymarket"),
        ])
        status = query_status(db)
        assert status.snapshots.total == 3

    def test_by_source_counts(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        _insert_snapshots(db, [
            _snap(1000, "okx"),
            _snap(2000, "okx"),
            _snap(3000, "polymarket"),
        ])
        status = query_status(db)
        assert status.snapshots.by_source["okx"].count == 2
        assert status.snapshots.by_source["polymarket"].count == 1

    def test_latest_ts_ms_per_source(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        _insert_snapshots(db, [
            _snap(1000, "okx"),
            _snap(5000, "okx"),
            _snap(3000, "polymarket"),
        ])
        status = query_status(db)
        assert status.snapshots.by_source["okx"].latest_ts_ms == 5000
        assert status.snapshots.by_source["polymarket"].latest_ts_ms == 3000

    def test_single_source_only(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        _insert_snapshots(db, [_snap(1000, "okx")])
        status = query_status(db)
        assert "okx" in status.snapshots.by_source
        assert "polymarket" not in status.snapshots.by_source


# ---------------------------------------------------------------------------
# 3. Lag record counts
# ---------------------------------------------------------------------------

class TestLagRecordStats:
    def test_lag_count_correct(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        _insert_lag_records(db, [_lag(1000), _lag(2000)])
        status = query_status(db)
        assert status.lag_record_count == 2

    def test_lag_latest_ts_ms(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        _insert_lag_records(db, [_lag(1000), _lag(5000)])
        status = query_status(db)
        assert status.lag_latest_ts_ms == 5000

    def test_no_lags_latest_is_none(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        status = query_status(db)
        assert status.lag_latest_ts_ms is None


# ---------------------------------------------------------------------------
# 4. Paper trade counts
# ---------------------------------------------------------------------------

class TestPaperTradeStats:
    def test_paper_total_count(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        _insert_paper_trades(db, [
            _paper("closed", pnl=0.15),
            _paper("open_no_exit"),
        ])
        status = query_status(db)
        assert status.paper_trade_count == 2

    def test_paper_by_status_breakdown(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        _insert_paper_trades(db, [
            _paper("closed", pnl=0.15),
            _paper("closed", pnl=-0.05),
            _paper("open_no_exit"),
        ])
        status = query_status(db)
        assert status.paper_trade_by_status["closed"] == 2
        assert status.paper_trade_by_status["open_no_exit"] == 1

    def test_paper_by_status_empty_when_no_trades(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        status = query_status(db)
        assert status.paper_trade_by_status == {}


# ---------------------------------------------------------------------------
# 5. format_status output
# ---------------------------------------------------------------------------

class TestFormatStatus:
    def _full_status(self, tmp_path: Path) -> DbStatus:
        db = _make_db(tmp_path)
        _insert_snapshots(db, [_snap(1_000_000_000, "okx"),
                                _snap(1_000_001_000, "polymarket")])
        _insert_lag_records(db, [_lag(1_000_002_000)])
        _insert_paper_trades(db, [_paper("closed", pnl=0.10)])
        return query_status(db)

    def test_contains_phase1_disclaimer(self, tmp_path: Path) -> None:
        text = format_status(self._full_status(tmp_path))
        assert "no real trading" in text.lower() or "phase-1" in text.lower()

    def test_contains_db_path(self, tmp_path: Path) -> None:
        status = self._full_status(tmp_path)
        text = format_status(status)
        assert status.db_path in text

    def test_contains_snapshot_total(self, tmp_path: Path) -> None:
        text = format_status(self._full_status(tmp_path))
        assert "2" in text   # 2 total snapshots

    def test_contains_okx_label(self, tmp_path: Path) -> None:
        text = format_status(self._full_status(tmp_path))
        assert "okx" in text.lower()

    def test_contains_polymarket_label(self, tmp_path: Path) -> None:
        text = format_status(self._full_status(tmp_path))
        assert "polymarket" in text.lower()

    def test_contains_lag_records_section(self, tmp_path: Path) -> None:
        text = format_status(self._full_status(tmp_path))
        assert "lag" in text.lower()

    def test_contains_paper_trades_section(self, tmp_path: Path) -> None:
        text = format_status(self._full_status(tmp_path))
        assert "paper" in text.lower()

    def test_empty_db_shows_helpful_hints(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        text = format_status(query_status(db))
        # Should guide the user to run 'scan' first
        assert "scan" in text.lower()

    def test_db_size_shown_when_file_exists(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        text = format_status(query_status(db))
        assert "kb" in text.lower() or "mb" in text.lower()

    def test_db_size_not_found_when_missing(self, tmp_path: Path) -> None:
        db = tmp_path / "missing.db"
        text = format_status(query_status(db))
        assert "not found" in text.lower()
