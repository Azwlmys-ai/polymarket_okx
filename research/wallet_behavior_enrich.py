"""
wallet_behavior_enrich.py - offline enrichment for wallet BTC 5m behavior.

Reads collector JSONL outputs and writes per-market behavior summaries.

Usage:
    python3 research/wallet_behavior_enrich.py
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
DEFAULT_TRADES = Path(f"research/wallet_{SHORT_USER}_trades_raw.jsonl")
DEFAULT_MARKETS = Path(f"research/wallet_{SHORT_USER}_markets_raw.jsonl")
DEFAULT_OUT = Path(f"research/wallet_{SHORT_USER}_enriched_markets.jsonl")

BUCKETS = [
    ("0-30", 0, 30),
    ("30-60", 30, 60),
    ("60-90", 60, 90),
    ("90-120", 90, 120),
    ("120-180", 120, 180),
    ("180-240", 180, 240),
    ("240-300", 240, 300),
]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return [{"_input_error": True, "path": str(path), "error": "missing_file"}]
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
                rows.append({"_input_error": True, "path": str(path), "line": line_no, "error": str(exc)})
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(rows)


def parse_ts(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value)
    if text.isdigit():
        return int(text)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except ValueError:
        return None


def market_end_ts(market: dict[str, Any]) -> tuple[int | None, str | None, bool]:
    for key in ("endDate", "closedTime", "umaEndDate", "eventEndTime"):
        ts = parse_ts(market.get(key))
        if ts:
            return ts, key, bool(market.get("metadata_confirmed"))
    events = market.get("events") or []
    if isinstance(events, list):
        for event in events:
            if isinstance(event, dict):
                for key in ("endDate", "closedTime", "endTime"):
                    ts = parse_ts(event.get(key))
                    if ts:
                        return ts, f"events.{key}", bool(market.get("metadata_confirmed"))
    event = market.get("event") or {}
    if isinstance(event, dict):
        for key in ("endDate", "closedTime"):
            ts = parse_ts(event.get(key))
            if ts:
                return ts, f"event.{key}", bool(market.get("metadata_confirmed"))
    return None, None, False


def market_text(row: dict[str, Any]) -> str:
    keys = ("title", "slug", "eventSlug", "question", "description", "seriesSlug", "icon")
    return " ".join(str(row.get(k, "")) for k in keys).lower()


def is_btc_5m_candidate(row: dict[str, Any]) -> bool:
    text = market_text(row)
    return ("btc" in text or "bitcoin" in text) and "5m" in text and ("updown" in text or "up or down" in text)


def entry_bucket(seconds_to_resolution: int | float | None) -> str:
    if seconds_to_resolution is None:
        return "out_of_range"
    for name, lo, hi in BUCKETS:
        if lo <= seconds_to_resolution < hi:
            return name
    return "out_of_range"


def data_quality_flags(market: dict[str, Any], trades: list[dict[str, Any]], end_ts: int | None) -> list[str]:
    flags: list[str] = []
    if market.get("metadata_missing"):
        flags.append("metadata_missing")
    if not market.get("metadata_confirmed"):
        flags.append("metadata_unconfirmed")
    if end_ts is None:
        flags.append("missing_end_ts")
    if not trades:
        flags.append("no_trades")
    if any(t.get("_collector_error") for t in trades):
        flags.append("collector_error")
    if any(t.get("_input_error") for t in trades):
        flags.append("input_error")
    return flags


def outcome_key(trade: dict[str, Any]) -> str:
    if trade.get("outcome") is not None:
        return str(trade.get("outcome"))
    if trade.get("outcomeIndex") is not None:
        return str(trade.get("outcomeIndex"))
    if trade.get("asset") is not None:
        return str(trade.get("asset"))
    return "unknown"


def trade_size(trade: dict[str, Any]) -> float:
    try:
        return float(trade.get("size") or 0)
    except (TypeError, ValueError):
        return 0.0


def flip_analysis(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Confirm a flip only when a later trade makes the opposite outcome the main
    net exposure after a prior main exposure existed. With BUY-only histories
    where both sides appear but no main exposure switch is observable, mark the
    flip as uncertain instead of forcing it.
    """
    net: dict[str, float] = {}
    previous_main: str | None = None
    confirmed_count = 0
    uncertain = False
    reasons: list[str] = []
    switches: list[dict[str, Any]] = []

    for trade in trades:
        side = str(trade.get("side") or "").upper()
        outcome = outcome_key(trade)
        size = trade_size(trade)
        if side == "BUY":
            delta = size
        elif side == "SELL":
            delta = -size
        else:
            uncertain = True
            reasons.append("unknown_side")
            continue

        before = dict(net)
        prior_main = max((k for k, v in before.items() if v > 0), key=lambda k: before[k], default=None)
        net[outcome] = round(net.get(outcome, 0.0) + delta, 8)
        after_main = max((k for k, v in net.items() if v > 0), key=lambda k: net[k], default=None)

        if prior_main and after_main and prior_main != after_main:
            previous_main_size = before.get(prior_main, 0.0)
            new_main_size = net.get(after_main, 0.0)
            if new_main_size > previous_main_size:
                confirmed_count += 1
                switches.append({
                    "ts": parse_ts(trade.get("timestamp")),
                    "from": prior_main,
                    "to": after_main,
                    "previous_main_size": previous_main_size,
                    "new_main_size": new_main_size,
                })
            else:
                uncertain = True
                reasons.append("opposite_trade_without_main_exposure_switch")
        previous_main = after_main or previous_main

    positive_outcomes = [k for k, v in net.items() if v > 0]
    buy_outcomes = {
        outcome_key(t)
        for t in trades
        if str(t.get("side") or "").upper() == "BUY" and trade_size(t) > 0
    }
    sell_count = sum(1 for t in trades if str(t.get("side") or "").upper() == "SELL")
    if confirmed_count == 0 and len(buy_outcomes) > 1:
        uncertain = True
        if sell_count == 0:
            reasons.append("both_outcomes_bought_without_sells")
        else:
            reasons.append("both_outcomes_seen_without_confirmed_net_switch")

    strict_confirmed_count = 0 if uncertain else confirmed_count
    return {
        "observed_net_switch_count": confirmed_count,
        "confirmed_flip_count": strict_confirmed_count,
        "has_confirmed_flip": strict_confirmed_count > 0,
        "flip_uncertain": uncertain,
        "flip_uncertain_reasons": sorted(set(reasons)),
        "final_net_by_outcome": net,
        "positive_net_outcomes": positive_outcomes,
        "main_net_outcome": max(positive_outcomes, key=lambda k: net[k], default=None),
        "observed_net_switches": switches,
        "confirmed_flip_switches": [] if uncertain else switches,
    }


def enrich_market(condition_id: str, trades: list[dict[str, Any]], market: dict[str, Any]) -> dict[str, Any]:
    clean_trades = [t for t in trades if not t.get("_collector_error") and not t.get("_input_error")]
    clean_trades.sort(key=lambda t: parse_ts(t.get("timestamp")) or 0)
    end_ts, end_ts_source, end_ts_confirmed = market_end_ts(market)

    trade_ts = [parse_ts(t.get("timestamp")) for t in clean_trades]
    trade_ts = [ts for ts in trade_ts if ts is not None]
    first_ts = min(trade_ts) if trade_ts else None
    last_ts = max(trade_ts) if trade_ts else None
    first_str = (end_ts - first_ts) if end_ts_confirmed and end_ts is not None and first_ts is not None else None
    seconds_values = [(end_ts - ts) for ts in trade_ts] if end_ts_confirmed and end_ts is not None else []

    sides = [str(t.get("side") or "").upper() for t in clean_trades]
    outcomes = [outcome_key(t) for t in clean_trades]
    flip = flip_analysis(clean_trades)

    buy_trades = [t for t in clean_trades if str(t.get("side") or "").upper() == "BUY"]
    sell_trades = [t for t in clean_trades if str(t.get("side") or "").upper() == "SELL"]
    total_buy_size = round(sum(float(t.get("size") or 0) for t in buy_trades), 8)
    total_sell_size = round(sum(float(t.get("size") or 0) for t in sell_trades), 8)

    merged_for_candidate = dict(market)
    if clean_trades:
        merged_for_candidate.update(clean_trades[0])

    flags = data_quality_flags(market, trades, end_ts)
    if not is_btc_5m_candidate(merged_for_candidate):
        flags.append("not_btc_5m_candidate")
    if seconds_values and any(v < 0 or v > 300 for v in seconds_values):
        flags.append("str_out_of_5m_range")
    if end_ts is not None and not end_ts_confirmed:
        flags.append("unconfirmed_end_ts")
    if flip["flip_uncertain"]:
        flags.append("flip_uncertain")

    return {
        "conditionId": condition_id,
        "slug": market.get("slug") or (clean_trades[0].get("slug") if clean_trades else None),
        "title": market.get("question") or market.get("title") or (clean_trades[0].get("title") if clean_trades else None),
        "market_end_ts": end_ts,
        "market_end_ts_source": end_ts_source,
        "market_end_ts_confirmed": end_ts_confirmed,
        "is_btc_5m_candidate": "not_btc_5m_candidate" not in flags,
        "trade_count": len(clean_trades),
        "first_trade_ts": first_ts,
        "last_trade_ts": last_ts,
        "seconds_to_resolution": first_str,
        "first_entry_str": first_str,
        "entry_bucket": entry_bucket(first_str),
        "all_entry_buckets": dict(Counter(entry_bucket(v) for v in seconds_values)),
        "buy_count": len(buy_trades),
        "sell_count": len(sell_trades),
        "total_buy_size": total_buy_size,
        "total_sell_size": total_sell_size,
        "flip_count": flip["confirmed_flip_count"],
        "has_flip": flip["has_confirmed_flip"],
        "observed_net_switch_count": flip["observed_net_switch_count"],
        "observed_net_switches": flip["observed_net_switches"],
        "confirmed_flip_count": flip["confirmed_flip_count"],
        "has_confirmed_flip": flip["has_confirmed_flip"],
        "flip_uncertain": flip["flip_uncertain"],
        "flip_uncertain_reasons": flip["flip_uncertain_reasons"],
        "final_net_by_outcome": flip["final_net_by_outcome"],
        "main_net_outcome": flip["main_net_outcome"],
        "confirmed_flip_switches": flip["confirmed_flip_switches"],
        "holding_seconds": (last_ts - first_ts) if first_ts is not None and last_ts is not None else None,
        "exit_classification": "active_exit" if sell_trades else "hold_to_resolution",
        "metadata_missing": bool(market.get("metadata_missing")),
        "metadata_confirmed": bool(market.get("metadata_confirmed")),
        "metadata_source": market.get("metadata_source") or market.get("source"),
        "metadata_missing_reason": market.get("metadata_missing_reason"),
        "metadata_resolution_attempts": market.get("metadata_resolution_attempts") or [],
        "data_quality": flags,
        "outcome_counts": dict(Counter(outcomes)),
        "side_counts": dict(Counter(sides)),
    }


def enrich(trades_path: Path, markets_path: Path) -> list[dict[str, Any]]:
    trades = read_jsonl(trades_path)
    markets = read_jsonl(markets_path)

    if any(row.get("_collector_error") or row.get("_input_error") for row in trades + markets):
        errors = [row for row in trades + markets if row.get("_collector_error") or row.get("_input_error")]
        if not any(not row.get("_collector_error") and not row.get("_input_error") for row in trades):
            return [{
                "_enrich_error": True,
                "conditionId": None,
                "trade_count": 0,
                "is_btc_5m_candidate": False,
                "data_quality": ["collector_or_input_error"],
                "errors": errors,
            }]

    by_condition: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        if trade.get("_collector_error") or trade.get("_input_error"):
            continue
        cid = str(trade.get("conditionId") or "")
        if cid:
            by_condition.setdefault(cid, []).append(trade)

    markets_by_condition = {
        str(m.get("conditionId") or ""): m
        for m in markets
        if isinstance(m, dict) and m.get("conditionId")
    }

    rows: list[dict[str, Any]] = []
    for condition_id, condition_trades in sorted(by_condition.items()):
        market = markets_by_condition.get(condition_id) or {
            "conditionId": condition_id,
            "metadata_missing": True,
            "metadata_missing_reason": "market metadata row not found",
        }
        rows.append(enrich_market(condition_id, condition_trades, market))
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enrich read-only wallet behavior samples.")
    parser.add_argument("--trades", type=Path, default=DEFAULT_TRADES)
    parser.add_argument("--markets", type=Path, default=DEFAULT_MARKETS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = enrich(args.trades, args.markets)
    n = write_jsonl(args.out, rows)
    print(f"enriched_markets: {n} rows -> {args.out}")
    print("scope: offline enrichment only; no HTTP; no trading; no strategy integration")
    return 0


if __name__ == "__main__":
    sys.exit(main())
