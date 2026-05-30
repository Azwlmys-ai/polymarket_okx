"""
checkpoint_report.py — Rolling-anchor validation checkpoint.

Separates signals into:
  LEGACY  : event_start_ts < ROLLING_EPOCH_START_TS  (static correction 76.75 → 88.70)
  ROLLING : event_start_ts >= ROLLING_EPOCH_START_TS (dynamic rolling correction)

Strategy logic is FROZEN. This script is read-only analytics only.

Usage:
  python3 research/checkpoint_report.py
  python3 research/checkpoint_report.py --out research/checkpoint_latest.md
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, stdev

SIGNALS_PATH = Path("research/paper_anchor_signals.jsonl")

# Must match paper_anchor_sim.py — DO NOT change
ROLLING_EPOCH_START_TS = 1779541200
ANCHOR_CORRECTION_WINDOW = 100
TAKER_FEE_RATE = 0.07


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_signals(path: Path) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
    return records


def is_rolling(rec: dict) -> bool:
    return rec.get("event_start_ts", 0) >= ROLLING_EPOCH_START_TS


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------

def paper_pnl(cp: dict, outcome: str | None) -> float | None:
    """Fee-adjusted PnL for one checkpoint. Mirrors paper_anchor_sim.py logic (FROZEN)."""
    if outcome is None:
        return None
    if not cp.get("triggered"):
        return None
    direction = cp.get("direction")
    if direction == "UP":
        bet_price = cp.get("poly_ask") or 0.50
    elif direction == "DOWN":
        bid = cp.get("poly_bid")
        bet_price = (1.0 - bid) if bid else 0.50
    else:
        return None
    fee = TAKER_FEE_RATE * (1.0 - bet_price)
    total_cost = bet_price + fee
    payout = 1.0 if direction == outcome else 0.0
    return payout - total_cost


def compute_rolling_correction_series(resolved: list[dict]) -> list[float]:
    """Reproduce the rolling deque from resolved records in chronological order."""
    dq: deque[float] = deque(maxlen=ANCHOR_CORRECTION_WINDOW)
    series = []
    for r in sorted(resolved, key=lambda x: x.get("event_start_ts", 0)):
        t_open = r.get("binance_t_open")
        ptb = r.get("price_to_beat")
        if t_open and ptb:
            dq.append(t_open - ptb)
            series.append(mean(dq) if len(dq) >= 20 else None)
        else:
            series.append(None)
    return series


def analyse_cohort(
    records: list[dict],
    offset_s: int = 180,
    dist_threshold: float = 120.0,
) -> dict:
    """Compute WR, PnL stats, drawdown for a cohort at the given offset+dist threshold."""
    resolved = [r for r in records if r.get("resolved") and r.get("outcome")]

    trades: list[tuple[float, str, str]] = []  # (dist, direction, outcome)
    pnls: list[float] = []

    for r in resolved:
        outcome = r.get("outcome")
        for cp in r.get("checkpoints", []):
            if cp.get("offset_s") != offset_s:
                continue
            if not cp.get("triggered"):
                continue
            dist = cp.get("distance", 0.0)
            if dist < dist_threshold:
                continue
            direction = cp.get("direction")
            pnl = paper_pnl(cp, outcome)
            if pnl is not None:
                trades.append((dist, direction, outcome))
                pnls.append(pnl)

    n = len(trades)
    if n == 0:
        return {"n": 0}

    wins = sum(1 for _, d, o in trades if d == o)
    wr = wins / n
    cum_pnl = pnls[:]
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in cum_pnl:
        running += p
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    up_dir = sum(1 for _, d, _ in trades if d == "UP")
    dn_dir = sum(1 for _, d, _ in trades if d == "DOWN")

    dist_vals = [d for d, _, _ in trades]

    return {
        "n": n,
        "wins": wins,
        "wr": wr,
        "mean_pnl": mean(pnls),
        "median_pnl": median(pnls),
        "stdev_pnl": stdev(pnls) if n >= 2 else float("nan"),
        "cumulative_pnl": sum(pnls),
        "max_drawdown": max_dd,
        "up_pct": up_dir / n,
        "dn_pct": dn_dir / n,
        "mean_dist": mean(dist_vals),
        "min_dist": min(dist_vals),
    }


def rolling_correction_stats(resolved: list[dict]) -> dict:
    """Stats on the observed anchor delta series."""
    deltas = [
        r["binance_t_open"] - r["price_to_beat"]
        for r in resolved
        if r.get("binance_t_open") and r.get("price_to_beat")
    ]
    if not deltas:
        return {"n": 0}

    dq: deque[float] = deque(maxlen=ANCHOR_CORRECTION_WINDOW)
    for d in deltas:
        dq.append(d)

    last50 = deltas[-50:] if len(deltas) >= 50 else deltas

    return {
        "n": len(deltas),
        "rolling_current": mean(dq),
        "last50_mean": mean(last50),
        "all_mean": mean(deltas),
        "all_stdev": stdev(deltas) if len(deltas) >= 2 else float("nan"),
        "last50_stdev": stdev(last50) if len(last50) >= 2 else float("nan"),
        "min": min(deltas),
        "max": max(deltas),
    }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_checkpoint(records: list[dict], out: Path | None = None) -> str:
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    legacy = [r for r in records if not is_rolling(r)]
    rolling = [r for r in records if is_rolling(r)]

    legacy_resolved = [r for r in legacy if r.get("resolved") and r.get("outcome")]
    rolling_resolved = [r for r in rolling if r.get("resolved") and r.get("outcome")]

    # Rolling correction stats (all resolved, chronological)
    all_resolved = [r for r in records if r.get("resolved") and r.get("outcome")]
    corr_stats = rolling_correction_stats(all_resolved)

    # Performance analysis
    OFFSET, DIST = 180, 120.0
    leg_perf = analyse_cohort(legacy, OFFSET, DIST)
    rol_perf = analyse_cohort(rolling, OFFSET, DIST)

    # Targets
    TARGET_N_PHASE1 = 50
    TARGET_WR_PHASE1 = 0.80
    TARGET_N_PHASE2 = 100

    post_n = rol_perf.get("n", 0)
    post_wr = rol_perf.get("wr", 0.0)

    lines: list[str] = []
    a = lines.append

    def _pct(v, fallback="N/A") -> str:
        return f"{v:.1%}" if isinstance(v, float) and not math.isnan(v) else fallback

    def _f2(v, fallback="N/A") -> str:
        return f"{v:.2f}" if isinstance(v, float) and not math.isnan(v) else fallback

    a("# Rolling-Anchor Validation Checkpoint")
    a("")
    a(f"> Generated: {ts_now}")
    a(f"> Strategy FROZEN — observe only. No logic changes permitted.")
    a(f"> Epoch boundary: event_start_ts ≥ {ROLLING_EPOCH_START_TS} (2026-05-23 ~13:00 UTC)")
    a(f"> Signal evaluated: T+{OFFSET}s / dist ≥ ${DIST:.0f}")
    a("")

    # ── 1. Rolling correction status ────────────────────────────────────────
    a("## 1. Rolling Correction Status")
    a("")
    a("| Metric | Value |")
    a("|--------|-------|")
    a(f"| rolling_correction_current | {_f2(corr_stats.get('rolling_current', float('nan')))} USD |")
    a(f"| rolling_correction_last50_mean | {_f2(corr_stats.get('last50_mean', float('nan')))} USD |")
    a(f"| rolling_sample_count (total) | {corr_stats.get('n', 0)} |")
    a(f"| post_migration_sample_count | {len(rolling_resolved)} |")
    a(f"| all-time delta mean | {_f2(corr_stats.get('all_mean', float('nan')))} USD |")
    a(f"| all-time delta stdev | {_f2(corr_stats.get('all_stdev', float('nan')))} USD |")
    a(f"| last-50 delta stdev | {_f2(corr_stats.get('last50_stdev', float('nan')))} USD |")
    a(f"| delta range | [{_f2(corr_stats.get('min', float('nan')))}, {_f2(corr_stats.get('max', float('nan')))}] |")
    a("")

    # ── 2. Dataset split ─────────────────────────────────────────────────────
    a("## 2. Dataset Split")
    a("")
    a("| Cohort | Total windows | Resolved |")
    a("|--------|--------------|---------|")
    a(f"| LEGACY (static correction) | {len(legacy)} | {len(legacy_resolved)} |")
    a(f"| ROLLING (dynamic correction) | {len(rolling)} | {len(rolling_resolved)} |")
    a(f"| **Total** | **{len(records)}** | **{len(all_resolved)}** |")
    a("")

    # ── 3. Performance: legacy_vs_rolling ────────────────────────────────────
    a("## 3. Legacy vs Rolling Performance (T+180s / dist ≥ $120)")
    a("")
    a("| Metric | LEGACY | ROLLING |")
    a("|--------|--------|---------|")

    def _cohort_row(label, leg_val, rol_val):
        a(f"| {label} | {leg_val} | {rol_val} |")

    _cohort_row("N trades",
                str(leg_perf.get("n", 0)),
                str(rol_perf.get("n", 0)))
    _cohort_row("Win rate",
                _pct(leg_perf.get("wr", float("nan"))),
                _pct(rol_perf.get("wr", float("nan"))))
    _cohort_row("Mean PnL/trade",
                _f2(leg_perf.get("mean_pnl", float("nan"))),
                _f2(rol_perf.get("mean_pnl", float("nan"))))
    _cohort_row("Cumulative PnL",
                _f2(leg_perf.get("cumulative_pnl", float("nan"))),
                _f2(rol_perf.get("cumulative_pnl", float("nan"))))
    _cohort_row("Max drawdown",
                _f2(leg_perf.get("max_drawdown", float("nan"))),
                _f2(rol_perf.get("max_drawdown", float("nan"))))
    _cohort_row("Direction: UP%",
                _pct(leg_perf.get("up_pct", float("nan"))),
                _pct(rol_perf.get("up_pct", float("nan"))))
    _cohort_row("Mean dist",
                _f2(leg_perf.get("mean_dist", float("nan"))),
                _f2(rol_perf.get("mean_dist", float("nan"))))
    a("")

    # ── 4. Phase targets progress ────────────────────────────────────────────
    a("## 4. Validation Targets")
    a("")
    a("### Phase 1 (current)")
    a("")
    a("| Criterion | Target | Current | Pass? |")
    a("|-----------|--------|---------|-------|")

    def _pass(ok: bool) -> str:
        return "✅" if ok else "❌"

    n_ok = post_n >= TARGET_N_PHASE1
    wr_ok = post_wr >= TARGET_WR_PHASE1 if post_n > 0 else False
    a(f"| N trades (rolling) | ≥ {TARGET_N_PHASE1} | {post_n} | {_pass(n_ok)} |")
    a(f"| Win rate (rolling) | ≥ {_pct(TARGET_WR_PHASE1)} | {_pct(post_wr) if post_n > 0 else 'N/A'} | {_pass(wr_ok) if post_n > 0 else '⏳'} |")

    # DD check: rolling DD not worse than legacy DD * 1.5
    leg_dd = leg_perf.get("max_drawdown", 0.0) or 0.0
    rol_dd = rol_perf.get("max_drawdown", 0.0) or 0.0
    dd_tol = max(leg_dd * 1.5, 2.0)
    dd_ok = rol_dd <= dd_tol if post_n > 0 else False
    a(f"| Max drawdown | ≤ {_f2(dd_tol)} | {_f2(rol_dd) if post_n > 0 else 'N/A'} | {_pass(dd_ok) if post_n > 0 else '⏳'} |")

    phase1_pass = n_ok and wr_ok and dd_ok
    a("")
    a(f"**Phase 1 verdict: {'✅ PASS — proceed to Phase 2' if phase1_pass else '⏳ IN PROGRESS' if post_n > 0 else '⏳ COLLECTING DATA'}**")
    a("")

    a("### Phase 2 (after Phase 1 pass)")
    a("")
    a(f"| Criterion | Target | Current |")
    a(f"|-----------|--------|---------|")
    a(f"| N trades (rolling) | ≥ {TARGET_N_PHASE2} | {post_n} |")
    a("")

    # ── 5. Rolling correction drift stability ────────────────────────────────
    a("## 5. Correction Drift Stability")
    a("")
    if corr_stats.get("n", 0) >= 10:
        all_resolved_sorted = sorted(all_resolved, key=lambda x: x.get("event_start_ts", 0))
        deltas_all = [
            r["binance_t_open"] - r["price_to_beat"]
            for r in all_resolved_sorted
            if r.get("binance_t_open") and r.get("price_to_beat")
        ]
        blocks: list[tuple[str, list[float]]] = []
        bsz = 50
        for i in range(0, len(deltas_all), bsz):
            blk = deltas_all[i:i + bsz]
            label = f"win {i+1}–{i+len(blk)}"
            blocks.append((label, blk))

        a("| Block | N | Mean correction | Stdev |")
        a("|-------|---|----------------|-------|")
        for label, blk in blocks:
            sd_str = f"{stdev(blk):.2f}" if len(blk) >= 2 else "N/A"
            a(f"| {label} | {len(blk)} | {mean(blk):.2f} | {sd_str} |")
        a("")
        if len(blocks) >= 2:
            first_mean = mean(blocks[0][1])
            last_mean = mean(blocks[-1][1])
            drift = last_mean - first_mean
            a(f"Total drift first→last block: **{drift:+.2f} USD**")
            a("")
    else:
        a("*Not enough samples for drift analysis yet.*")
        a("")

    # ── 6. Post-migration recent signals ────────────────────────────────────
    a("## 6. Post-Migration Recent Signals (last 20 at T+180 / dist ≥ $120)")
    a("")
    rolling_trades = []
    for r in sorted(rolling_resolved, key=lambda x: x.get("event_start_ts", 0)):
        outcome = r.get("outcome")
        for cp in r.get("checkpoints", []):
            if cp.get("offset_s") == 180 and cp.get("triggered") and cp.get("distance", 0) >= 120:
                pnl = paper_pnl(cp, outcome)
                rolling_trades.append({
                    "ts": r["event_start_ts"],
                    "dist": cp["distance"],
                    "dir": cp["direction"],
                    "outcome": outcome,
                    "win": cp["direction"] == outcome,
                    "pnl": pnl,
                })

    if rolling_trades:
        recent = rolling_trades[-20:]
        a("| # | Dir | Dist | Outcome | Win | PnL |")
        a("|---|-----|------|---------|-----|-----|")
        for i, t in enumerate(recent, 1):
            win_str = "✅" if t["win"] else "❌"
            pnl_str = f"{t['pnl']:+.4f}" if t["pnl"] is not None else "N/A"
            a(f"| {i} | {t['dir']} | ${t['dist']:.0f} | {t['outcome']} | {win_str} | {pnl_str} |")
        a("")
    else:
        a("*No post-migration trades at T+180 / dist ≥ $120 yet.*")
        a("")

    # ── Footer ───────────────────────────────────────────────────────────────
    a("---")
    a("*Read-only checkpoint. Strategy FROZEN. No real orders.*")

    report = "\n".join(lines)

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"Checkpoint written → {out}", file=sys.stderr)

    return report


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Rolling-anchor validation checkpoint")
    parser.add_argument("--signals", default=str(SIGNALS_PATH), help="Path to signals JSONL")
    parser.add_argument("--out", default=None, help="Write report to this file (optional)")
    args = parser.parse_args()

    path = Path(args.signals)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        sys.exit(1)

    records = load_signals(path)
    out_path = Path(args.out) if args.out else None
    report = generate_checkpoint(records, out=out_path)
    print(report)


if __name__ == "__main__":
    main()
