"""
snapshot_store.py — async SQLite storage for MarketSnapshot rows.

Uses aiosqlite so it can be awaited from the same event loop as the
WebSocket collector without blocking.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import aiosqlite

from src.models import MarketSnapshot

logger = logging.getLogger(__name__)


class SnapshotStore:
    """
    Async context manager that persists MarketSnapshot rows to SQLite.

    The table must already exist (created by db.init_db / schema.sql).
    This class never modifies the schema.

    Usage::

        async with SnapshotStore(db_path) as store:
            await store.insert(snapshot)
            total = await store.count()
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> "SnapshotStore":
        self._conn = await aiosqlite.connect(self._path)
        # WAL mode: faster concurrent reads while writes are in progress.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.commit()
        logger.debug("SnapshotStore connected: %s", self._path)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------

    async def insert(self, snapshot: MarketSnapshot) -> None:
        """Insert one snapshot row. Every call appends a new row; there is no uniqueness
        constraint on the table, so duplicate snapshots (same ts+source+market) will
        result in duplicate rows.  De-duplication is left to the analysis layer."""
        assert self._conn is not None, "Use SnapshotStore as an async context manager"

        raw_json = json.dumps(snapshot.raw, ensure_ascii=False)
        await self._conn.execute(
            """
            INSERT INTO market_snapshots
                (ts_ms, source, market_id, symbol, bid, ask, mid, last,
                 liquidity, volume_24h, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.ts_ms,
                snapshot.source.value,
                snapshot.market_id,
                snapshot.symbol,
                snapshot.bid,
                snapshot.ask,
                snapshot.mid,
                snapshot.last,
                snapshot.liquidity,
                snapshot.volume_24h,
                raw_json,
            ),
        )
        await self._conn.commit()
        logger.debug(
            "Stored snapshot source=%s market=%s ts=%d",
            snapshot.source.value,
            snapshot.market_id,
            snapshot.ts_ms,
        )

    async def count(self) -> int:
        """Return the total number of rows in market_snapshots."""
        assert self._conn is not None
        async with self._conn.execute("SELECT COUNT(*) FROM market_snapshots") as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def recent(self, limit: int = 10) -> list[dict]:
        """Return the *limit* most recent rows as plain dicts (for logging/testing)."""
        assert self._conn is not None
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT ts_ms, source, market_id, bid, ask, last, volume_24h
            FROM market_snapshots
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            cols = [d[0] for d in cur.description]
            async for row in cur:
                rows.append(dict(zip(cols, row)))
        return rows


def ensure_schema(db_path: str | Path) -> None:
    """
    Synchronous helper: run schema.sql against *db_path* if the
    market_snapshots table is missing.  Safe to call multiple times.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    schema_path = Path(__file__).resolve().parents[1] / "schema.sql"
    schema = schema_path.read_text(encoding="utf-8")
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)
        conn.commit()
    logger.debug("Schema ensured at %s", path)
