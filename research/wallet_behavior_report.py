"""
wallet_behavior_report.py - Markdown report for wallet BTC 5m behavior.

Usage:
    python3 research/wallet_behavior_report.py

Output:
    research/wallet_0xe022_behavior_report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SHORT_USER = "0xe022"
DEFAULT_IN = Path(f"research/wallet_{SHORT_USER}_enriched_markets.jsonl")
DEFAULT_REPORT = Path(f"research/wallet_{SHORT_USER}_behavior_report.md")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return [{"_report_input_error": True, "path": str(path), "error": "missing_file"}]
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except json.JSONDecodeError as exc:
                rows.append({"_report_input_error": True, "path": str(path), "line": line_no, "error": str(exc)})
    return rows


def pct(num: int | float, den: int | float) -> str:
    if not den:
        return "0.0%"
    return f"{(num / den) * 100:.1f}%"


def bucket_dist(rows: list[dict[str, Any]]) -> Counter:
    counter: Counter = Counter()
    for row in rows:
        counter.update(row.get("all_entry_buckets") or {})
    return counter


def holding_bucket(seconds: int | float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 30:
        return "0-30"
    if seconds < 60:
        return "30-60"
    if seconds < 120:
        return "60-120"
    if seconds < 180:
        return "120-180"
    if seconds < 300:
        return "180-300"
    return "300+"


def table(counter: Counter, order: list[str]) -> list[str]:
    lines = ["| Bucket | Count |", "|---|---:|"]
    for key in order:
        lines.append(f"| `{key}` | {counter.get(key, 0)} |")
    for key in sorted(k for k in counter if k not in order):
        lines.append(f"| `{key}` | {counter[key]} |")
    return lines


def late_entry_signal(rows: list[dict[str, Any]], buckets: Counter) -> tuple[bool, str]:
    total_entries = sum(buckets.values())
    late = buckets.get("60-90", 0) + buckets.get("90-120", 0)
    very_late = buckets.get("0-30", 0) + buckets.get("30-60", 0)
    multi_add = sum(1 for row in rows if row.get("buy_count", 0) >= 2)
    signal = total_entries > 0 and (late / total_entries >= 0.25 or very_late / total_entries >= 0.25) and multi_add > 0
    reason = (
        f"last 60-120s entries={late}/{total_entries}, last 0-60s entries={very_late}/{total_entries}, "
        f"markets with >=2 buys={multi_add}/{len(rows)}"
    )
    return signal, reason


def quality_counter(rows: list[dict[str, Any]]) -> Counter:
    counter: Counter = Counter()
    for row in rows:
        flags = row.get("data_quality") or []
        if not flags:
            counter["ok"] += 1
        else:
            counter.update(flags)
    for row in rows:
        if row.get("_enrich_error") or row.get("_report_input_error"):
            counter["pipeline_error"] += 1
    return counter


def metadata_source_counter(rows: list[dict[str, Any]]) -> Counter:
    counter: Counter = Counter()
    for row in rows:
        counter[str(row.get("metadata_source") or "unknown")] += 1
    return counter


def missing_reason_counter(rows: list[dict[str, Any]]) -> Counter:
    counter: Counter = Counter()
    for row in rows:
        if row.get("metadata_missing"):
            reason = str(row.get("metadata_missing_reason") or "unknown")
            counter[reason] += 1
    return counter


def render_report(rows: list[dict[str, Any]], input_path: Path) -> str:
    usable = [row for row in rows if not row.get("_enrich_error") and not row.get("_report_input_error")]
    total = len(usable)
    btc = [row for row in usable if row.get("is_btc_5m_candidate")]
    confirmed_metadata = [row for row in btc if row.get("metadata_confirmed")]
    metadata_missing_rows = [row for row in btc if row.get("metadata_missing")]
    str_valid = [
        row for row in btc
        if row.get("seconds_to_resolution") is not None
        and row.get("market_end_ts_confirmed")
        and "missing_end_ts" not in (row.get("data_quality") or [])
    ]
    flips = [row for row in btc if row.get("has_confirmed_flip")]
    flip_uncertain = [row for row in btc if row.get("flip_uncertain")]
    adders = [row for row in btc if row.get("buy_count", 0) >= 2]
    active_exits = [row for row in btc if row.get("exit_classification") == "active_exit"]
    buckets = bucket_dist(btc)
    holding = Counter(holding_bucket(row.get("holding_seconds")) for row in btc)
    quality = quality_counter(rows)
    sources = metadata_source_counter(btc)
    missing_reasons = missing_reason_counter(btc)
    late_signal, late_reason = late_entry_signal(btc, buckets)
    metadata_ok_rate = len(confirmed_metadata) / len(btc) if btc else 0.0
    confirmed_flip_rate = len(flips) / len(btc) if btc else 0.0
    shadow_worth = (
        bool(btc)
        and len(str_valid) / len(btc) >= 0.8
        and metadata_ok_rate >= 0.5
        and (late_signal or confirmed_flip_rate >= 0.25)
        and len(flip_uncertain) / len(btc) < 0.5
    )
    shadow_reason = (
        f"metadata_ok_rate={metadata_ok_rate:.1%}, STR_valid={pct(len(str_valid), len(btc))}, "
        f"confirmed_flip_rate={confirmed_flip_rate:.1%}, flip_uncertain_rate={pct(len(flip_uncertain), len(btc))}, "
        f"late_signal={late_signal}"
    )

    lines = [
        "# Wallet 0xe022 Behavior Research Report",
        "",
        f"- Generated: `{utc_now_iso()}`",
        f"- Input: `{input_path}`",
        "",
        "## Summary",
        "",
        f"- Sample scale: `{total}` enriched markets",
        f"- BTC 5m candidates: `{len(btc)}`",
        f"- confirmed_metadata_count: `{len(confirmed_metadata)}`",
        f"- metadata_missing_count: `{len(metadata_missing_rows)}`",
        f"- STR_confirmed_count: `{len(str_valid)}`",
        f"- STR confirmed rate: `{pct(len(str_valid), len(btc))}` ({len(str_valid)}/{len(btc)})",
        f"- confirmed_flip_rate: `{pct(len(flips), len(btc))}` ({len(flips)}/{len(btc)})",
        f"- flip_uncertain_count: `{len(flip_uncertain)}`",
        f"- Active exit markets: `{len(active_exits)}`",
        f"- Batch add markets (`buy_count >= 2`): `{len(adders)}`",
        f"- Last 60-120 second concentrated entry/add signal: `{late_signal}` ({late_reason})",
        f"- Worth entering VPS shadow test: `{'YES' if shadow_worth else 'NO'}` ({shadow_reason})",
        "",
        "## Entry Bucket Distribution",
        "",
        *table(buckets, ["0-30", "30-60", "60-90", "90-120", "120-180", "180-240", "240-300", "out_of_range"]),
        "",
        "## Holding Duration Distribution",
        "",
        *table(holding, ["0-30", "30-60", "60-120", "120-180", "180-300", "300+", "unknown"]),
        "",
        "## Batch Add Statistics",
        "",
        f"- Markets with at least 2 buys: `{len(adders)}`",
        f"- Max buy count in a market: `{max((row.get('buy_count', 0) for row in btc), default=0)}`",
        f"- Total buy size across BTC 5m candidates: `{round(sum(float(row.get('total_buy_size') or 0) for row in btc), 8)}`",
        f"- Total sell size across BTC 5m candidates: `{round(sum(float(row.get('total_sell_size') or 0) for row in btc), 8)}`",
        "",
        "## Data Quality",
        "",
        *table(quality, sorted(quality)),
        "",
        "## Metadata Sources",
        "",
        *table(sources, sorted(sources)),
        "",
        "## Metadata Missing Reasons",
        "",
        *table(missing_reasons, sorted(missing_reasons)),
        "",
        "## Notes",
        "",
        "- This report is based only on public read-only API data collected by the Phase 2 scripts.",
        "- No orders were placed.",
        "- No live trading connection was made.",
        "- `mvp_runner.py` and the main strategy were not modified.",
        "- Existing VPS systemd services were not modified.",
        "- The running `paper_anchor_sim.py` workflow was not touched.",
    ]

    if any(row.get("_enrich_error") or row.get("_report_input_error") for row in rows):
        lines.extend([
            "",
            "## Pipeline Errors",
            "",
            "One or more upstream errors were present. See enriched JSONL rows for full error details.",
        ])

    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render wallet behavior Markdown report.")
    parser.add_argument("--input", type=Path, default=DEFAULT_IN)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = read_jsonl(args.input)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_report(rows, args.input), encoding="utf-8")
    print(f"behavior_report: {args.report}")
    print("scope: report only; no HTTP; no trading; no strategy integration")
    return 0


if __name__ == "__main__":
    sys.exit(main())
