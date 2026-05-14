"""
report.py — lag distribution report from local SQLite data.

Reads lag_records (and optionally market_snapshots) from SQLite and produces
descriptive statistics only.

DISCLAIMER: Lag records are NOT proof of profitability and are NOT trading
signals.  They are raw research observations about timing differences between
two public data sources.  No edge, profit, or strategy recommendation is made.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DISCLAIMER = (
    "DISCLAIMER: These lag records are NOT proof of profitability and are NOT "
    "trading signals.  They are descriptive observations about timing differences "
    "between OKX public price data and Polymarket public prediction prices.  No "
    "edge, profitability claim, or strategy recommendation is made or implied."
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PercentileStats:
    """Lag timing statistics for one group (asset or overall)."""
    count: int
    min_ms: float
    median_ms: float
    mean_ms: float
    p90_ms: float
    p95_ms: float
    max_ms: float


@dataclass
class AssetStats:
    """Per-asset lag statistics."""
    asset: str
    count: int
    lag: PercentileStats
    avg_move_pct: float | None   # mean absolute OKX price move (from price fields)


@dataclass
class DataQuality:
    """Data quality observations — not conclusions, just counts."""
    total_records: int
    missing_prediction_price_before: int   # always expected; noted for transparency
    missing_prediction_price_after: int
    records_with_notes: int
    unique_polymarket_market_ids: int
    unique_okx_move_timestamps: int
    possible_duplicate_responses: int      # move count > unique poly market_ids count
    snapshot_density_warning: bool         # True if very few records (< 5)


@dataclass
class LagReport:
    """Full lag distribution report."""
    generated_at: str
    db_path: str
    total_records: int
    overall: PercentileStats | None
    by_asset: list[AssetStats] = field(default_factory=list)
    data_quality: DataQuality | None = None
    disclaimer: str = _DISCLAIMER


# ---------------------------------------------------------------------------
# Pure statistical helpers (no I/O — fully unit-testable)
# ---------------------------------------------------------------------------

def _percentile(sorted_values: list[float], p: float) -> float:
    """
    Compute the p-th percentile (0–100) of a pre-sorted list using linear
    interpolation.  Raises ValueError for empty lists.
    """
    if not sorted_values:
        raise ValueError("Cannot compute percentile of empty list")
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    idx = (p / 100) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return float(sorted_values[-1])
    frac = idx - lo
    return sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo])


def compute_percentile_stats(lag_ms_values: list[float]) -> PercentileStats:
    """
    Compute PercentileStats from a list of lag_ms values (unsorted is fine).
    Raises ValueError if the list is empty.
    """
    if not lag_ms_values:
        raise ValueError("No lag_ms values to compute statistics from")
    sv = sorted(lag_ms_values)
    n = len(sv)
    return PercentileStats(
        count=n,
        min_ms=sv[0],
        median_ms=_percentile(sv, 50),
        mean_ms=sum(sv) / n,
        p90_ms=_percentile(sv, 90),
        p95_ms=_percentile(sv, 95),
        max_ms=sv[-1],
    )


def compute_avg_move_pct(records: list[dict]) -> float | None:
    """
    Compute mean absolute OKX price move percentage from exchange_price_before/after.
    Returns None if no usable price pairs are found.
    """
    pcts: list[float] = []
    for r in records:
        before = r.get("exchange_price_before")
        after = r.get("exchange_price_after")
        if before and after and before != 0.0:
            pcts.append(abs(after - before) / before)
    if not pcts:
        return None
    return sum(pcts) / len(pcts)


def build_data_quality(records: list[dict]) -> DataQuality:
    """Summarise data quality observations from the raw lag_records rows."""
    total = len(records)
    missing_before = sum(1 for r in records if r.get("prediction_price_before") is None)
    missing_after = sum(1 for r in records if r.get("prediction_price_after") is None)
    with_notes = sum(1 for r in records if r.get("notes"))
    unique_poly = len({r.get("market_id") for r in records})
    unique_move_ts = len({r.get("exchange_move_ts_ms") for r in records})
    # If more moves than unique Poly markets, the same market may have been
    # counted multiple times — not an error, but noted so analysts are aware.
    possible_dupes = max(0, total - unique_poly)
    return DataQuality(
        total_records=total,
        missing_prediction_price_before=missing_before,
        missing_prediction_price_after=missing_after,
        records_with_notes=with_notes,
        unique_polymarket_market_ids=unique_poly,
        unique_okx_move_timestamps=unique_move_ts,
        possible_duplicate_responses=possible_dupes,
        snapshot_density_warning=total < 5,
    )


def build_report(records: list[dict], db_path: str) -> LagReport:
    """
    Build a LagReport from raw lag_records rows (list of dicts from SQLite).

    Returns a LagReport with total_records=0 and no overall/by_asset stats
    if *records* is empty.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not records:
        return LagReport(
            generated_at=ts,
            db_path=db_path,
            total_records=0,
            overall=None,
            by_asset=[],
            data_quality=DataQuality(
                total_records=0,
                missing_prediction_price_before=0,
                missing_prediction_price_after=0,
                records_with_notes=0,
                unique_polymarket_market_ids=0,
                unique_okx_move_timestamps=0,
                possible_duplicate_responses=0,
                snapshot_density_warning=True,
            ),
        )

    all_lags = [float(r["lag_ms"]) for r in records]
    overall = compute_percentile_stats(all_lags)

    # Group by asset
    asset_groups: dict[str, list[dict]] = {}
    for r in records:
        asset_groups.setdefault(str(r.get("asset", "unknown")), []).append(r)

    by_asset: list[AssetStats] = []
    for asset, group in sorted(asset_groups.items()):
        lags = [float(r["lag_ms"]) for r in group]
        by_asset.append(
            AssetStats(
                asset=asset,
                count=len(group),
                lag=compute_percentile_stats(lags),
                avg_move_pct=compute_avg_move_pct(group),
            )
        )

    dq = build_data_quality(records)

    return LagReport(
        generated_at=ts,
        db_path=db_path,
        total_records=len(records),
        overall=overall,
        by_asset=by_asset,
        data_quality=dq,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_ms(value: float) -> str:
    """Format a millisecond value for human display."""
    return f"{value:,.1f} ms"


def format_report(report: LagReport) -> str:
    """Render the report as a plain-text string suitable for console output."""
    lines: list[str] = []
    sep = "─" * 60

    lines.append(sep)
    lines.append("  Polymarket × OKX — Lag Distribution Report")
    lines.append(sep)
    lines.append(f"  Generated : {report.generated_at}")
    lines.append(f"  Database  : {report.db_path}")
    lines.append(f"  Records   : {report.total_records}")
    lines.append("")

    if report.total_records == 0:
        lines.append("  No lag records found.")
        lines.append("  Run 'scan --source okx' and 'scan --source polymarket',")
        lines.append("  then 'lag' before generating a report.")
        lines.append("")
        lines.append(sep)
        lines.append(f"  {_DISCLAIMER}")
        lines.append(sep)
        return "\n".join(lines)

    # Overall statistics
    o = report.overall
    if o:
        lines.append("  OVERALL LAG STATISTICS (all assets)")
        lines.append(f"    Count   : {o.count}")
        lines.append(f"    Min     : {_fmt_ms(o.min_ms)}")
        lines.append(f"    Median  : {_fmt_ms(o.median_ms)}")
        lines.append(f"    Mean    : {_fmt_ms(o.mean_ms)}")
        lines.append(f"    P90     : {_fmt_ms(o.p90_ms)}")
        lines.append(f"    P95     : {_fmt_ms(o.p95_ms)}")
        lines.append(f"    Max     : {_fmt_ms(o.max_ms)}")
        lines.append("")

    # Per-asset breakdown
    if report.by_asset:
        lines.append("  PER-ASSET BREAKDOWN")
        for a in report.by_asset:
            lines.append(f"    [{a.asset}]  {a.count} record(s)")
            lines.append(f"      Lag   min={_fmt_ms(a.lag.min_ms)}  "
                         f"median={_fmt_ms(a.lag.median_ms)}  "
                         f"p95={_fmt_ms(a.lag.p95_ms)}  "
                         f"max={_fmt_ms(a.lag.max_ms)}")
            if a.avg_move_pct is not None:
                lines.append(f"      OKX move  avg={a.avg_move_pct:.3%}")
        lines.append("")

    # Data quality
    dq = report.data_quality
    if dq:
        lines.append("  DATA QUALITY NOTES")
        lines.append(f"    Unique Polymarket markets  : {dq.unique_polymarket_market_ids}")
        lines.append(f"    Unique OKX move timestamps : {dq.unique_okx_move_timestamps}")
        lines.append(f"    Missing prediction_before  : {dq.missing_prediction_price_before} "
                     f"(expected — not yet collected)")
        lines.append(f"    Missing prediction_after   : {dq.missing_prediction_price_after}")
        if dq.possible_duplicate_responses > 0:
            lines.append(f"    Possible multi-mapped resp.: {dq.possible_duplicate_responses} "
                         f"(same Poly market matched >1 OKX move — not an error)")
        if dq.snapshot_density_warning:
            lines.append("    ⚠ Fewer than 5 lag records — run longer scans for meaningful stats")
        lines.append("")

    lines.append(sep)
    lines.append(f"  {_DISCLAIMER}")
    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SQLite loader
# ---------------------------------------------------------------------------

def load_lag_records(db_path: str | Path) -> list[dict[str, Any]]:
    """
    Load all rows from the lag_records table as plain dicts.
    Returns an empty list if the table does not exist or is empty.
    """
    path = str(db_path)
    rows: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT ts_ms, exchange_source, prediction_source, asset, market_id,
                       exchange_move_ts_ms, prediction_response_ts_ms, lag_ms,
                       exchange_price_before, exchange_price_after,
                       prediction_price_before, prediction_price_after, notes
                FROM lag_records
                ORDER BY ts_ms ASC
                """
            )
            for row in cur:
                rows.append(dict(row))
    except sqlite3.OperationalError as exc:
        logger.warning("Could not load lag_records: %s", exc)
    logger.info("Loaded %d lag record(s) from %s", len(rows), path)
    return rows


# ---------------------------------------------------------------------------
# File writers
# ---------------------------------------------------------------------------

def _ensure_reports_dir(reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    return reports_dir


def write_report_json(report: LagReport, reports_dir: Path) -> Path:
    """Serialise the report to JSON and write to reports_dir."""
    _ensure_reports_dir(reports_dir)
    ts_tag = report.generated_at.replace(":", "").replace("-", "")[:15]
    out_path = reports_dir / f"lag_report_{ts_tag}.json"

    def _to_dict(obj: Any) -> Any:
        if hasattr(obj, "__dataclass_fields__"):
            return {k: _to_dict(v) for k, v in asdict(obj).items()}
        return obj

    out_path.write_text(
        json.dumps(_to_dict(report), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("JSON report written: %s", out_path)
    return out_path


def write_report_markdown(report: LagReport, reports_dir: Path) -> Path:
    """Write the formatted report as a Markdown file."""
    _ensure_reports_dir(reports_dir)
    ts_tag = report.generated_at.replace(":", "").replace("-", "")[:15]
    out_path = reports_dir / f"lag_report_{ts_tag}.md"
    out_path.write_text(format_report(report), encoding="utf-8")
    logger.info("Markdown report written: %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# Per-asset paper-trade percentile statistics
# ---------------------------------------------------------------------------

@dataclass
class PerAssetTradeStats:
    """Percentile statistics for one asset's closed paper trades."""
    asset: str
    trade_count: int
    pnl_p25: float | None
    pnl_p50: float | None
    pnl_p75: float | None
    hold_s_p25: float | None
    hold_s_p50: float | None
    hold_s_p75: float | None
    entry_p25: float | None
    entry_p50: float | None
    entry_p75: float | None
    exit_p25: float | None
    exit_p50: float | None
    exit_p75: float | None


def compute_per_asset_trade_stats(
    trades: list[Any],
) -> list["PerAssetTradeStats"]:
    """Compute per-asset p25/p50/p75 for closed paper trades.

    The primary input is ``state.closed_positions`` from ``mvp_runner.py``.
    For testability and backward compatibility this helper also accepts dicts
    with equivalent keys.  ClosedPosition fields are intentionally read without
    changing the runner data structure:

    - asset: ``closed.pos.asset``
    - pnl: ``closed.pnl``
    - hold_s: ``(closed.closed_ts_ms - closed.pos.opened_ts_ms) / 1000``
    - entry_yes_price: ``closed.pos.entry_yes_price``
    - exit_yes_price: ``closed.exit_yes_price``

    None values are excluded from percentile calculation.  Returns an empty
    list for empty input.
    """
    groups: dict[str, list[dict[str, float | str | None]]] = {}
    for t in trades:
        normalized = _normalize_closed_position(t)
        key = str(normalized.get("asset") or "?")
        groups.setdefault(key, []).append(normalized)

    def _p3(values: list[float], p: int) -> float | None:
        sv = sorted(v for v in values if v is not None)
        return _percentile(sv, p) if sv else None

    result: list[PerAssetTradeStats] = []
    asset_order = {"BTC": 0, "ETH": 1, "SOL": 2}
    for asset in sorted(groups, key=lambda a: (asset_order.get(a, 99), a)):
        g = groups[asset]
        pnls    = [t.get("pnl")             for t in g]
        holds   = [t.get("hold_s")          for t in g]
        entries = [t.get("entry_yes_price") for t in g]
        exits   = [t.get("exit_yes_price")  for t in g]
        result.append(PerAssetTradeStats(
            asset=asset,
            trade_count=len(g),
            pnl_p25=_p3(pnls, 25),
            pnl_p50=_p3(pnls, 50),
            pnl_p75=_p3(pnls, 75),
            hold_s_p25=_p3(holds, 25),
            hold_s_p50=_p3(holds, 50),
            hold_s_p75=_p3(holds, 75),
            entry_p25=_p3(entries, 25),
            entry_p50=_p3(entries, 50),
            entry_p75=_p3(entries, 75),
            exit_p25=_p3(exits, 25),
            exit_p50=_p3(exits, 50),
            exit_p75=_p3(exits, 75),
        ))
    return result


def _normalize_closed_position(trade: Any) -> dict[str, float | str | None]:
    """Return the report fields for a ClosedPosition-like object or dict."""
    if isinstance(trade, dict):
        return {
            "asset": trade.get("asset"),
            "pnl": _to_optional_float(trade.get("pnl")),
            "hold_s": _to_optional_float(trade.get("hold_s")),
            "entry_yes_price": _to_optional_float(trade.get("entry_yes_price")),
            "exit_yes_price": _to_optional_float(trade.get("exit_yes_price")),
        }

    pos = getattr(trade, "pos", None)
    opened_ts_ms = getattr(pos, "opened_ts_ms", None)
    closed_ts_ms = getattr(trade, "closed_ts_ms", None)
    hold_s: float | None = None
    if opened_ts_ms is not None and closed_ts_ms is not None:
        hold_s = (float(closed_ts_ms) - float(opened_ts_ms)) / 1000.0

    return {
        "asset": getattr(pos, "asset", None),
        "pnl": _to_optional_float(getattr(trade, "pnl", None)),
        "hold_s": hold_s,
        "entry_yes_price": _to_optional_float(getattr(pos, "entry_yes_price", None)),
        "exit_yes_price": _to_optional_float(getattr(trade, "exit_yes_price", None)),
    }


def _to_optional_float(value: Any) -> float | None:
    """Convert numeric values to float while preserving None/non-numeric as None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_per_asset_trade_table(stats: list["PerAssetTradeStats"]) -> str:
    """Render per-asset trade stats as a Markdown section.

    Returns an empty-data notice when *stats* is empty.
    """
    if not stats:
        return "## Per-Asset 统计\n\n暂无已平仓交易，无法生成 per-asset 统计。\n"

    def _fv(v: float | None, fmt: str = ".4f") -> str:
        return "N/A" if v is None else format(v, fmt)

    header = (
        "| asset | trades "
        "| pnl_p25 | pnl_p50 | pnl_p75 "
        "| hold_s_p25 | hold_s_p50 | hold_s_p75 "
        "| entry_p25 | entry_p50 | entry_p75 "
        "| exit_p25 | exit_p50 | exit_p75 |"
    )
    sep = "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    rows = [header, sep]
    for s in stats:
        rows.append(
            f"| {s.asset} | {s.trade_count} "
            f"| {_fv(s.pnl_p25, '+.4f')} | {_fv(s.pnl_p50, '+.4f')} | {_fv(s.pnl_p75, '+.4f')} "
            f"| {_fv(s.hold_s_p25, '.0f')} | {_fv(s.hold_s_p50, '.0f')} | {_fv(s.hold_s_p75, '.0f')} "
            f"| {_fv(s.entry_p25)} | {_fv(s.entry_p50)} | {_fv(s.entry_p75)} "
            f"| {_fv(s.exit_p25)} | {_fv(s.exit_p50)} | {_fv(s.exit_p75)} |"
        )
    return "## Per-Asset 统计\n\n" + "\n".join(rows) + "\n"


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def run_report(
    db_path: str | Path,
    reports_dir: Path | None = None,
    output_formats: list[str] | None = None,
) -> LagReport:
    """
    Load lag records, build report, optionally write files.

    *output_formats* may contain "json" and/or "markdown".
    Returns the LagReport object regardless.
    """
    records = load_lag_records(db_path)
    report = build_report(records, str(db_path))

    if output_formats and reports_dir:
        if "json" in output_formats:
            write_report_json(report, reports_dir)
        if "markdown" in output_formats:
            write_report_markdown(report, reports_dir)

    return report
