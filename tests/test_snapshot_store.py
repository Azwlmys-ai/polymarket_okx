"""
tests/test_snapshot_store.py — tests for async SQLite snapshot persistence.

Uses a real temporary SQLite file (not mocks) to verify the actual SQL
path works end-to-end.
"""
from __future__ import annotations

import json
import time

import pytest
import pytest_asyncio

from src.models import MarketSnapshot, MarketSource
from src.snapshot_store import SnapshotStore, ensure_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    """Provide a fresh in-directory SQLite path with schema applied."""
    path = tmp_path / "test_research.db"
    ensure_schema(path)
    return path


def _make_snapshot(
    symbol: str = "BTC-USDT",
    last: float = 43000.5,
    bid: float = 43000.0,
    ask: float = 43001.0,
    ts_ms: int | None = None,
) -> MarketSnapshot:
    ts = ts_ms or int(time.time() * 1000)
    return MarketSnapshot(
        ts_ms=ts,
        source=MarketSource.OKX,
        market_id=symbol,
        symbol=symbol,
        bid=bid,
        ask=ask,
        mid=round((bid + ask) / 2, 8),
        last=last,
        liquidity=0.3,
        volume_24h=8888.0,
        raw={"instId": symbol, "last": str(last), "bidPx": str(bid), "askPx": str(ask)},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_increases_count(db_path):
    async with SnapshotStore(db_path) as store:
        assert await store.count() == 0
        await store.insert(_make_snapshot())
        assert await store.count() == 1
        await store.insert(_make_snapshot("ETH-USDT"))
        assert await store.count() == 2


@pytest.mark.asyncio
async def test_recent_returns_latest_rows(db_path):
    base_ts = int(time.time() * 1000)
    snaps = [_make_snapshot(ts_ms=base_ts + i * 1000) for i in range(5)]
    async with SnapshotStore(db_path) as store:
        for s in snaps:
            await store.insert(s)
        rows = await store.recent(3)

    assert len(rows) == 3
    # Most recent first
    assert rows[0]["ts_ms"] > rows[1]["ts_ms"]


@pytest.mark.asyncio
async def test_insert_preserves_all_numeric_fields(db_path):
    import sqlite3

    snap = _make_snapshot(last=99999.99, bid=99998.0, ask=100000.0)
    async with SnapshotStore(db_path) as store:
        await store.insert(snap)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT ts_ms, source, market_id, bid, ask, last, volume_24h, raw_json"
            " FROM market_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert row is not None
    ts_ms, source, market_id, bid, ask, last, vol, raw_json = row
    assert source == "okx"
    assert market_id == "BTC-USDT"
    assert abs(bid - 99998.0) < 1e-6
    assert abs(ask - 100000.0) < 1e-6
    assert abs(last - 99999.99) < 1e-6
    raw = json.loads(raw_json)
    assert raw.get("instId") == "BTC-USDT"


@pytest.mark.asyncio
async def test_multiple_symbols_stored_independently(db_path):
    async with SnapshotStore(db_path) as store:
        for sym in ("BTC-USDT", "ETH-USDT", "SOL-USDT"):
            await store.insert(_make_snapshot(sym))
        assert await store.count() == 3


@pytest.mark.asyncio
async def test_ensure_schema_is_idempotent(tmp_path):
    """Calling ensure_schema twice must not raise or corrupt data."""
    path = tmp_path / "idempotent.db"
    ensure_schema(path)
    ensure_schema(path)
    async with SnapshotStore(path) as store:
        await store.insert(_make_snapshot())
        assert await store.count() == 1
