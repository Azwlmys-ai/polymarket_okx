"""
lag_recorder.py — offline lag recording from collected market_snapshots.

Reads OKX and Polymarket snapshots from SQLite, detects OKX price moves above
a configurable threshold, and records the lag until the first matching
Polymarket snapshot is found after each move.

No network calls.  No trading signals.  No strategy.  Phase-1 only.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from src.market_mapper import asset_for_okx_market, keywords_for_asset, snapshot_matches_asset
from src.models import LagRecord, MarketSource

logger = logging.getLogger(__name__)

# Default thresholds — these are conservative MVP values.
# Tune them via CLI flags; environment variables are not needed for Phase 1.
DEFAULT_MOVE_THRESHOLD_PCT: float = 0.005   # 0.5% price change triggers a lag search
DEFAULT_MAX_LAG_MS: int = 60_000            # look at most 60 s ahead for a Poly response


# ---------------------------------------------------------------------------
# Internal data types (plain frozen dataclasses — no Pydantic; fast to test)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SnapshotRow:
    """Minimal projection of a market_snapshots row for lag analysis."""
    ts_ms: int
    source: str           # "okx" or "polymarket"
    market_id: str
    symbol: str | None
    last: float | None


@dataclass(frozen=True)
class PriceMove:
    """A detected OKX price move between two consecutive snapshots."""
    asset: str
    market_id: str
    ts_ms: int            # timestamp of the snapshot that crossed the threshold
    price_before: float
    price_after: float
    pct_change: float     # absolute fractional change (e.g. 0.008 = 0.8%)


# ---------------------------------------------------------------------------
# Pure analysis helpers (no I/O — unit-testable)
# ---------------------------------------------------------------------------

def _asset_from_okx_market_id(market_id: str) -> str:
    """Return canonical asset name; falls back to the instrument prefix (e.g. 'BTC').

    *market_id* must be a **bare** OKX instrument ID such as ``"BTC-USDT"``.
    Prefixed IDs (e.g. ``"okx:BTC-USDT"``) do not match ``_ASSET_MAP`` and
    trigger the split-based fallback, which produces ``"okx:BTC"`` — a value
    that will never match any Polymarket keyword list, resulting in 0 lag
    records without any error.  Always use bare instrument IDs.
    """
    return asset_for_okx_market(market_id) or market_id.split("-")[0]


def detect_moves(
    snapshots: list[SnapshotRow],
    threshold_pct: float = DEFAULT_MOVE_THRESHOLD_PCT,
) -> list[PriceMove]:
    """
    Detect consecutive price moves >= threshold_pct in a sorted snapshot list.

    *snapshots* must be sorted by ts_ms ascending and belong to a single OKX market.
    Pairs with None or zero prices are skipped.
    Returns one PriceMove per qualifying consecutive pair.
    """
    moves: list[PriceMove] = []
    for i in range(1, len(snapshots)):
        prev = snapshots[i - 1]
        curr = snapshots[i]
        if prev.last is None or curr.last is None or prev.last == 0.0:
            continue
        pct = abs(curr.last - prev.last) / prev.last
        if pct >= threshold_pct:
            moves.append(
                PriceMove(
                    asset=_asset_from_okx_market_id(curr.market_id),
                    market_id=curr.market_id,
                    ts_ms=curr.ts_ms,
                    price_before=prev.last,
                    price_after=curr.last,
                    pct_change=pct,
                )
            )
    return moves


def find_lag(
    move: PriceMove,
    poly_snapshots: list[SnapshotRow],
    max_lag_ms: int = DEFAULT_MAX_LAG_MS,
) -> LagRecord | None:
    """
    Find the first Polymarket snapshot after *move.ts_ms* that matches the asset.

    *poly_snapshots* must be sorted by ts_ms ascending and may contain any market.
    Returns a LagRecord if a matching snapshot is found within max_lag_ms,
    otherwise None.
    """
    keywords = keywords_for_asset(move.asset)
    if not keywords:
        # Unknown asset — use the asset name as a single-keyword fallback
        keywords = [move.asset]

    for snap in poly_snapshots:
        if snap.ts_ms <= move.ts_ms:
            continue
        if snap.ts_ms - move.ts_ms > max_lag_ms:
            break  # list is sorted → no later snapshot will qualify
        if not snapshot_matches_asset(snap.symbol, keywords):
            continue

        lag_ms = snap.ts_ms - move.ts_ms
        return LagRecord(
            ts_ms=int(time.time() * 1000),
            exchange_source=MarketSource.OKX,
            prediction_source=MarketSource.POLYMARKET,
            asset=move.asset,
            market_id=snap.market_id,
            exchange_move_ts_ms=move.ts_ms,
            prediction_response_ts_ms=snap.ts_ms,
            lag_ms=lag_ms,
            exchange_price_before=move.price_before,
            exchange_price_after=move.price_after,
            prediction_price_before=None,   # not available from snapshot history
            prediction_price_after=snap.last,
            notes=f"pct_change={move.pct_change:.4%}",
        )

    return None


def compute_lag_records(
    okx_rows: list[SnapshotRow],
    poly_rows: list[SnapshotRow],
    threshold_pct: float = DEFAULT_MOVE_THRESHOLD_PCT,
    max_lag_ms: int = DEFAULT_MAX_LAG_MS,
) -> list[LagRecord]:
    """
    Full in-memory pipeline: detect OKX moves → find Polymarket responses.

    *okx_rows* and *poly_rows* must each be sorted by ts_ms ascending.
    *poly_rows* may contain snapshots for any Polymarket market; keyword
    matching filters for the relevant asset.

    Returns a list of LagRecord objects (no database I/O here).
    """
    results: list[LagRecord] = []

    # Group OKX rows by market_id so each asset is processed independently
    markets: dict[str, list[SnapshotRow]] = {}
    for row in okx_rows:
        markets.setdefault(row.market_id, []).append(row)

    for market_id, market_rows in markets.items():
        market_rows_sorted = sorted(market_rows, key=lambda r: r.ts_ms)
        moves = detect_moves(market_rows_sorted, threshold_pct)
        logger.debug("%s: %d move(s) detected (threshold=%.2f%%)", market_id, len(moves), threshold_pct * 100)
        for move in moves:
            record = find_lag(move, poly_rows, max_lag_ms)
            if record is not None:
                results.append(record)
                logger.debug(
                    "Lag recorded: asset=%s lag_ms=%d exchange_move_ts=%d",
                    record.asset,
                    record.lag_ms,
                    record.exchange_move_ts_ms,
                )

    return results


# ---------------------------------------------------------------------------
# SQLite I/O helpers
# ---------------------------------------------------------------------------

def load_snapshots(db_path: str | Path, source: str) -> list[SnapshotRow]:
    """
    Load all snapshots for *source* from market_snapshots, sorted by ts_ms asc.

    Returns an empty list if the table is empty or the DB does not exist.
    """
    path = str(db_path)
    rows: list[SnapshotRow] = []
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT ts_ms, source, market_id, symbol, last
                FROM market_snapshots
                WHERE source = ?
                ORDER BY ts_ms ASC
                """,
                (source,),
            )
            for row in cur:
                rows.append(
                    SnapshotRow(
                        ts_ms=int(row["ts_ms"]),
                        source=str(row["source"]),
                        market_id=str(row["market_id"]),
                        symbol=row["symbol"],
                        last=float(row["last"]) if row["last"] is not None else None,
                    )
                )
    except sqlite3.OperationalError as exc:
        logger.warning("Could not load %s snapshots: %s", source, exc)
        return []

    logger.info("Loaded %d %s snapshot(s) from %s", len(rows), source, path)
    return rows


def insert_lag_records(db_path: str | Path, records: list[LagRecord]) -> int:
    """
    Insert LagRecord objects into lag_records table.
    Returns the number of rows inserted.
    """
    if not records:
        return 0
    path = str(db_path)
    with sqlite3.connect(path) as conn:
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
                    r.ts_ms,
                    r.exchange_source.value,
                    r.prediction_source.value,
                    r.asset,
                    r.market_id,
                    r.exchange_move_ts_ms,
                    r.prediction_response_ts_ms,
                    r.lag_ms,
                    r.exchange_price_before,
                    r.exchange_price_after,
                    r.prediction_price_before,
                    r.prediction_price_after,
                    r.notes,
                )
                for r in records
            ],
        )
        conn.commit()
    logger.info("Inserted %d lag record(s) into %s", len(records), path)
    return len(records)


def run_lag_recording(
    db_path: str | Path,
    threshold_pct: float = DEFAULT_MOVE_THRESHOLD_PCT,
    max_lag_ms: int = DEFAULT_MAX_LAG_MS,
) -> int:
    """
    Full offline lag recording pipeline:
      1. Load OKX and Polymarket snapshots from SQLite.
      2. Detect price moves and compute lag records.
      3. Persist results to lag_records table.

    Returns the number of lag records inserted.
    """
    okx_rows = load_snapshots(db_path, "okx")
    poly_rows = load_snapshots(db_path, "polymarket")

    if not okx_rows:
        logger.warning("No OKX snapshots in DB. Run 'scan --source okx' first.")
        return 0
    if not poly_rows:
        logger.warning("No Polymarket snapshots in DB. Run 'scan --source polymarket' first.")
        return 0

    records = compute_lag_records(okx_rows, poly_rows, threshold_pct, max_lag_ms)
    logger.info("Lag analysis complete: %d lag record(s) found", len(records))
    return insert_lag_records(db_path, records)
