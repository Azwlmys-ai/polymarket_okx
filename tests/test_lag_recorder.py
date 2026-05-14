"""
tests/test_lag_recorder.py — unit tests for lag recording logic.

All tests are deterministic and use no network or real filesystem I/O.
SQLite tests use in-memory databases.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.lag_recorder import (
    DEFAULT_MAX_LAG_MS,
    DEFAULT_MOVE_THRESHOLD_PCT,
    PriceMove,
    SnapshotRow,
    compute_lag_records,
    detect_moves,
    find_lag,
    insert_lag_records,
    load_snapshots,
)
from src.market_mapper import (
    asset_for_okx_market,
    keywords_for_asset,
    okx_market_ids,
    snapshot_matches_asset,
)
from src.models import LagRecord, MarketSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SCHEMA_SQL = (Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8")


def _in_memory_db() -> sqlite3.Connection:
    """Return an in-memory SQLite connection with the project schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def _okx_snap(ts_ms: int, last: float | None, market_id: str = "BTC-USDT") -> SnapshotRow:
    return SnapshotRow(
        ts_ms=ts_ms,
        source="okx",
        market_id=market_id,
        symbol=market_id,
        last=last,
    )


def _poly_snap(
    ts_ms: int,
    last: float | None = 0.65,
    symbol: str = "Will Bitcoin exceed $100k?",
    market_id: str = "poly-btc-001",
) -> SnapshotRow:
    return SnapshotRow(
        ts_ms=ts_ms,
        source="polymarket",
        market_id=market_id,
        symbol=symbol,
        last=last,
    )


def _lag_record(
    asset: str = "BTC",
    market_id: str = "poly-btc-001",
    exchange_move_ts_ms: int = 1000,
    prediction_response_ts_ms: int = 2000,
) -> LagRecord:
    return LagRecord(
        ts_ms=9_000_000,
        exchange_source=MarketSource.OKX,
        prediction_source=MarketSource.POLYMARKET,
        asset=asset,
        market_id=market_id,
        exchange_move_ts_ms=exchange_move_ts_ms,
        prediction_response_ts_ms=prediction_response_ts_ms,
        lag_ms=prediction_response_ts_ms - exchange_move_ts_ms,
        exchange_price_before=100.0,
        exchange_price_after=101.0,
        prediction_price_before=None,
        prediction_price_after=0.65,
        notes="pct_change=1.0000%",
    )


# ---------------------------------------------------------------------------
# market_mapper
# ---------------------------------------------------------------------------

class TestMarketMapper:
    def test_asset_for_btc_usdt(self):
        assert asset_for_okx_market("BTC-USDT") == "BTC"

    def test_asset_for_eth_usdt(self):
        assert asset_for_okx_market("ETH-USDT") == "ETH"

    def test_asset_for_sol_usdt(self):
        assert asset_for_okx_market("SOL-USDT") == "SOL"

    def test_asset_for_unknown_market_returns_none(self):
        assert asset_for_okx_market("UNKNOWN-USDT") is None

    def test_keywords_for_btc(self):
        kws = keywords_for_asset("BTC")
        assert "BTC" in kws
        assert "bitcoin" in kws

    def test_keywords_for_unknown_asset_returns_empty_list(self):
        assert keywords_for_asset("DOGE") == []

    def test_okx_market_ids_contains_expected(self):
        ids = okx_market_ids()
        assert "BTC-USDT" in ids
        assert "ETH-USDT" in ids
        assert "SOL-USDT" in ids

    def test_prefixed_market_id_returns_none(self):
        """F-4: prefixed IDs such as 'okx:BTC-USDT' must NOT match _ASSET_MAP.

        Using a prefixed market_id silently produces 0 lag records because
        _asset_from_okx_market_id falls back to split('-')[0] = 'okx:BTC',
        which matches no Polymarket keyword list.  Callers must use bare
        instrument IDs (e.g. 'BTC-USDT').
        """
        assert asset_for_okx_market("okx:BTC-USDT") is None
        assert asset_for_okx_market("okx:ETH-USDT") is None

    def test_snapshot_matches_btc_keyword(self):
        assert snapshot_matches_asset("Will Bitcoin exceed $100k?", ["BTC", "bitcoin"]) is True

    def test_snapshot_matches_case_insensitive(self):
        assert snapshot_matches_asset("BITCOIN price prediction", ["bitcoin"]) is True

    def test_snapshot_no_match_returns_false(self):
        assert snapshot_matches_asset("Will the US win the World Cup?", ["BTC", "bitcoin"]) is False

    def test_snapshot_none_symbol_returns_false(self):
        assert snapshot_matches_asset(None, ["BTC"]) is False

    def test_snapshot_empty_symbol_returns_false(self):
        assert snapshot_matches_asset("", ["BTC"]) is False


# ---------------------------------------------------------------------------
# detect_moves
# ---------------------------------------------------------------------------

class TestDetectMoves:
    def test_empty_list_returns_empty(self):
        assert detect_moves([]) == []

    def test_single_snapshot_returns_empty(self):
        assert detect_moves([_okx_snap(1000, 100.0)]) == []

    def test_no_move_below_threshold(self):
        # 0.1% change vs default 0.5% threshold
        snaps = [_okx_snap(1000, 100.0), _okx_snap(2000, 100.1)]
        assert detect_moves(snaps) == []

    def test_move_above_threshold_detected(self):
        # 1% move, well above 0.5% default
        snaps = [_okx_snap(1000, 100.0), _okx_snap(2000, 101.0)]
        moves = detect_moves(snaps)
        assert len(moves) == 1
        assert moves[0].asset == "BTC"
        assert moves[0].price_before == pytest.approx(100.0)
        assert moves[0].price_after == pytest.approx(101.0)
        assert moves[0].pct_change == pytest.approx(0.01)
        assert moves[0].ts_ms == 2000

    def test_move_is_absolute_detects_down_move(self):
        # price drops 1%
        snaps = [_okx_snap(1000, 100.0), _okx_snap(2000, 99.0)]
        moves = detect_moves(snaps)
        assert len(moves) == 1
        assert moves[0].pct_change == pytest.approx(0.01)

    def test_custom_threshold_respected(self):
        # 0.3% move; passes 0.2% threshold but not 0.5% default
        snaps = [_okx_snap(1000, 100.0), _okx_snap(2000, 100.3)]
        assert detect_moves(snaps, threshold_pct=0.005) == []
        assert len(detect_moves(snaps, threshold_pct=0.002)) == 1

    def test_multiple_consecutive_moves_detected(self):
        snaps = [
            _okx_snap(1000, 100.0),
            _okx_snap(2000, 101.0),   # +1% move
            _okx_snap(3000, 101.5),   # +0.495% — below default threshold
            _okx_snap(4000, 106.0),   # +4.43% move
        ]
        moves = detect_moves(snaps)
        assert len(moves) == 2
        assert moves[0].ts_ms == 2000
        assert moves[1].ts_ms == 4000

    def test_skips_none_last_price(self):
        snaps = [_okx_snap(1000, None), _okx_snap(2000, 101.0)]
        assert detect_moves(snaps) == []

    def test_skips_zero_price_before(self):
        snaps = [_okx_snap(1000, 0.0), _okx_snap(2000, 101.0)]
        assert detect_moves(snaps) == []

    def test_move_asset_derived_from_market_id(self):
        snaps = [
            _okx_snap(1000, 2000.0, market_id="ETH-USDT"),
            _okx_snap(2000, 2100.0, market_id="ETH-USDT"),
        ]
        moves = detect_moves(snaps)
        assert len(moves) == 1
        assert moves[0].asset == "ETH"

    def test_exact_threshold_boundary_triggers_move(self):
        # Exactly at threshold should trigger
        snaps = [_okx_snap(1000, 100.0), _okx_snap(2000, 100.5)]
        moves = detect_moves(snaps, threshold_pct=0.005)
        assert len(moves) == 1


# ---------------------------------------------------------------------------
# find_lag
# ---------------------------------------------------------------------------

class TestFindLag:
    def _btc_move(self, ts_ms: int = 1000) -> PriceMove:
        return PriceMove(
            asset="BTC",
            market_id="BTC-USDT",
            ts_ms=ts_ms,
            price_before=100.0,
            price_after=101.0,
            pct_change=0.01,
        )

    def test_matching_poly_snap_returns_lag_record(self):
        move = self._btc_move(ts_ms=1000)
        poly = [_poly_snap(ts_ms=2000)]
        record = find_lag(move, poly)
        assert record is not None
        assert record.lag_ms == 1000
        assert record.exchange_move_ts_ms == 1000
        assert record.prediction_response_ts_ms == 2000
        assert record.asset == "BTC"

    def test_poly_snap_at_or_before_move_not_used(self):
        move = self._btc_move(ts_ms=1000)
        poly = [_poly_snap(ts_ms=500), _poly_snap(ts_ms=1000)]
        assert find_lag(move, poly) is None

    def test_poly_snap_beyond_max_lag_not_used(self):
        move = self._btc_move(ts_ms=1000)
        poly = [_poly_snap(ts_ms=1000 + DEFAULT_MAX_LAG_MS + 1)]
        assert find_lag(move, poly) is None

    def test_poly_snap_at_exact_max_lag_boundary_is_included(self):
        # The break condition is strict (> max_lag_ms), so a snap exactly at
        # max_lag_ms is still within the window and should be returned.
        move = self._btc_move(ts_ms=1000)
        poly = [_poly_snap(ts_ms=1000 + DEFAULT_MAX_LAG_MS)]
        record = find_lag(move, poly, max_lag_ms=DEFAULT_MAX_LAG_MS)
        assert record is not None
        assert record.lag_ms == DEFAULT_MAX_LAG_MS

    def test_keyword_mismatch_snap_skipped(self):
        move = self._btc_move(ts_ms=1000)
        poly = [_poly_snap(ts_ms=2000, symbol="Will Solana reach $500?")]
        assert find_lag(move, poly) is None

    def test_first_matching_snap_used_not_second(self):
        move = self._btc_move(ts_ms=1000)
        poly = [
            _poly_snap(ts_ms=1500, market_id="poly-btc-first"),
            _poly_snap(ts_ms=2500, market_id="poly-btc-second"),
        ]
        record = find_lag(move, poly)
        assert record is not None
        assert record.market_id == "poly-btc-first"
        assert record.lag_ms == 500

    def test_custom_max_lag_respected(self):
        move = self._btc_move(ts_ms=1000)
        poly = [_poly_snap(ts_ms=6000)]   # 5000 ms lag
        assert find_lag(move, poly, max_lag_ms=3000) is None
        assert find_lag(move, poly, max_lag_ms=10000) is not None

    def test_prediction_price_after_stored(self):
        move = self._btc_move(ts_ms=1000)
        poly = [_poly_snap(ts_ms=2000, last=0.72)]
        record = find_lag(move, poly)
        assert record is not None
        assert record.prediction_price_after == pytest.approx(0.72)

    def test_prediction_price_before_is_none(self):
        move = self._btc_move(ts_ms=1000)
        record = find_lag(move, [_poly_snap(ts_ms=2000)])
        assert record is not None
        assert record.prediction_price_before is None

    def test_notes_contain_pct_change(self):
        move = self._btc_move(ts_ms=1000)
        record = find_lag(move, [_poly_snap(ts_ms=2000)])
        assert record is not None
        assert "pct_change" in (record.notes or "")

    def test_unknown_asset_falls_back_to_asset_keyword(self):
        move = PriceMove(
            asset="DOGE",
            market_id="DOGE-USDT",
            ts_ms=1000,
            price_before=0.10,
            price_after=0.11,
            pct_change=0.10,
        )
        poly = [_poly_snap(ts_ms=2000, symbol="Will DOGE hit $1?")]
        record = find_lag(move, poly)
        assert record is not None


# ---------------------------------------------------------------------------
# compute_lag_records (integration of detect_moves + find_lag)
# ---------------------------------------------------------------------------

class TestComputeLagRecords:
    def test_no_okx_rows_returns_empty(self):
        poly = [_poly_snap(ts_ms=2000)]
        assert compute_lag_records([], poly) == []

    def test_no_poly_rows_returns_empty(self):
        okx = [_okx_snap(1000, 100.0), _okx_snap(2000, 101.0)]
        assert compute_lag_records(okx, []) == []

    def test_end_to_end_produces_lag_record(self):
        okx = [_okx_snap(1000, 100.0), _okx_snap(2000, 101.0)]  # 1% move at t=2000
        poly = [_poly_snap(ts_ms=3000)]                           # poly responds at t=3000
        records = compute_lag_records(okx, poly)
        assert len(records) == 1
        assert records[0].lag_ms == 1000
        assert records[0].asset == "BTC"

    def test_no_move_below_threshold_no_records(self):
        okx = [_okx_snap(1000, 100.0), _okx_snap(2000, 100.1)]  # 0.1% < 0.5% threshold
        poly = [_poly_snap(ts_ms=3000)]
        assert compute_lag_records(okx, poly) == []

    def test_poly_snap_before_move_not_used(self):
        okx = [_okx_snap(5000, 100.0), _okx_snap(6000, 101.0)]
        poly = [_poly_snap(ts_ms=3000)]   # before the move
        assert compute_lag_records(okx, poly) == []

    def test_multiple_assets_processed_independently(self):
        okx = [
            _okx_snap(1000, 100.0, "BTC-USDT"),
            _okx_snap(2000, 101.0, "BTC-USDT"),
            _okx_snap(1000, 2000.0, "ETH-USDT"),
            _okx_snap(2000, 2100.0, "ETH-USDT"),
        ]
        poly = [
            _poly_snap(3000, symbol="Will Bitcoin exceed $100k?", market_id="btc-m"),
            _poly_snap(3000, symbol="Will Ethereum hit $5000?", market_id="eth-m"),
        ]
        records = compute_lag_records(okx, poly)
        assets = {r.asset for r in records}
        assert "BTC" in assets
        assert "ETH" in assets

    def test_exchange_price_fields_stored_correctly(self):
        okx = [_okx_snap(1000, 100.0), _okx_snap(2000, 102.0)]
        poly = [_poly_snap(ts_ms=3000)]
        records = compute_lag_records(okx, poly)
        assert len(records) == 1
        assert records[0].exchange_price_before == pytest.approx(100.0)
        assert records[0].exchange_price_after == pytest.approx(102.0)


# ---------------------------------------------------------------------------
# SQLite persistence: insert_lag_records + load_snapshots
# ---------------------------------------------------------------------------

class TestSqlitePersistence:
    def test_insert_lag_records_returns_count(self, tmp_path: Path):
        db_file = tmp_path / "test.db"
        with sqlite3.connect(db_file) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
        inserted = insert_lag_records(str(db_file), [_lag_record()])
        assert inserted == 1

    def test_insert_empty_list_returns_zero(self, tmp_path: Path):
        db_file = tmp_path / "test.db"
        with sqlite3.connect(db_file) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
        assert insert_lag_records(str(db_file), []) == 0

    def test_insert_multiple_records(self, tmp_path: Path):
        db_file = tmp_path / "test.db"
        with sqlite3.connect(db_file) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
        records = [_lag_record(market_id=f"poly-m-{i}") for i in range(5)]
        inserted = insert_lag_records(str(db_file), records)
        assert inserted == 5

    def test_load_snapshots_empty_db_returns_empty(self, tmp_path: Path):
        db_file = tmp_path / "test.db"
        with sqlite3.connect(db_file) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
        rows = load_snapshots(db_file, "okx")
        assert rows == []

    def test_load_snapshots_returns_inserted_rows(self, tmp_path: Path):
        db_file = tmp_path / "test.db"
        with sqlite3.connect(db_file) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                """INSERT INTO market_snapshots
                   (ts_ms, source, market_id, symbol, bid, ask, mid, last,
                    liquidity, volume_24h, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (1000, "okx", "BTC-USDT", "BTC-USDT", None, None, 100.0, 100.0, None, None, "{}"),
            )
            conn.commit()
        rows = load_snapshots(db_file, "okx")
        assert len(rows) == 1
        assert rows[0].ts_ms == 1000
        assert rows[0].source == "okx"
        assert rows[0].market_id == "BTC-USDT"
        assert rows[0].last == pytest.approx(100.0)

    def test_load_snapshots_filters_by_source(self, tmp_path: Path):
        db_file = tmp_path / "test.db"
        with sqlite3.connect(db_file) as conn:
            conn.executescript(SCHEMA_SQL)
            for source in ("okx", "polymarket", "okx"):
                conn.execute(
                    """INSERT INTO market_snapshots
                       (ts_ms, source, market_id, symbol, bid, ask, mid, last,
                        liquidity, volume_24h, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (1000, source, "mkt", "mkt", None, None, 1.0, 1.0, None, None, "{}"),
                )
            conn.commit()
        okx_rows = load_snapshots(db_file, "okx")
        poly_rows = load_snapshots(db_file, "polymarket")
        assert len(okx_rows) == 2
        assert len(poly_rows) == 1

    def test_load_snapshots_nonexistent_db_returns_empty(self, tmp_path: Path):
        rows = load_snapshots(tmp_path / "nonexistent.db", "okx")
        assert rows == []

    def test_lag_fields_round_trip(self, tmp_path: Path):
        db_file = tmp_path / "test.db"
        with sqlite3.connect(db_file) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
        record = _lag_record(exchange_move_ts_ms=5000, prediction_response_ts_ms=8000)
        insert_lag_records(str(db_file), [record])
        with sqlite3.connect(db_file) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM lag_records").fetchone()
        assert row["lag_ms"] == 3000
        assert row["asset"] == "BTC"
        assert row["exchange_source"] == "okx"
        assert row["prediction_source"] == "polymarket"
        assert row["exchange_price_before"] == pytest.approx(100.0)
        assert row["exchange_price_after"] == pytest.approx(101.0)
        assert row["prediction_price_before"] is None
        assert row["notes"] is not None
