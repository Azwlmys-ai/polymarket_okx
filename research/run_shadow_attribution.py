"""
run_shadow_attribution.py — Shadow PnL Attribution (read-only analysis)

Usage:
    python3 research/run_shadow_attribution.py <shadow_events_file.jsonl>

Example:
    python3 research/run_shadow_attribution.py \
        research/shadow_execution_events_vps_20260527.jsonl

Output: stdout summary + research/shadow_attribution_result.md

NO side effects. Does not modify any file except the output report.
Does not touch VPS, services, or trading logic.
"""

from __future__ import annotations

import json
import sys
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

TAKER_FEE_RATE = 0.07
OUTPUT_PATH = Path(__file__).parent / "shadow_attribution_result.md"


def fee_adj_pnl(entry: float, payout: float) -> float:
    fee = TAKER_FEE_RATE * (1.0 - entry)
    return payout - entry - fee


def breakeven_fill(win_rate: float) -> float:
    """Numerically solve for fill price where EV = 0."""
    for fp_int in range(1, 1000):
        fp = fp_int / 1000
        ev = win_rate * fee_adj_pnl(fp, 1.0) + (1 - win_rate) * fee_adj_pnl(fp, 0.0)
        if ev < 0:
            return (fp_int - 1) / 1000
    return 1.0


def stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0, "mean": None, "sum": None, "min": None, "max": None,
                "p25": None, "p50": None, "p75": None}
    s = sorted(vals)
    n = len(s)
    return {
        "n": n,
        "mean": statistics.mean(vals),
        "sum": sum(vals),
        "min": s[0],
        "max": s[-1],
        "p25": s[n // 4],
        "p50": s[n // 2],
        "p75": s[3 * n // 4],
    }


def load_events(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def analyse(events: list[dict]) -> str:
    ts_run = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []
    a = lines.append

    a("# Shadow PnL Attribution Result")
    a(f"")
    a(f"> Generated: {ts_run}  ")
    a(f"> Events file: {len(events)} records  ")
    a(f"> Read-only analysis. No orders, no VPS changes.")
    a("")
    a("---")
    a("")

    # ── Split real-CLOB vs fallback ─────────────────────────────────────────
    real_clob = [e for e in events if not e.get("fallback_used") and
                 e.get("estimated_fill_price_10") is not None and
                 e.get("shadow_adjusted_pnl_10") is not None]
    fallback = [e for e in events if e.get("fallback_used")]
    unresolved = [e for e in events if e.get("paper_exit_price") is None]

    a("## 1. Event Breakdown")
    a("")
    a(f"| Category | Count | % of total |")
    a(f"|----------|-------|-----------|")
    n = len(events)
    a(f"| Total events | {n} | 100% |")
    a(f"| Real CLOB (fallback=False, pnl computed) | {len(real_clob)} | {len(real_clob)/n*100:.1f}% |")
    a(f"| Fallback (poly_ask used as fill) | {len(fallback)} | {len(fallback)/n*100:.1f}% |")
    a(f"| Unresolved (paper_exit_price=None) | {len(unresolved)} | {len(unresolved)/n*100:.1f}% |")
    a("")

    if not real_clob:
        a("⛔ **No real-CLOB events found. File may be the old fallback-only version.**")
        a("")
        a("Pull the current VPS file:")
        a("```bash")
        a("scp root@158.247.220.86:/opt/polymarket_okx/research/shadow_execution_events.jsonl \\")
        a("    research/shadow_execution_events_vps_$(date +%Y%m%d).jsonl")
        a("```")
        return "\n".join(lines)

    # ── Real-CLOB Attribution ───────────────────────────────────────────────
    a("## 2. Real-CLOB Attribution (primary result)")
    a("")

    pnl10 = [e["shadow_adjusted_pnl_10"] for e in real_clob]
    paper_pnl = [e["fee_adjusted_paper_pnl"] for e in real_clob
                 if e.get("fee_adjusted_paper_pnl") is not None]
    fill10 = [e["estimated_fill_price_10"] for e in real_clob]
    paper_entry = [e["paper_entry_price"] for e in real_clob
                   if e.get("paper_entry_price") is not None]
    degradation = [p - s for p, s in zip(paper_pnl, pnl10)
                   if p is not None]

    wins_shadow = [p for p in pnl10 if p > 0]
    losses_shadow = [p for p in pnl10 if p <= 0]
    win_rate_shadow = len(wins_shadow) / len(pnl10) if pnl10 else 0

    wins_paper = [p for p in paper_pnl if p > 0]
    win_rate_paper = len(wins_paper) / len(paper_pnl) if paper_pnl else 0

    mean_fill = statistics.mean(fill10) if fill10 else None
    be_fill = breakeven_fill(win_rate_shadow)

    a("### Key Numbers")
    a("")
    a(f"| Metric | Value |")
    a(f"|--------|-------|")
    a(f"| Real-CLOB events (n) | {len(real_clob)} |")
    a(f"| **Mean shadow_adjusted_pnl_10** | **{statistics.mean(pnl10):+.4f}** |")
    a(f"| Sum shadow PnL | {sum(pnl10):+.4f} |")
    a(f"| Shadow win rate | {win_rate_shadow:.3f} ({win_rate_shadow*100:.1f}%) |")
    a(f"| Paper win rate (same events) | {win_rate_paper:.3f} ({win_rate_paper*100:.1f}%) |")
    a(f"| Mean fill price (CLOB ask) | {mean_fill:.4f} |")
    a(f"| Mean paper entry price | {statistics.mean(paper_entry):.4f} |")
    a(f"| Mean price degradation (fill - paper) | {statistics.mean(fill10) - statistics.mean(paper_entry):+.4f} |")
    a(f"| Break-even fill @ actual win rate | {be_fill:.3f} |")
    a(f"| Mean degradation vs paper PnL | {statistics.mean(degradation):+.4f} |")
    a("")

    mean_shadow_pnl = statistics.mean(pnl10)
    if mean_shadow_pnl > 0.02:
        verdict = "🟢 **TENTATIVE GO** — Mean shadow PnL positive and meaningful. Gather 300+ events before live."
    elif mean_shadow_pnl > 0.005:
        verdict = "🟡 **MARGINAL** — Positive but near-zero EV. High sensitivity to fill price. NO-GO until 300+ events confirm."
    elif mean_shadow_pnl > 0:
        verdict = "🟡 **NEAR-ZERO EV** — Statistically indistinguishable from zero at this sample size. NO-GO."
    else:
        verdict = "🔴 **NO-GO** — Mean shadow PnL ≤ 0. Strategy edge does not survive real CLOB fills."

    a(f"### Verdict: {verdict}")
    a("")

    # ── Fill price distribution ─────────────────────────────────────────────
    a("## 3. Fill Price Distribution (real CLOB)")
    a("")
    fill_stats = stats(fill10)
    a(f"| Stat | Value |")
    a(f"|------|-------|")
    for k, v in fill_stats.items():
        a(f"| {k} | {f'{v:.4f}' if isinstance(v, float) else v} |")
    a("")
    buckets = {"<0.80": 0, "0.80–0.89": 0, "0.90–0.93": 0, "0.94": 0,
               "0.95–0.97": 0, "0.98–0.99": 0, "1.00": 0}
    for f in fill10:
        if f < 0.80:
            buckets["<0.80"] += 1
        elif f < 0.90:
            buckets["0.80–0.89"] += 1
        elif f < 0.94:
            buckets["0.90–0.93"] += 1
        elif f < 0.95:
            buckets["0.94"] += 1
        elif f < 0.98:
            buckets["0.95–0.97"] += 1
        elif f < 1.00:
            buckets["0.98–0.99"] += 1
        else:
            buckets["1.00"] += 1
    a("| Fill bucket | Count | EV at that price |")
    a("|-------------|-------|-----------------|")
    for bucket, count in buckets.items():
        if count == 0:
            continue
        try:
            mid_fp = {"<0.80": 0.75, "0.80–0.89": 0.84, "0.90–0.93": 0.915,
                      "0.94": 0.94, "0.95–0.97": 0.96, "0.98–0.99": 0.985, "1.00": 1.0}[bucket]
            ev = win_rate_shadow * fee_adj_pnl(mid_fp, 1.0) + (1 - win_rate_shadow) * fee_adj_pnl(mid_fp, 0.0)
            a(f"| {bucket} | {count} | {ev:+.4f} |")
        except Exception:
            a(f"| {bucket} | {count} | N/A |")
    a("")

    # ── By checkpoint ──────────────────────────────────────────────────────
    a("## 4. Breakdown by Checkpoint")
    a("")
    a("| Checkpoint | n | Win rate | Mean fill | Mean shadow PnL | Verdict |")
    a("|------------|---|----------|-----------|-----------------|---------|")
    cp_groups: dict[str, list] = defaultdict(list)
    for e in real_clob:
        cp_groups[e.get("checkpoint_time", "?")].append(e)
    for cp, evts in sorted(cp_groups.items()):
        cp_pnl = [e["shadow_adjusted_pnl_10"] for e in evts]
        cp_fill = [e["estimated_fill_price_10"] for e in evts]
        cp_wr = sum(1 for p in cp_pnl if p > 0) / len(cp_pnl)
        v = "✅" if statistics.mean(cp_pnl) > 0.005 else ("⚠️" if statistics.mean(cp_pnl) > 0 else "❌")
        a(f"| {cp} | {len(evts)} | {cp_wr*100:.1f}% | {statistics.mean(cp_fill):.3f} | {statistics.mean(cp_pnl):+.4f} | {v} |")
    a("")

    # ── By dist bucket ─────────────────────────────────────────────────────
    a("## 5. Breakdown by Distance Bucket")
    a("")
    a("| Dist bucket | n | Win rate | Mean fill | Mean shadow PnL | Verdict |")
    a("|-------------|---|----------|-----------|-----------------|---------|")
    dist_groups: dict[str, list] = defaultdict(list)
    for e in real_clob:
        d = e.get("distance") or e.get("dist") or 0
        if d < 150:
            bucket = "130–149"
        elif d < 180:
            bucket = "150–179"
        elif d < 220:
            bucket = "180–219"
        else:
            bucket = "220+"
        dist_groups[bucket].append(e)
    for bucket in ["130–149", "150–179", "180–219", "220+"]:
        evts = dist_groups.get(bucket, [])
        if not evts:
            continue
        dp = [e["shadow_adjusted_pnl_10"] for e in evts]
        df = [e["estimated_fill_price_10"] for e in evts]
        dw = sum(1 for p in dp if p > 0) / len(dp)
        v = "✅" if statistics.mean(dp) > 0.005 else ("⚠️" if statistics.mean(dp) > 0 else "❌")
        a(f"| dist {bucket} | {len(evts)} | {dw*100:.1f}% | {statistics.mean(df):.3f} | {statistics.mean(dp):+.4f} | {v} |")
    a("")

    # ── Fallback analysis ──────────────────────────────────────────────────
    a("## 6. Fallback Events (non-attributable)")
    a("")
    if fallback:
        fb_pnl = [e["fee_adjusted_paper_pnl"] for e in fallback
                  if e.get("fee_adjusted_paper_pnl") is not None]
        fb_wr = sum(1 for p in fb_pnl if p > 0) / len(fb_pnl) if fb_pnl else 0
        a(f"Fallback events: {len(fallback)}. These used poly_ask (~0.51) as fill — not attributable.")
        a(f"Paper win rate on fallback events: {fb_wr*100:.1f}% (for comparison only)")
        a("")
        if abs(fb_wr - win_rate_paper) > 0.05:
            a(f"⚠️ Fallback win rate ({fb_wr*100:.1f}%) differs from real-CLOB win rate ({win_rate_paper*100:.1f}%) "
              f"by >{5:.0f}pp — selection bias risk. Monitor.")
        else:
            a(f"✅ Fallback win rate similar to real-CLOB. No selection bias detected.")
    else:
        a("No fallback events in this file.")
    a("")

    # ── Hard gates ─────────────────────────────────────────────────────────
    a("## 7. Hard Gate Status for Live Consideration")
    a("")
    a("| Gate | Threshold | Actual | Status |")
    a("|------|-----------|--------|--------|")
    g1 = statistics.mean(pnl10)
    g1_ok = g1 > 0.02
    a(f"| G1: Mean shadow PnL | >+0.020 | {g1:+.4f} | {'✅' if g1_ok else '❌'} |")
    g2 = len(real_clob)
    g2_ok = g2 >= 300
    a(f"| G2: Real-CLOB event count | ≥300 | {g2} | {'✅' if g2_ok else '❌'} |")
    g3 = len(fallback) / n if n else 1
    g3_ok = g3 < 0.20
    a(f"| G3: Fallback rate | <20% | {g3*100:.1f}% | {'✅' if g3_ok else '❌'} |")
    g4 = win_rate_shadow
    g4_ok = g4 >= 0.90
    a(f"| G4: Real-CLOB win rate | ≥90% | {g4*100:.1f}% | {'✅' if g4_ok else '❌'} |")
    a("")
    gates_passed = sum([g1_ok, g2_ok, g3_ok, g4_ok])
    a(f"**Gates passed: {gates_passed}/4**")
    a("")
    if gates_passed == 4:
        a("🟢 All gates cleared. May proceed to small live consideration (50–100 USDC max, execution layer required first).")
    elif gates_passed >= 2 and g1_ok:
        a("🟡 G1 positive but not all gates cleared. Continue accumulating data. Do not go live.")
    else:
        a("🔴 GO conditions not met. Do not proceed to live trading.")

    a("")
    a("---")
    a("*Attribution is read-only. No code was changed. No orders were placed.*")

    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 run_shadow_attribution.py <shadow_events.jsonl>")
        print("")
        print("To get fresh VPS data:")
        print("  scp root@158.247.220.86:/opt/polymarket_okx/research/shadow_execution_events.jsonl \\")
        print("      research/shadow_execution_events_vps_$(date +%Y%m%d).jsonl")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    events = load_events(path)
    print(f"Loaded {len(events)} events from {path}")

    report = analyse(events)
    print(report)

    OUTPUT_PATH.write_text(report, encoding="utf-8")
    print(f"\nReport written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
