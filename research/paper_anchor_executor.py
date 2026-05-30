"""
paper_anchor_executor.py — paper execution prototype.

Replays paper_anchor_signals.jsonl applying the T+180/dist>=120 strategy
and records simulated paper trades.  NO real orders.  NO OKX.  NO mvp_runner
changes.  Read-only data source.

Strategy:
    offset = 180s
    entry condition: abs(dist_usd) >= 120  (default, configurable)
    direction: UP if BTC > anchor_est, DOWN otherwise
    one trade per window (earliest eligible checkpoint)
    spread <= 0.03 required

Usage:
    python3 research/paper_anchor_executor.py
    python3 research/paper_anchor_executor.py --dist 100   # baseline run
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, stdev

SIGNALS_PATH = Path("research/paper_anchor_signals.jsonl")
TRADES_PATH  = Path("research/paper_anchor_executor_trades.jsonl")
REPORT_PATH  = Path("research/paper_anchor_executor_report.md")

FEE_RATE       = 0.07
SPREAD_MAX     = 0.03
OFFSET_ALLOWED = {180}        # only T+180 enters execution
DIST_DEFAULT   = 120.0        # default threshold
DIST_BASELINE  = 100.0
DIST_OBSERVE   = 150.0


# ---------------------------------------------------------------------------
# Pure functions (unit-testable, no I/O)
# ---------------------------------------------------------------------------

def should_enter_trade(
    offset_s: int,
    dist_usd: float,
    spread: float | None,
    dist_threshold: float,
) -> tuple[bool, str]:
    """
    Evaluate entry conditions. Returns (enter: bool, reason: str).

    Disallowed offsets (T+90, T+120) are hard-rejected.
    Only T+180 is allowed.
    """
    if offset_s not in OFFSET_ALLOWED:
        return False, f"offset_blocked:{offset_s}s"
    if dist_usd < dist_threshold:
        return False, f"dist_too_small:{dist_usd:.1f}<{dist_threshold:.0f}"
    if spread is None or spread > SPREAD_MAX:
        return False, f"spread_wide:{spread}"
    return True, "ok"


def classify_skip_reason(offset_s: int, dist_usd: float, spread: float | None) -> str:
    """Return a concise skip reason string for a rejected signal."""
    if offset_s not in OFFSET_ALLOWED:
        return f"offset_blocked:{offset_s}s"
    if dist_usd < DIST_DEFAULT:
        if dist_usd < DIST_BASELINE:
            return "below_baseline"
        return "below_default_above_baseline"
    if spread is None or spread > SPREAD_MAX:
        return "spread_too_wide"
    return "unknown"


def compute_paper_pnl(
    direction: str,
    outcome: str,
    entry_price: float,
) -> float:
    """
    Fee-adjusted PnL for one paper trade.

    entry_price: the YES ask (UP) or implied NO ask (1 - YES_bid) (DOWN).
    Returns: 1.0 - (entry_price + fee)  if correct
             0.0 - (entry_price + fee)  if wrong
    """
    fee  = FEE_RATE * (1.0 - entry_price)
    cost = entry_price + fee
    payout = 1.0 if direction == outcome else 0.0
    return round(payout - cost, 6)


def enforce_one_trade_per_window(
    checkpoints: list[dict],
    dist_threshold: float,
) -> dict | None:
    """
    From a list of checkpoint dicts, return the first T+180 checkpoint
    that satisfies all entry conditions, or None.

    'First' means lowest offset_s that clears the threshold (practically
    always T+180 since T+90/T+120 are blocked).
    """
    for cp in sorted(checkpoints, key=lambda c: c.get("offset_s", 999)):
        if cp.get("error") or not cp.get("triggered"):
            continue
        offset = cp.get("offset_s", 0)
        dist   = cp.get("distance", 0.0)
        spread = cp.get("poly_spread")
        ok, _ = should_enter_trade(offset, dist, spread, dist_threshold)
        if ok:
            return cp
    return None


def summarize_trades(trades: list[dict]) -> dict:
    """Aggregate paper-trade statistics from a list of trade dicts."""
    executed = [t for t in trades if not t.get("skipped")]
    pnls     = [t["pnl"] for t in executed if t.get("pnl") is not None]

    if not executed:
        return {
            "total_trades": 0, "win_rate": None, "mean_pnl": None,
            "median_pnl": None, "cumulative_pnl": 0.0, "max_drawdown": 0.0,
            "longest_loss_streak": 0, "skipped_count": len(trades),
        }

    wins = sum(1 for t in executed if t.get("pnl", -1) > 0)

    # drawdown
    peak = cur = dd = 0.0
    for p in pnls:
        cur += p; peak = max(peak, cur); dd = max(dd, peak - cur)

    # loss streak
    best = cur_ls = 0
    for p in pnls:
        if p < 0: cur_ls += 1; best = max(best, cur_ls)
        else:      cur_ls = 0

    return {
        "total_trades":       len(executed),
        "win_rate":           round(wins / len(executed), 4),
        "mean_pnl":           round(mean(pnls), 6) if pnls else None,
        "median_pnl":         round(median(pnls), 6) if pnls else None,
        "cumulative_pnl":     round(sum(pnls), 6),
        "max_drawdown":       round(dd, 6),
        "longest_loss_streak": best,
        "skipped_count":      len(trades) - len(executed),
    }


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PaperTrade:
    window_id:     str
    ts:            str
    asset:         str
    offset_s:      int
    direction:     str
    dist_usd:      float
    anchor_est:    float | None
    observed_price: float
    spread:        float | None
    entry_price:   float
    exit_price:    float | None   # 1.0 if win else 0.0
    outcome:       str
    pnl:           float
    reason:        str
    skipped:       bool
    skipped_reason: str | None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Replay engine
# ---------------------------------------------------------------------------

def _entry_price(cp: dict) -> float:
    """Ask price to simulate taker entry."""
    d = cp.get("direction", "UP")
    if d == "UP":
        return cp.get("poly_ask") or 0.50
    return 1.0 - (cp.get("poly_bid") or 0.50)


def replay(resolved: list[dict], dist_threshold: float) -> list[PaperTrade]:
    """
    Simulate paper trades by replaying resolved windows.

    One trade per window, T+180 only, dist >= dist_threshold.
    Skipped windows are included with skipped=True for audit.
    """
    trades: list[PaperTrade] = []

    for r in resolved:
        slug    = r.get("slug", "unknown")
        outcome = r.get("outcome")
        bto     = r.get("binance_t_open")
        ptb     = r.get("price_to_beat")
        anchor  = (bto - 76.75) if bto else None   # static correction

        cps = r.get("checkpoints", [])
        best_cp = enforce_one_trade_per_window(cps, dist_threshold)

        if best_cp is None:
            # Find best T+180 cp for skip audit (regardless of threshold)
            t180_cps = [c for c in cps if c.get("offset_s") == 180 and not c.get("error")]
            if not t180_cps:
                continue   # no T+180 data at all — omit entirely
            cp = max(t180_cps, key=lambda c: c.get("distance", 0))
            skip_reason = classify_skip_reason(
                cp.get("offset_s", 0),
                cp.get("distance", 0.0),
                cp.get("poly_spread"),
            )
            trades.append(PaperTrade(
                window_id=slug, ts=cp.get("ts_utc", ""),
                asset="BTC-POLYMARKET", offset_s=cp.get("offset_s", 180),
                direction=cp.get("direction", "—"),
                dist_usd=round(cp.get("distance", 0.0), 2),
                anchor_est=round(anchor, 2) if anchor else None,
                observed_price=round(cp.get("btc_live", 0.0), 2),
                spread=cp.get("poly_spread"),
                entry_price=_entry_price(cp),
                exit_price=None,
                outcome=outcome or "unknown",
                pnl=0.0,
                reason="skipped",
                skipped=True,
                skipped_reason=skip_reason,
            ))
        else:
            ep  = _entry_price(best_cp)
            pnl = compute_paper_pnl(best_cp["direction"], outcome or "", ep)
            trades.append(PaperTrade(
                window_id=slug, ts=best_cp.get("ts_utc", ""),
                asset="BTC-POLYMARKET", offset_s=180,
                direction=best_cp["direction"],
                dist_usd=round(best_cp.get("distance", 0.0), 2),
                anchor_est=round(anchor, 2) if anchor else None,
                observed_price=round(best_cp.get("btc_live", 0.0), 2),
                spread=best_cp.get("poly_spread"),
                entry_price=round(ep, 4),
                exit_price=1.0 if pnl > 0 else 0.0,
                outcome=outcome or "unknown",
                pnl=pnl,
                reason="entered",
                skipped=False,
                skipped_reason=None,
            ))

    return trades


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _f(v, fmt="+.4f"):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:{fmt}}"


def build_report(
    trades_120: list[PaperTrade],
    trades_100: list[PaperTrade],
    n_resolved:  int,
) -> str:
    d120 = [t.to_dict() for t in trades_120]
    d100 = [t.to_dict() for t in trades_100]
    s120 = summarize_trades(d120)
    s100 = summarize_trades(d100)
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # dist>=150 observation from the 120 trade list
    d150 = [t.to_dict() for t in trades_120 if not t.skipped and t.dist_usd >= 150]
    s150 = summarize_trades(d150)

    lines: list[str] = []
    a = lines.append

    a("# Paper Anchor Executor Report")
    a("")
    a(f"> Generated: {ts}  |  Strategy: T+180s  |  Anchor correction: −76.75 USD")
    a(f"> Source: paper_anchor_signals.jsonl  |  Resolved windows: {n_resolved}")
    a(f"> **NO REAL ORDERS PLACED. PAPER MODE ONLY.**")
    a("")

    # ── Threshold comparison ──
    a("## 1. Threshold Comparison")
    a("")
    a("| Metric | dist ≥ $150 (obs) | dist ≥ $120 (**default**) | dist ≥ $100 (baseline) |")
    a("|--------|------------------|--------------------------|----------------------|")
    for label, s in [("dist ≥ $150 obs", s150), ("dist ≥ $120 default", s120),
                     ("dist ≥ $100 baseline", s100)]:
        n = s.get("total_trades", 0)
        wr = f"{s['win_rate']:.1%}" if s.get("win_rate") is not None else "—"
        mp = _f(s.get("mean_pnl"))
        cum = _f(s.get("cumulative_pnl"))
        dd = f"{s.get('max_drawdown',0):.3f}"
        ls = str(s.get("longest_loss_streak", 0))
        a(f"| {label} | {n} | {wr} | {mp} | {cum} | {dd} | {ls} |")

    # Proper table layout
    a("")
    a("| Metric | dist ≥ $150 | dist ≥ $120 | dist ≥ $100 |")
    a("|--------|------------|------------|------------|")
    rows = [
        ("N trades",         s150.get("total_trades",0), s120.get("total_trades",0), s100.get("total_trades",0)),
        ("Win rate",         f"{s150['win_rate']:.1%}" if s150.get("win_rate") else "—",
                             f"{s120['win_rate']:.1%}" if s120.get("win_rate") else "—",
                             f"{s100['win_rate']:.1%}" if s100.get("win_rate") else "—"),
        ("Mean PnL",         _f(s150.get("mean_pnl")), _f(s120.get("mean_pnl")), _f(s100.get("mean_pnl"))),
        ("Median PnL",       _f(s150.get("median_pnl")), _f(s120.get("median_pnl")), _f(s100.get("median_pnl"))),
        ("Cumulative PnL",   _f(s150.get("cumulative_pnl")), _f(s120.get("cumulative_pnl")), _f(s100.get("cumulative_pnl"))),
        ("Max drawdown",     f"{s150.get('max_drawdown',0):.3f}", f"{s120.get('max_drawdown',0):.3f}", f"{s100.get('max_drawdown',0):.3f}"),
        ("Longest loss run", s150.get("longest_loss_streak",0), s120.get("longest_loss_streak",0), s100.get("longest_loss_streak",0)),
        ("Skipped",          s150.get("skipped_count",0), s120.get("skipped_count",0), s100.get("skipped_count",0)),
    ]
    for row in rows:
        a(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} |")
    a("")

    # ── Sample coverage ──
    a("## 2. Sample Coverage vs Targets")
    a("")
    a("| Threshold | Signals | Target | Met? |")
    a("|-----------|---------|--------|------|")
    n100t = s100.get("total_trades", 0)
    n120t = s120.get("total_trades", 0)
    n150t = s150.get("total_trades", 0)
    met100 = "✅" if n100t >= 200 else f"❌ need {max(0,200-n100t)} more"
    met120 = "✅" if n120t >= 100 else f"❌ need {max(0,100-n120t)} more"
    met150 = "✅" if n150t >= 50  else f"❌ need {max(0, 50-n150t)} more"
    a(f"| dist ≥ $100 | {n100t} | 200 | {met100} |")
    a(f"| dist ≥ $120 | {n120t} | 100 | {met120} |")
    a(f"| dist ≥ $150 | {n150t} | 50  | {met150} |")
    a("")

    # ── Recent trades ──
    executed = [t for t in trades_120 if not t.skipped][-20:]
    if executed:
        a("## 3. Last 20 Executed Trades (dist ≥ $120)")
        a("")
        a("| # | Dir | Dist | Spread | Entry | Outcome | PnL |")
        a("|---|-----|------|--------|-------|---------|-----|")
        for i, t in enumerate(executed, 1):
            icon = "✅" if t.pnl > 0 else "❌"
            a(f"| {i} | {t.direction} | ${t.dist_usd:.0f} | {t.spread or '—'} "
              f"| {t.entry_price:.3f} | {t.outcome} {icon} | {_f(t.pnl)} |")
        a("")

    # ── Status and conclusions ──
    a("## 4. Status and Conclusions")
    a("")
    a("| Item | Status |")
    a("|------|--------|")
    a("| T+90s | ❌ Blocked from execution path |")
    a("| T+120s | ❌ Blocked from execution path |")
    a("| T+180s (all) | ❌ Too wide distribution |")
    a(f"| T+180s / dist ≥ $120 | {'✅ Active default' if s120.get('win_rate',0)>=0.535 else '⚠️ Below threshold'} |")
    a(f"| T+180s / dist ≥ $100 | Baseline reference |")
    a("| Real orders | ❌ PROHIBITED — paper mode only |")
    a("| Live runner | ❌ mvp_runner.py NOT modified |")
    a("")

    if s120.get("win_rate", 0) >= 0.535 and s120.get("total_trades", 0) >= 30:
        a("**dist ≥ $120 meets win-rate threshold.** "
          "Pending: ≥100 trade sample and BTC up-regime coverage before live consideration.")
    else:
        missing = max(0, 100 - s120.get("total_trades", 0))
        a(f"**Insufficient sample** — need {missing} more dist≥$120 trades for GO decision.")

    a("")
    a("---")
    a("*Paper execution prototype. No wallet. No real orders. Read-only data source.*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_resolved() -> list[dict]:
    return [json.loads(l) for l in SIGNALS_PATH.read_text().splitlines()
            if l.strip() and json.loads(l).get("resolved")]


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper anchor executor (read-only)")
    parser.add_argument("--dist", type=float, default=DIST_DEFAULT,
                        help=f"Default dist threshold (default {DIST_DEFAULT})")
    parser.add_argument("--out",    type=Path, default=TRADES_PATH)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    args = parser.parse_args()

    resolved = load_resolved()
    print(f"[executor] Resolved windows: {len(resolved)}")
    print(f"[executor] Strategy: T+180s, dist≥${args.dist:.0f} (default), dist≥$100 (baseline)")
    print(f"[executor] NO REAL ORDERS — paper mode only")

    trades_default  = replay(resolved, args.dist)
    trades_baseline = replay(resolved, DIST_BASELINE)

    executed_d = [t for t in trades_default  if not t.skipped]
    executed_b = [t for t in trades_baseline if not t.skipped]
    print(f"[executor] Default (dist≥${args.dist:.0f}):  {len(executed_d)} trades executed")
    print(f"[executor] Baseline (dist≥$100): {len(executed_b)} trades executed")

    # Write JSONL (default threshold trades, all including skipped)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for t in trades_default:
            f.write(json.dumps(t.to_dict()) + "\n")
    print(f"[executor] Trades → {args.out} ({len(trades_default)} records)")

    # Stats
    sd = summarize_trades([t.to_dict() for t in trades_default])
    sb = summarize_trades([t.to_dict() for t in trades_baseline])
    print(f"\n  default  WR={sd.get('win_rate',0):.1%}  "
          f"mean={sd.get('mean_pnl',0):+.4f}  cum={sd.get('cumulative_pnl',0):+.4f}"
          f"  maxDD={sd.get('max_drawdown',0):.3f}")
    print(f"  baseline WR={sb.get('win_rate',0):.1%}  "
          f"mean={sb.get('mean_pnl',0):+.4f}  cum={sb.get('cumulative_pnl',0):+.4f}"
          f"  maxDD={sb.get('max_drawdown',0):.3f}")

    report = build_report(trades_default, trades_baseline, len(resolved))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(f"[executor] Report → {args.report}")


if __name__ == "__main__":
    main()
