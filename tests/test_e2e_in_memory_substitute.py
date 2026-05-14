"""
Non-E2E in-memory integration substitute for the Phase 1 lineage.

This test intentionally uses a single SQLite :memory: connection and seeded
synthetic rows. It is not an official file-backed E2E smoke test and makes no
claim about live data collection, real trading, or real profitability.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.evaluator import compute_eval_metrics
from src.lag_recorder import SnapshotRow, compute_lag_records
from src.models import LagRecord
from src.paper_trader import SimConfig, SimTrade, run_paper_simulation


SCHEMA_SQL = (Path(__file__).resolve().parents[1] / "schema.sql").read_text(
    encoding="utf-8"
)


def _insert_seed_snapshots(conn: sqlite3.Connection) -> None:
    rows = [
        (1_000, "okx", "BTC-USDT", "BTC-USDT", 100.0),
        (2_000, "okx", "BTC-USDT", "BTC-USDT", 101.0),
        (2_500, "polymarket", "poly-btc-001", "Will Bitcoin exceed 100k?", 0.65),
        (302_501, "polymarket", "poly-btc-001", "Will Bitcoin exceed 100k?", 0.70),
    ]
    conn.executemany(
        """
        INSERT INTO market_snapshots
            (ts_ms, source, market_id, symbol, last, raw_json)
        VALUES (?, ?, ?, ?, ?, '{}')
        """,
        rows,
    )
    conn.commit()


def _load_snapshots(conn: sqlite3.Connection, source: str) -> list[SnapshotRow]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT ts_ms, source, market_id, symbol, last
        FROM market_snapshots
        WHERE source = ?
        ORDER BY ts_ms ASC
        """,
        (source,),
    ).fetchall()
    return [
        SnapshotRow(
            ts_ms=int(row["ts_ms"]),
            source=str(row["source"]),
            market_id=str(row["market_id"]),
            symbol=row["symbol"],
            last=float(row["last"]) if row["last"] is not None else None,
        )
        for row in rows
    ]


def _insert_lag_records(conn: sqlite3.Connection, records: list[LagRecord]) -> None:
    conn.executemany(
        """
        INSERT INTO lag_records
            (ts_ms, exchange_source, prediction_source, asset, market_id,
             exchange_move_ts_ms, prediction_response_ts_ms, lag_ms,
             exchange_price_before, exchange_price_after,
             prediction_price_before, prediction_price_after, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                record.ts_ms,
                record.exchange_source.value,
                record.prediction_source.value,
                record.asset,
                record.market_id,
                record.exchange_move_ts_ms,
                record.prediction_response_ts_ms,
                record.lag_ms,
                record.exchange_price_before,
                record.exchange_price_after,
                record.prediction_price_before,
                record.prediction_price_after,
                record.notes,
            )
            for record in records
        ],
    )
    conn.commit()


def _lag_rows(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT ts_ms, exchange_source, prediction_source, asset, market_id,
               exchange_move_ts_ms, prediction_response_ts_ms, lag_ms,
               exchange_price_before, exchange_price_after,
               prediction_price_before, prediction_price_after, notes
        FROM lag_records
        ORDER BY exchange_move_ts_ms ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _poly_snapshot_rows(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT ts_ms, source, market_id, symbol, last
        FROM market_snapshots
        WHERE source = 'polymarket'
        ORDER BY ts_ms ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _insert_paper_trades(conn: sqlite3.Connection, trades: list[SimTrade]) -> None:
    conn.executemany(
        """
        INSERT INTO paper_trades
            (opened_ts_ms, closed_ts_ms, market_id, asset, side,
             entry_price, exit_price, notional, quantity,
             fees, slippage, pnl, status, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                trade.opened_ts_ms,
                trade.closed_ts_ms,
                trade.market_id,
                trade.asset,
                trade.side,
                trade.entry_price,
                trade.exit_price,
                trade.notional,
                trade.quantity,
                trade.fees,
                trade.slippage_cost,
                trade.pnl,
                trade.status,
                trade.reason,
            )
            for trade in trades
            if trade.status in {"closed", "open_no_exit"}
        ],
    )
    conn.commit()


def _paper_rows(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, opened_ts_ms, closed_ts_ms, market_id, asset, side,
               entry_price, exit_price, notional, quantity, fees, slippage,
               pnl, status, reason
        FROM paper_trades
        ORDER BY opened_ts_ms ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def test_phase1_lineage_in_memory_substitute_is_not_official_e2e() -> None:
    """Synthetic in-memory lineage check: snapshots -> lag -> paper -> evaluation."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    _insert_seed_snapshots(conn)

    okx_rows = _load_snapshots(conn, "okx")
    poly_rows = _load_snapshots(conn, "polymarket")

    lag_records = compute_lag_records(
        okx_rows,
        poly_rows,
        threshold_pct=0.005,
        max_lag_ms=60_000,
    )
    _insert_lag_records(conn, lag_records)

    stored_lag_rows = _lag_rows(conn)
    trades, final_cash = run_paper_simulation(
        stored_lag_rows,
        _poly_snapshot_rows(conn),
        SimConfig(initial_cash=100.0, hold_window_ms=300_000),
    )
    _insert_paper_trades(conn, trades)

    metrics = compute_eval_metrics(_paper_rows(conn), db_path=":memory:")

    assert len(lag_records) == 1
    assert stored_lag_rows[0]["market_id"] == "poly-btc-001"
    assert stored_lag_rows[0]["exchange_move_ts_ms"] == 2_000
    assert stored_lag_rows[0]["prediction_response_ts_ms"] == 2_500
    assert stored_lag_rows[0]["lag_ms"] == 500

    assert len(trades) == 1
    assert trades[0].market_id == stored_lag_rows[0]["market_id"]
    assert trades[0].opened_ts_ms == stored_lag_rows[0]["prediction_response_ts_ms"]
    assert trades[0].closed_ts_ms == 302_501
    assert trades[0].status == "closed"

    assert metrics.db_path == ":memory:"
    assert metrics.total_rows == 1
    assert metrics.closed_count == 1
    assert metrics.by_status == {"closed": 1}
    assert metrics.net_pnl == pytest.approx(trades[0].pnl)
    assert final_cash == pytest.approx(100.0 + (trades[0].pnl or 0.0))
    assert "NOT proof of real profitability" in metrics.disclaimer
