"""
db_status.py — read-only DB summary for the `status` CLI command.

Queries the local SQLite database and returns a structured snapshot of:
  - market_snapshots count (total and per-source) with latest timestamp
  - lag_records count with latest timestamp
  - paper_trades count by status

No network calls.  No writes.  No trading signals.  Phase-1 only.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SourceStats:
    """Snapshot counts for one data source (e.g. 'okx' or 'polymarket')."""
    count: int
    latest_ts_ms: int | None   # None if no rows exist


@dataclass
class SnapshotStats:
    """Summary of the market_snapshots table."""
    total: int
    by_source: dict[str, SourceStats] = field(default_factory=dict)


@dataclass
class DbStatus:
    """Complete DB status snapshot."""
    queried_at: str                    # ISO-8601 UTC
    db_path: str
    db_size_bytes: int | None          # None if DB file does not exist on disk
    snapshots: SnapshotStats
    lag_record_count: int
    lag_latest_ts_ms: int | None
    paper_trade_count: int
    paper_trade_by_status: dict[str, int]   # {status_label: count}


# ---------------------------------------------------------------------------
# Query helpers (pure SQLite reads)
# ---------------------------------------------------------------------------

def _ms_to_utc_str(ts_ms: int | None) -> str:
    """Format a millisecond epoch timestamp as a human-readable UTC string."""
    if ts_ms is None:
        return "—"
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OSError, OverflowError, ValueError):
        return f"ts={ts_ms}"


def query_status(db_path: str | Path) -> DbStatus:
    """
    Query the SQLite DB at *db_path* and return a DbStatus.

    Returns zeroed counts if the DB does not exist or tables are missing.
    Never raises — errors are swallowed and reflected as empty counts.
    """
    path = Path(db_path)
    queried_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # DB file size (None if file does not exist)
    try:
        db_size_bytes: int | None = path.stat().st_size if path.exists() else None
    except OSError:
        db_size_bytes = None

    # Defaults — used if DB is missing or tables do not exist
    snapshots = SnapshotStats(total=0)
    lag_count = 0
    lag_latest: int | None = None
    paper_count = 0
    paper_by_status: dict[str, int] = {}

    try:
        with sqlite3.connect(str(path)) as conn:
            conn.row_factory = sqlite3.Row

            # --- market_snapshots ---
            try:
                total_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM market_snapshots"
                ).fetchone()
                total = int(total_row["n"]) if total_row else 0

                by_source_rows = conn.execute(
                    """
                    SELECT source,
                           COUNT(*)      AS n,
                           MAX(ts_ms)    AS latest_ts_ms
                    FROM market_snapshots
                    GROUP BY source
                    """
                ).fetchall()

                by_source: dict[str, SourceStats] = {}
                for row in by_source_rows:
                    by_source[row["source"]] = SourceStats(
                        count=int(row["n"]),
                        latest_ts_ms=row["latest_ts_ms"],
                    )

                snapshots = SnapshotStats(total=total, by_source=by_source)
            except sqlite3.OperationalError:
                pass   # table missing — leave default

            # --- lag_records ---
            try:
                lag_row = conn.execute(
                    "SELECT COUNT(*) AS n, MAX(ts_ms) AS latest FROM lag_records"
                ).fetchone()
                if lag_row:
                    lag_count = int(lag_row["n"])
                    lag_latest = lag_row["latest"]
            except sqlite3.OperationalError:
                pass

            # --- paper_trades ---
            try:
                paper_total_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM paper_trades"
                ).fetchone()
                paper_count = int(paper_total_row["n"]) if paper_total_row else 0

                status_rows = conn.execute(
                    """
                    SELECT status, COUNT(*) AS n
                    FROM paper_trades
                    GROUP BY status
                    """
                ).fetchall()
                paper_by_status = {row["status"]: int(row["n"]) for row in status_rows}
            except sqlite3.OperationalError:
                pass

    except sqlite3.Error:
        pass   # entire DB unreadable — return zeros

    return DbStatus(
        queried_at=queried_at,
        db_path=str(path),
        db_size_bytes=db_size_bytes,
        snapshots=snapshots,
        lag_record_count=lag_count,
        lag_latest_ts_ms=lag_latest,
        paper_trade_count=paper_count,
        paper_trade_by_status=paper_by_status,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_status(status: DbStatus) -> str:
    """Render a human-readable DB status report for console output."""
    lines: list[str] = []
    sep = "─" * 56

    lines.append(sep)
    lines.append("  Polymarket × OKX — Database Status")
    lines.append("  (Phase-1 research only — no real trading)")
    lines.append(sep)
    lines.append(f"  Queried at : {status.queried_at}")
    lines.append(f"  DB path    : {status.db_path}")

    if status.db_size_bytes is None:
        lines.append("  DB size    : file not found")
    else:
        kb = status.db_size_bytes / 1024
        if kb < 1024:
            lines.append(f"  DB size    : {kb:.1f} KB")
        else:
            lines.append(f"  DB size    : {kb/1024:.2f} MB")

    lines.append("")

    # --- market_snapshots ---
    lines.append("  MARKET SNAPSHOTS")
    lines.append(f"    Total            : {status.snapshots.total:>8,}")
    if status.snapshots.by_source:
        for src, s in sorted(status.snapshots.by_source.items()):
            ts_str = _ms_to_utc_str(s.latest_ts_ms)
            lines.append(f"    {src:<16} : {s.count:>8,}  (latest: {ts_str})")
    else:
        lines.append("    (no snapshots yet — run 'scan' first)")
    lines.append("")

    # --- lag_records ---
    lag_ts = _ms_to_utc_str(status.lag_latest_ts_ms)
    lines.append("  LAG RECORDS")
    lines.append(f"    Total            : {status.lag_record_count:>8,}")
    if status.lag_record_count > 0:
        lines.append(f"    Latest           : {lag_ts}")
    else:
        lines.append("    (no lag records yet — run 'lag' after 'scan')")
    lines.append("")

    # --- paper_trades ---
    lines.append("  PAPER TRADES  (simulated — not real)")
    lines.append(f"    Total            : {status.paper_trade_count:>8,}")
    if status.paper_trade_by_status:
        for st, cnt in sorted(status.paper_trade_by_status.items()):
            lines.append(f"    {st:<16} : {cnt:>8,}")
    else:
        lines.append("    (no paper trades yet — run 'paper' after 'lag')")
    lines.append("")

    lines.append(sep)
    return "\n".join(lines)
