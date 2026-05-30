"""
backfill_shadow_outcomes.py — Backfill paper_exit_price into shadow events

Usage:
    python3 research/backfill_shadow_outcomes.py \
        research/shadow_execution_events_vps_20260527.jsonl \
        research/paper_anchor_signals_vps_20260527.jsonl \
        research/shadow_events_backfilled.jsonl

Inputs:
    arg1: shadow_execution_events file (JSONL, per-checkpoint)
    arg2: paper_anchor_signals file (JSONL, per-window with outcome field)
           OR paper_anchor_signal_events.jsonl (streaming events with event_type)
    arg3: output file path

What it does:
    1. Loads the signals file and extracts slug → outcome mappings
    2. For each shadow event missing paper_exit_price, looks up the outcome by slug
    3. Maps outcome to paper_exit_price:
       - outcome matches signal direction → paper_exit_price = 1.0 (win)
       - outcome does not match → paper_exit_price = 0.0 (loss)
    4. Recomputes shadow_adjusted_pnl_10/25/50 and fee_adjusted_paper_pnl
    5. Writes backfilled JSONL

No side effects on input files. Output is a new file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TAKER_FEE_RATE = 0.07


def fee_adj_pnl(entry: float, payout: float) -> float:
    fee = TAKER_FEE_RATE * (1.0 - entry)
    return payout - entry - fee


def load_jsonl(path: Path) -> list[dict]:
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


def build_outcome_map(signal_records: list[dict]) -> dict[str, str]:
    """
    Build slug → outcome (UP/DOWN) map from signals file.

    Supports two formats:
    1. Per-window format: each record has 'slug' and 'outcome' fields
    2. Streaming events format: records have 'event_type' field
       - 'signal_resolved' or 'window_resolved' events carry 'outcome'
    """
    outcome_map: dict[str, str] = {}

    for rec in signal_records:
        slug = rec.get("slug")
        if not slug:
            continue

        event_type = rec.get("event_type", "")

        # Format 1: per-window (no event_type field, has 'outcome' directly)
        if not event_type and rec.get("outcome") in ("UP", "DOWN"):
            outcome_map[slug] = rec["outcome"]
            continue

        # Format 2: streaming events — resolved event types
        if event_type in ("signal_resolved", "window_resolved", "resolved"):
            outcome = rec.get("outcome") or rec.get("resolution") or rec.get("result")
            if outcome in ("UP", "DOWN"):
                outcome_map[slug] = outcome
            continue

        # Format 2b: signal_started with embedded outcome (rare)
        if event_type == "signal_started" and rec.get("outcome") in ("UP", "DOWN"):
            outcome_map[slug] = rec["outcome"]

    return outcome_map


def backfill(shadow_events: list[dict], outcome_map: dict[str, str]) -> tuple[list[dict], dict]:
    """
    Backfill paper_exit_price and recompute PnL fields.
    Returns (backfilled_events, stats_dict).
    """
    filled = 0
    already_had = 0
    slug_not_found = 0
    still_missing = 0

    result = []
    for e in shadow_events:
        e = dict(e)  # shallow copy

        # Already has outcome — don't overwrite
        if e.get("paper_exit_price") is not None:
            already_had += 1
            result.append(e)
            continue

        slug = e.get("slug", "")
        direction = e.get("direction") or e.get("side", "")
        outcome = outcome_map.get(slug)

        if outcome is None:
            slug_not_found += 1
            result.append(e)
            continue

        # Map outcome to paper_exit_price
        # If the market outcome matches our predicted direction → win (1.0)
        paper_exit = 1.0 if outcome == direction else 0.0
        e["paper_exit_price"] = paper_exit

        # Recompute paper PnL using paper_entry_price
        paper_entry = e.get("paper_entry_price")
        if paper_entry is not None:
            e["fee_adjusted_paper_pnl"] = fee_adj_pnl(paper_entry, paper_exit)

        # Recompute shadow PnL at $10/$25/$50
        for size_tag in ("10", "25", "50"):
            fill_key = f"estimated_fill_price_{size_tag}"
            pnl_key = f"shadow_adjusted_pnl_{size_tag}"
            fill = e.get(fill_key)
            if fill is not None and not e.get("fallback_used"):
                e[pnl_key] = fee_adj_pnl(fill, paper_exit)
            elif fill is not None and e.get("fallback_used"):
                # Fallback uses paper entry as effective fill
                e[pnl_key] = fee_adj_pnl(fill, paper_exit)

        filled += 1
        result.append(e)

    # Count still missing after backfill
    still_missing = sum(1 for e in result if e.get("paper_exit_price") is None)

    stats = {
        "total": len(result),
        "already_had_outcome": already_had,
        "backfilled": filled,
        "slug_not_found": slug_not_found,
        "still_missing": still_missing,
    }
    return result, stats


def main() -> None:
    if len(sys.argv) < 4:
        print("Usage: python3 backfill_shadow_outcomes.py <shadow.jsonl> <signals.jsonl> <output.jsonl>")
        print()
        print("Pull VPS files:")
        print("  scp root@158.247.220.86:/opt/polymarket_okx/research/paper_anchor_signal_events.jsonl \\")
        print("      research/paper_anchor_signal_events_vps_$(date +%Y%m%d).jsonl")
        print("  # OR if per-window format exists:")
        print("  scp root@158.247.220.86:/opt/polymarket_okx/research/paper_anchor_signals.jsonl \\")
        print("      research/paper_anchor_signals_vps_$(date +%Y%m%d).jsonl")
        sys.exit(1)

    shadow_path = Path(sys.argv[1])
    signals_path = Path(sys.argv[2])
    output_path = Path(sys.argv[3])

    if not shadow_path.exists():
        print(f"Error: shadow file not found: {shadow_path}")
        sys.exit(1)
    if not signals_path.exists():
        print(f"Error: signals file not found: {signals_path}")
        sys.exit(1)

    print(f"Loading shadow events: {shadow_path}")
    shadow_events = load_jsonl(shadow_path)
    print(f"  {len(shadow_events)} events loaded")

    print(f"\nLoading signal outcomes: {signals_path}")
    signal_records = load_jsonl(signals_path)
    print(f"  {len(signal_records)} records loaded")

    outcome_map = build_outcome_map(signal_records)
    print(f"  Outcome map: {len(outcome_map)} slug→outcome entries")

    if not outcome_map:
        print("\n⛔ No outcomes extracted from signals file.")
        print("Check file format. Expected fields: 'slug' + 'outcome' (UP/DOWN)")
        print("\nSample from signals file:")
        for r in signal_records[:2]:
            print(f"  {json.dumps(r)[:200]}")
        sys.exit(1)

    print(f"\nBackfilling outcomes...")
    backfilled, stats = backfill(shadow_events, outcome_map)

    print(f"\nBackfill stats:")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    # Write output
    output_path.write_text(
        "\n".join(json.dumps(e) for e in backfilled) + "\n",
        encoding="utf-8"
    )
    print(f"\nOutput written to: {output_path}")
    print(f"\nNext: run attribution on backfilled file:")
    print(f"  python3 research/run_shadow_attribution.py {output_path}")


if __name__ == "__main__":
    main()
