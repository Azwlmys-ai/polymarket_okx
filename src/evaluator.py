"""
evaluator.py — profitability evaluation report for paper trading results.

Reads the local ``paper_trades`` SQLite table (populated by ``paper_trader.py``)
and computes descriptive metrics over **simulated** outcomes only.

╔══════════════════════════════════════════════════════════════════════╗
║  IMPORTANT DISCLAIMER                                                ║
║  All results in this report are hypothetical simulations from a      ║
║  local paper-trading exercise.  They are NOT proof of               ║
║  profitability, NOT trading recommendations, and do NOT reflect      ║
║  real-market execution costs, liquidity, slippage, or market         ║
║  impact.  No real money is involved.  Phase-1 rules forbid real      ║
║  trading, wallet operations, API keys, and order placement.          ║
╚══════════════════════════════════════════════════════════════════════╝
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
    "SIMULATION DISCLAIMER: All metrics in this evaluation are derived from "
    "hypothetical local paper trades.  They are NOT proof of real profitability, "
    "NOT trading recommendations, and do NOT reflect actual market execution, "
    "liquidity constraints, fees beyond the fixed simulation rate, slippage "
    "beyond the fixed simulation rate, or real-market outcomes.  No real money "
    "is involved.  Phase-1 rules forbid real trading, private key handling, "
    "wallet operations, and order placement."
)

# Status codes (mirrors paper_trader.py constants — kept local to avoid coupling)
_STATUS_CLOSED = "closed"
_STATUS_CLOSED_STOP_LOSS = "closed_stop_loss"
_STATUS_OPEN_NO_EXIT = "open_no_exit"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class DrawdownStats:
    """Max simulated drawdown computed over the closed-trade PnL sequence."""
    max_drawdown: float | None        # largest peak-to-trough drop in cumulative PnL
    max_drawdown_pct: float | None    # same expressed as a fraction of the peak cash value


@dataclass
class PaperEvalMetrics:
    """
    All descriptive metrics for a paper-trading evaluation run.

    Notes
    -----
    - All monetary values are in simulated USDC.
    - ``gross_pnl`` = sum of (exit_price - entry_price) * quantity for closed trades.
    - ``net_pnl``   = sum of the ``pnl`` column (already deducts simulated fees).
    - ``win_rate``  = wins / (wins + losses); None if no closed trades with PnL.
    - Drawdown is computed over the closed-trade sequence ordered by ``opened_ts_ms``.
    """
    generated_at: str
    db_path: str
    total_rows: int                       # total rows in paper_trades table
    by_status: dict[str, int]             # count per status label
    closed_count: int
    open_no_exit_count: int
    skipped_count: int                    # total skipped (all non-executed statuses)
    # --- closed-trade PnL ---
    gross_pnl: float | None               # (exit−entry)*qty summed; None if no closed trades
    net_pnl: float | None                 # pnl column summed (fees deducted); None if no closed trades
    avg_pnl: float | None                 # mean net_pnl per closed trade
    median_pnl: float | None              # median net_pnl per closed trade
    wins: int                             # closed trades with pnl > 0
    losses: int                           # closed trades with pnl <= 0
    win_rate: float | None                # wins / (wins+losses), or None if denominator=0
    # --- open / unresolved exposure ---
    open_no_exit_notional: float          # sum of notional still in open_no_exit positions
    open_no_exit_fees_at_risk: float      # fees deducted but exit not yet found
    # --- stop-loss breakdown ---
    stop_loss_count: int = 0              # trades closed by stop_loss_yes_price trigger
    stop_loss_pnl: float | None = None    # total PnL for stop-loss closes (None if zero)
    large_loss_count: int = 0             # closed trades with pnl < -5.0 USDC
    # --- drawdown ---
    drawdown: DrawdownStats = field(default_factory=lambda: DrawdownStats(None, None))
    # --- data quality ---
    data_quality_notes: list[str] = field(default_factory=list)
    disclaimer: str = _DISCLAIMER


# ---------------------------------------------------------------------------
# Pure computation helpers (no I/O — fully unit-testable)
# ---------------------------------------------------------------------------

def compute_drawdown(closed_pnls: list[float]) -> DrawdownStats:
    """
    Compute max simulated drawdown over a sequence of per-trade net PnLs.

    ``closed_pnls`` should be ordered chronologically (by ``opened_ts_ms``).
    Drawdown is measured in cumulative-PnL space: the largest drop from any
    running peak to any subsequent trough.

    Returns a DrawdownStats with both values set to None if the list is empty.
    """
    if not closed_pnls:
        return DrawdownStats(max_drawdown=None, max_drawdown_pct=None)

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0

    for pnl in closed_pnls:
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Express as a fraction of the peak only if peak > 0
    if peak > 0:
        max_dd_pct: float | None = max_dd / peak
    else:
        max_dd_pct = None

    return DrawdownStats(
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
    )


def _median(values: list[float]) -> float:
    """Compute the median of a non-empty list (no external dependencies)."""
    sv = sorted(values)
    n = len(sv)
    mid = n // 2
    if n % 2 == 1:
        return sv[mid]
    return (sv[mid - 1] + sv[mid]) / 2.0


def build_data_quality_notes(rows: list[dict]) -> list[str]:
    """
    Return a list of plain-text data quality observations for the rows.

    These are descriptive caveats, not conclusions.
    """
    notes: list[str] = []
    total = len(rows)

    if total == 0:
        notes.append("No paper_trades rows found.  Run 'paper' first to generate simulation data.")
        return notes

    closed = [r for r in rows if r.get("status") == _STATUS_CLOSED]
    pnl_nulls = sum(1 for r in closed if r.get("pnl") is None)
    if pnl_nulls:
        notes.append(
            f"{pnl_nulls} closed trade(s) have NULL pnl — exit_price may be missing."
        )

    open_rows = [r for r in rows if r.get("status") == _STATUS_OPEN_NO_EXIT]
    if open_rows:
        pct_open = len(open_rows) / total * 100
        notes.append(
            f"{len(open_rows)} open_no_exit trade(s) ({pct_open:.1f}% of total): "
            f"no Polymarket exit snapshot was found within the hold window.  "
            f"Their outcomes are unresolved and excluded from PnL metrics."
        )

    if len(closed) < 30:
        notes.append(
            f"Only {len(closed)} closed trade(s) — sample size is very small.  "
            f"Simulated win rate and PnL averages carry high statistical uncertainty."
        )

    notes.append(
        "Simulated fees and slippage use fixed percentage rates.  "
        "Real-market costs vary with liquidity, order size, and timing."
    )
    notes.append(
        "No real order execution, no real liquidity, no real market impact.  "
        "Simulated results do NOT predict future real-money outcomes."
    )
    return notes


def compute_eval_metrics(rows: list[dict], db_path: str) -> PaperEvalMetrics:
    """
    Compute all evaluation metrics from raw ``paper_trades`` rows.

    ``rows`` — list of dicts from the paper_trades table (any order is fine;
    the function sorts closed trades by ``opened_ts_ms`` internally).

    All computation is deterministic and side-effect-free.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- Status counts ---
    by_status: dict[str, int] = {}
    for r in rows:
        s = str(r.get("status", "unknown"))
        by_status[s] = by_status.get(s, 0) + 1

    closed_rows = [r for r in rows if r.get("status") == _STATUS_CLOSED]
    stop_loss_rows = [r for r in rows if r.get("status") == _STATUS_CLOSED_STOP_LOSS]
    open_rows = [r for r in rows if r.get("status") == _STATUS_OPEN_NO_EXIT]
    all_closed_rows = closed_rows + stop_loss_rows
    skipped_count = len(rows) - len(closed_rows) - len(stop_loss_rows) - len(open_rows)

    # --- PnL metrics (all closed trades: hold_window_expired + stop_loss) ---
    closed_count = len(closed_rows)      # hold_window_expired count
    gross_pnl: float | None = None
    net_pnl: float | None = None
    avg_pnl: float | None = None
    median_pnl: float | None = None
    wins = 0
    losses = 0
    win_rate: float | None = None

    if all_closed_rows:
        # Gross PnL: (exit_price - entry_price) * quantity — does not deduct fees
        gross_vals: list[float] = []
        for r in all_closed_rows:
            ep = r.get("entry_price")
            xp = r.get("exit_price")
            qty = r.get("quantity")
            if ep is not None and xp is not None and qty is not None:
                gross_vals.append((float(xp) - float(ep)) * float(qty))
        gross_pnl = sum(gross_vals) if gross_vals else None

        # Net PnL: use the pre-computed pnl column (already fees-deducted)
        net_vals: list[float] = [
            float(r["pnl"]) for r in all_closed_rows if r.get("pnl") is not None
        ]
        if net_vals:
            net_pnl = sum(net_vals)
            avg_pnl = net_pnl / len(net_vals)
            median_pnl = _median(net_vals)
            wins = sum(1 for v in net_vals if v > 0)
            losses = sum(1 for v in net_vals if v <= 0)
            denom = wins + losses
            win_rate = wins / denom if denom > 0 else None

    # --- open_no_exit exposure ---
    open_notional = sum(float(r.get("notional") or 0.0) for r in open_rows)
    open_fees = sum(float(r.get("fees") or 0.0) for r in open_rows)

    # --- Stop-loss breakdown ---
    sl_count = len(stop_loss_rows)
    sl_pnl_vals = [float(r["pnl"]) for r in stop_loss_rows if r.get("pnl") is not None]
    sl_total_pnl: float | None = sum(sl_pnl_vals) if sl_pnl_vals else None

    # --- Large losses (|pnl| > 5 USDC, negative) ---
    all_pnl_vals = [float(r["pnl"]) for r in all_closed_rows if r.get("pnl") is not None]
    large_loss_cnt = sum(1 for p in all_pnl_vals if p < -5.0)

    # --- Drawdown (all closed trades, chronological) ---
    sorted_closed = sorted(
        [r for r in all_closed_rows if r.get("pnl") is not None],
        key=lambda r: int(r.get("opened_ts_ms") or 0),
    )
    drawdown = compute_drawdown([float(r["pnl"]) for r in sorted_closed])

    # --- Data quality notes ---
    dq_notes = build_data_quality_notes(rows)

    return PaperEvalMetrics(
        generated_at=ts,
        db_path=db_path,
        total_rows=len(rows),
        by_status=by_status,
        closed_count=closed_count,
        open_no_exit_count=len(open_rows),
        skipped_count=skipped_count,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        avg_pnl=avg_pnl,
        median_pnl=median_pnl,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        open_no_exit_notional=open_notional,
        open_no_exit_fees_at_risk=open_fees,
        stop_loss_count=sl_count,
        stop_loss_pnl=sl_total_pnl,
        large_loss_count=large_loss_cnt,
        drawdown=drawdown,
        data_quality_notes=dq_notes,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_eval_report(metrics: PaperEvalMetrics) -> str:
    """Render the evaluation metrics as a plain-text string for console output."""
    lines: list[str] = []
    sep = "─" * 62

    lines.append(sep)
    lines.append("  Polymarket × OKX — Paper Trading Profitability Evaluation")
    lines.append("  (Simulated results only — NOT a trading recommendation)")
    lines.append(sep)
    lines.append(f"  Generated : {metrics.generated_at}")
    lines.append(f"  Database  : {metrics.db_path}")
    lines.append(f"  Total rows: {metrics.total_rows}")
    lines.append("")

    if metrics.total_rows == 0:
        lines.append("  No paper_trades rows found.")
        lines.append("  Run 'paper' first to generate simulation data.")
        lines.append("")
        lines.append(sep)
        lines.append(f"  {_DISCLAIMER}")
        lines.append(sep)
        return "\n".join(lines)

    # --- Trade count breakdown ---
    lines.append("  TRADE COUNT BREAKDOWN")
    lines.append(f"    Closed          : {metrics.closed_count}")
    lines.append(f"    Open / no exit  : {metrics.open_no_exit_count}")
    lines.append(f"    Skipped (total) : {metrics.skipped_count}")
    if metrics.by_status:
        lines.append("    By status:")
        for status, count in sorted(metrics.by_status.items()):
            lines.append(f"      {status}: {count}")
    lines.append("")

    # --- Closed-trade PnL ---
    lines.append("  SIMULATED PnL (closed trades only)")
    if metrics.closed_count == 0:
        lines.append("    No closed trades.  Cannot compute PnL metrics.")
    else:
        def _fmt(v: float | None, suffix: str = " USDC (simulated)") -> str:
            if v is None:
                return "N/A"
            return f"{v:+.4f}{suffix}"

        lines.append(f"    Closed trade count : {metrics.closed_count}")
        lines.append(f"    Wins               : {metrics.wins}")
        lines.append(f"    Losses             : {metrics.losses}")
        if metrics.win_rate is not None:
            lines.append(f"    Simulated win rate : {metrics.win_rate:.1%}")
        else:
            lines.append("    Simulated win rate : N/A (no closed trades with PnL)")
        lines.append(f"    Gross PnL          : {_fmt(metrics.gross_pnl)}")
        lines.append(f"    Net PnL            : {_fmt(metrics.net_pnl)}")
        lines.append(f"    Avg PnL / trade    : {_fmt(metrics.avg_pnl)}")
        lines.append(f"    Median PnL / trade : {_fmt(metrics.median_pnl)}")
    lines.append("")

    # --- Stop-loss breakdown ---
    lines.append("  STOP-LOSS BREAKDOWN")
    lines.append(f"    Stop-loss triggers   : {metrics.stop_loss_count}")
    if metrics.stop_loss_pnl is not None:
        lines.append(f"    Stop-loss total PnL  : {metrics.stop_loss_pnl:+.4f} USDC (simulated)")
    else:
        lines.append("    Stop-loss total PnL  : N/A (no stop-loss closes)")
    lines.append(f"    Hold-window closes   : {metrics.closed_count}")
    lines.append(f"    Large losses >5 USDC : {metrics.large_loss_count}")
    lines.append("")

    # --- Open exposure ---
    lines.append("  OPEN / UNRESOLVED EXPOSURE")
    lines.append(f"    open_no_exit count   : {metrics.open_no_exit_count}")
    lines.append(
        f"    Notional at risk     : {metrics.open_no_exit_notional:+.4f} USDC (simulated)"
    )
    lines.append(
        f"    Fees deducted        : {metrics.open_no_exit_fees_at_risk:.4f} USDC (simulated)"
    )
    lines.append("")

    # --- Drawdown ---
    lines.append("  SIMULATED DRAWDOWN (closed trades, chronological)")
    dd = metrics.drawdown
    if dd.max_drawdown is None:
        lines.append("    N/A (no closed trades with PnL)")
    else:
        lines.append(f"    Max drawdown       : {dd.max_drawdown:.4f} USDC (simulated)")
        if dd.max_drawdown_pct is not None:
            lines.append(f"    Max drawdown pct   : {dd.max_drawdown_pct:.2%}")
        else:
            lines.append("    Max drawdown pct   : N/A (peak PnL ≤ 0)")
    lines.append("")

    # --- Data quality notes ---
    if metrics.data_quality_notes:
        lines.append("  DATA QUALITY CAVEATS")
        for note in metrics.data_quality_notes:
            # Wrap long notes with indentation
            lines.append(f"    • {note}")
        lines.append("")

    lines.append(sep)
    lines.append(f"  {_DISCLAIMER}")
    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SQLite loader
# ---------------------------------------------------------------------------

def load_paper_trades(db_path: str | Path) -> list[dict]:
    """
    Load all rows from the ``paper_trades`` SQLite table as plain dicts.

    Returns an empty list if the table does not exist or is empty.
    Raises no exceptions — a warning is logged on SQLite errors.
    """
    path = str(db_path)
    rows: list[dict] = []
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT id, opened_ts_ms, closed_ts_ms, market_id, asset, side,
                       entry_price, exit_price, notional, quantity,
                       fees, slippage, pnl, status, reason
                FROM paper_trades
                ORDER BY opened_ts_ms ASC
                """
            )
            for row in cur:
                rows.append(dict(row))
    except sqlite3.OperationalError as exc:
        logger.warning("Could not load paper_trades: %s", exc)
    logger.info("Loaded %d paper_trade row(s) from %s", len(rows), path)
    return rows


# ---------------------------------------------------------------------------
# File writers
# ---------------------------------------------------------------------------

def _ensure_reports_dir(reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    return reports_dir


def _metrics_to_dict(metrics: PaperEvalMetrics) -> dict[str, Any]:
    """Convert PaperEvalMetrics to a JSON-serialisable dict."""
    d = asdict(metrics)
    # DrawdownStats is already converted by asdict to a plain dict
    return d


def write_eval_report_json(metrics: PaperEvalMetrics, reports_dir: Path) -> Path:
    """Serialise the evaluation metrics to JSON and write to reports_dir."""
    _ensure_reports_dir(reports_dir)
    ts_tag = metrics.generated_at.replace(":", "").replace("-", "")[:15]
    out_path = reports_dir / f"paper_eval_{ts_tag}.json"
    out_path.write_text(
        json.dumps(_metrics_to_dict(metrics), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("JSON evaluation report written: %s", out_path)
    return out_path


def write_eval_report_markdown(metrics: PaperEvalMetrics, reports_dir: Path) -> Path:
    """Write the formatted evaluation report as a Markdown file."""
    _ensure_reports_dir(reports_dir)
    ts_tag = metrics.generated_at.replace(":", "").replace("-", "")[:15]
    out_path = reports_dir / f"paper_eval_{ts_tag}.md"
    out_path.write_text(format_eval_report(metrics), encoding="utf-8")
    logger.info("Markdown evaluation report written: %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def run_evaluation(
    db_path: str | Path,
    reports_dir: Path | None = None,
    output_formats: list[str] | None = None,
) -> PaperEvalMetrics:
    """
    Full evaluation pipeline:
      1. Load paper_trades from SQLite.
      2. Compute metrics.
      3. Optionally write JSON / Markdown report files.

    Returns the PaperEvalMetrics object regardless.
    ``output_formats`` may contain ``"json"`` and/or ``"markdown"``.
    """
    rows = load_paper_trades(db_path)
    metrics = compute_eval_metrics(rows, str(db_path))

    if output_formats and reports_dir:
        if "json" in output_formats:
            write_eval_report_json(metrics, reports_dir)
        if "markdown" in output_formats:
            write_eval_report_markdown(metrics, reports_dir)

    return metrics
