"""
shadow_execution_recorder.py - offline SHADOW_ONLY execution feasibility analysis.

Reads research/paper_anchor_signals.jsonl and estimates how much paper edge may
survive taker execution assumptions. This module never sends orders and never
uses private keys.

Usage:
  python3 research/shadow_execution_recorder.py
  python3 research/shadow_execution_recorder.py --threshold 120 --notional-usdc 10
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.paper_anchor_sim import Checkpoint, WindowRecord

SHADOW_ONLY = True
DEFAULT_THRESHOLD = 130
DEFAULT_NOTIONAL_USDC = 10.0
DEFAULT_MAX_ONLINE_RESOLVES = 50
CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
SIGNALS_PATH = Path("research/paper_anchor_signals.jsonl")
FRESH_SIGNAL_EVENTS_PATH = Path("research/paper_anchor_signal_events.jsonl")
EVENTS_PATH = Path("research/shadow_execution_events.jsonl")
REPORT_PATH = Path("research/shadow_execution_report.md")
SUMMARY_THRESHOLDS = [120, 130, 150]
ORDER_SIZES = [10, 25, 50]
LIQUIDITY_SCENARIOS = [0.70, 0.50, 0.30]
EXTRA_SLIPPAGE_SCENARIOS = [0.01, 0.02, 0.05]
TAKER_FEE_RATE = 0.07
READ_ONLY_HTTP_TIMEOUT = 8.0
LATENCY_SPIKE_THRESHOLD_MS = 1000.0
NEAR_EXPIRY_BUCKETS = [
    ("T-300~120", 120.0, 300.0, 1500.0),
    ("T-120~60", 60.0, 120.0, 1000.0),
    ("T-60~30", 30.0, 60.0, 750.0),
    ("T-30~0", 0.0, 30.0, 500.0),
]
_TOKEN_RESOLVE_CACHE: dict[str, tuple[str | None, str | None, dict[str, Any] | None]] = {}
READ_ONLY_HEADERS = {
    "User-Agent": "curl/8.0 shadow_execution_recorder SHADOW_ONLY",
    "Accept": "application/json",
}


@dataclass
class ShadowEvent:
    SHADOW_ONLY: bool
    ts_utc: str
    slug: str
    side: str
    direction: str
    distance: float | None
    checkpoint_time: str
    remaining_time_hours: float | None
    hold_duration_hours: float | None
    paper_entry_price: float | None
    paper_exit_price: float | None
    fee_adjusted_paper_pnl: float | None
    clob_token_id: str | None
    clob_orderbook_available: bool
    fallback_used: bool
    clob_fetch_latency_ms: float | None
    best_bid: float | None
    best_ask: float | None
    mid: float | None
    spread: float | None
    bid_levels_count: int | None
    ask_levels_count: int | None
    side_specific_depth_top1: float | None
    side_specific_depth_top3: float | None
    side_specific_depth_top5: float | None
    estimated_fill_price_10: float | None
    estimated_fill_price_25: float | None
    estimated_fill_price_50: float | None
    executable_10: bool | None
    executable_25: bool | None
    executable_50: bool | None
    slippage_bps_10: float | None
    slippage_bps_25: float | None
    slippage_bps_50: float | None
    shadow_adjusted_pnl_10: float | None
    shadow_adjusted_pnl_25: float | None
    shadow_adjusted_pnl_50: float | None
    degradation_vs_paper_10: float | None
    degradation_vs_paper_25: float | None
    degradation_vs_paper_50: float | None
    poly_bid: float | None
    poly_ask: float | None
    poly_mid: float | None
    estimated_taker_executable_price: float | None
    estimated_slippage: float | None
    estimated_spread: float | None
    available_depth: float | None
    fetch_latency_ms: float | None
    simulated_order_size: float
    shadow_adjusted_pnl: float | None
    degradation_vs_paper_pnl: float | None
    executable: bool
    reject_reason: str | None
    remaining_time_sec: float | None = None
    remaining_bucket: str | None = None
    latency_spike_gt_1000ms: bool | None = None


@dataclass
class RawRecord:
    record: WindowRecord
    raw: dict[str, Any]


_WINDOW_FIELDS = {f.name for f in fields(WindowRecord)}


def _http_json(
    url: str,
    params: dict[str, Any] | None,
    timeout: float = READ_ONLY_HTTP_TIMEOUT,
) -> tuple[Any | None, float | None, str | None]:
    encoded_params = urllib.parse.urlencode(params or {})
    full_url = f"{url}?{encoded_params}" if encoded_params else url
    start = time.perf_counter()
    try:
        req = urllib.request.Request(full_url, headers=READ_ONLY_HEADERS, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency_ms = (time.perf_counter() - start) * 1000
            return json.loads(resp.read().decode("utf-8")), latency_ms, None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        return None, latency_ms, str(exc)


def _jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _norm_outcome(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"yes", "up", "true", "1"}:
        return "YES"
    if text in {"no", "down", "false", "0"}:
        return "NO"
    return None


def _set_token_for_outcome(outcome: Any, token_id: Any, yes_no: dict[str, str | None]) -> None:
    side = _norm_outcome(outcome)
    if side == "YES" and token_id:
        yes_no["yes"] = str(token_id)
    elif side == "NO" and token_id:
        yes_no["no"] = str(token_id)


def _parse_clob_token_ids(raw_ids: Any, raw_outcomes: Any = None) -> tuple[str | None, str | None]:
    ids = _jsonish(raw_ids)
    outcomes = _jsonish(raw_outcomes)
    yes_no: dict[str, str | None] = {"yes": None, "no": None}

    if isinstance(ids, dict):
        for key, value in ids.items():
            _set_token_for_outcome(key, value, yes_no)
        return yes_no["yes"], yes_no["no"]

    if not isinstance(ids, list) or not ids or any(isinstance(item, dict) for item in ids):
        return None, None

    if isinstance(outcomes, list) and outcomes:
        for outcome, token_id in zip(outcomes, ids):
            _set_token_for_outcome(outcome, token_id, yes_no)
        if yes_no["yes"] or yes_no["no"]:
            return yes_no["yes"], yes_no["no"]

    yes = str(ids[0]) if len(ids) >= 1 and ids[0] else None
    no = str(ids[1]) if len(ids) >= 2 and ids[1] else None
    return yes, no


def _tokens_from_market(market: dict[str, Any]) -> tuple[str | None, str | None]:
    outcomes = market.get("outcomes")
    for ids_key in ("clobTokenIds", "tokenIds", "clob_token_ids", "clobTokenIDs"):
        yes, no = _parse_clob_token_ids(market.get(ids_key), outcomes)
        if yes or no:
            return yes, no

    token_map = _jsonish(market.get("tokens"))
    yes, no = _parse_clob_token_ids(token_map, outcomes)
    if yes or no:
        return yes, no

    yes_token = None
    no_token = None
    if not isinstance(token_map, list):
        token_map = []
    for token in token_map:
        if not isinstance(token, dict):
            continue
        outcome = token.get("outcome") or token.get("name") or token.get("label")
        token_id = (
            token.get("token_id")
            or token.get("tokenId")
            or token.get("asset_id")
            or token.get("assetId")
            or token.get("id")
        )
        side = _norm_outcome(outcome)
        if side == "YES" and token_id:
            yes_token = str(token_id)
        elif side == "NO" and token_id:
            no_token = str(token_id)
    return yes_token, no_token


def _find_tokens_in_payload(data: Any, slug: str) -> tuple[str | None, str | None, dict[str, Any] | None]:
    candidates: list[dict[str, Any]] = []
    if isinstance(data, list):
        candidates.extend(item for item in data if isinstance(item, dict))
    elif isinstance(data, dict):
        candidates.append(data)
        for key in ("markets", "events"):
            nested = data.get(key)
            if isinstance(nested, list):
                candidates.extend(item for item in nested if isinstance(item, dict))
        for market in data.get("markets") or []:
            if isinstance(market, dict):
                candidates.append(market)

    expanded: list[dict[str, Any]] = []
    for candidate in candidates:
        expanded.append(candidate)
        nested_markets = candidate.get("markets")
        if isinstance(nested_markets, list):
            expanded.extend(item for item in nested_markets if isinstance(item, dict))

    for candidate in expanded:
        if candidate.get("slug") and candidate.get("slug") != slug:
            continue
        yes, no = _tokens_from_market(candidate)
        if yes or no:
            return yes, no, candidate
    return None, None, None


def resolve_clob_token_ids(
    slug: str,
    gamma_base: str = GAMMA_BASE,
    http_getter: Any | None = None,
    retries: int = 2,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Resolve Polymarket YES/NO CLOB token ids for a market slug using read-only GETs."""
    if not slug:
        return None, None, None
    if slug in _TOKEN_RESOLVE_CACHE:
        return _TOKEN_RESOLVE_CACHE[slug]

    getter = http_getter or _http_json
    raw_response = None
    endpoints = [
        (f"{gamma_base.rstrip('/')}/events/slug/{urllib.parse.quote(slug)}", {}),
        (f"{gamma_base.rstrip('/')}/events", {"slug": slug}),
        (f"{gamma_base.rstrip('/')}/markets", {"slug": slug, "closed": "true"}),
        (f"{gamma_base.rstrip('/')}/markets", {"slug": slug}),
    ]
    for _ in range(max(1, retries)):
        for url, params in endpoints:
            data, _, err = getter(url, params)
            if err or data is None:
                continue
            raw_response = data
            yes, no, raw_market = _find_tokens_in_payload(data, slug)
            if yes or no:
                result = (yes, no, raw_market or (data if isinstance(data, dict) else {"raw": data}))
                _TOKEN_RESOLVE_CACHE[slug] = result
                return result

    return (
        None,
        None,
        raw_response if isinstance(raw_response, dict) else {"raw": raw_response} if raw_response is not None else None,
    )


def _load_records(path: Path = SIGNALS_PATH) -> list[WindowRecord]:
    return [r.record for r in _load_raw_records(path)]


def _load_raw_records(path: Path = SIGNALS_PATH) -> list[RawRecord]:
    if not path.exists():
        return []
    records: list[RawRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            records.append(RawRecord(record=_window_record_from_raw(raw), raw=raw))
        except (TypeError, json.JSONDecodeError, ValueError):
            continue
    return records


def _window_record_from_raw(raw: dict[str, Any]) -> WindowRecord:
    cleaned = {k: v for k, v in raw.items() if k in _WINDOW_FIELDS}
    return WindowRecord.from_dict(cleaned)


def _extract_token_id(raw: dict[str, Any], cp_index: int, direction: str) -> str | None:
    checkpoints = raw.get("checkpoints") if isinstance(raw.get("checkpoints"), list) else []
    cp_raw = checkpoints[cp_index] if cp_index < len(checkpoints) and isinstance(checkpoints[cp_index], dict) else {}
    for source in (cp_raw, raw):
        for key in ("clob_token_id", "token_id", "asset_id"):
            val = source.get(key)
            if val:
                return str(val)
        yes, no = _tokens_from_market(source)
        if direction == "DOWN" and no:
            return no
        if direction == "UP" and yes:
            return yes
    return None


def _checkpoint_epoch(r: WindowRecord, cp: Checkpoint) -> int | None:
    if cp.ts_utc:
        try:
            return int(datetime.fromisoformat(cp.ts_utc.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    if r.event_start_ts and cp.offset_s is not None:
        return r.event_start_ts + cp.offset_s
    return None


def _hours_between(start_ts: int | None, end_ts: int | None) -> float | None:
    if start_ts is None or end_ts is None or end_ts < start_ts:
        return None
    return (end_ts - start_ts) / 3600


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _paper_entry_price(cp: Checkpoint) -> float | None:
    if cp.direction == "UP":
        return cp.poly_ask if cp.poly_ask is not None else 0.50
    if cp.direction == "DOWN":
        return (1.0 - cp.poly_bid) if cp.poly_bid is not None else 0.50
    return None


def _paper_exit_price(r: WindowRecord, cp: Checkpoint) -> float | None:
    if not r.resolved or r.outcome is None:
        return None
    return 1.0 if cp.direction == r.outcome else 0.0


def _side_prices(cp: Checkpoint) -> tuple[float | None, float | None, float | None, float | None]:
    if cp.poly_bid is None or cp.poly_ask is None:
        return None, None, None, None
    if cp.direction == "UP":
        bid = cp.poly_bid
        ask = cp.poly_ask
    elif cp.direction == "DOWN":
        bid = 1.0 - cp.poly_ask
        ask = 1.0 - cp.poly_bid
    else:
        return None, None, None, None
    mid = (bid + ask) / 2
    spread = ask - bid
    return bid, ask, mid, spread


def _fee_adjusted_pnl(entry_price: float, payout: float) -> float:
    fee = TAKER_FEE_RATE * (1.0 - entry_price)
    return payout - entry_price - fee


def _level_price_size(level: Any) -> tuple[float, float] | None:
    try:
        if isinstance(level, dict):
            price = float(level.get("price", level.get("p")))
            size = float(level.get("size", level.get("q")))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price = float(level[0])
            size = float(level[1])
        else:
            return None
    except (TypeError, ValueError):
        return None
    if price <= 0 or size <= 0:
        return None
    return price, size


def _parse_levels(book: dict[str, Any] | None, key: str) -> list[tuple[float, float]]:
    if not book:
        return []
    levels = [_level_price_size(level) for level in (book.get(key) or [])]
    parsed = [level for level in levels if level is not None]
    reverse = key == "bids"
    return sorted(parsed, key=lambda x: x[0], reverse=reverse)


def _depth_usdc(levels: list[tuple[float, float]], n_levels: int, liquidity_factor: float = 1.0) -> float | None:
    if not levels:
        return None
    return sum(price * size * liquidity_factor for price, size in levels[:n_levels])


def _fill_price(levels: list[tuple[float, float]], notional_usdc: float, liquidity_factor: float = 1.0) -> float | None:
    if not levels or notional_usdc <= 0:
        return None
    remaining = notional_usdc
    shares = 0.0
    spent = 0.0
    for price, size in levels:
        available_shares = size * liquidity_factor
        available_usdc = price * available_shares
        take_usdc = min(remaining, available_usdc)
        if take_usdc <= 0:
            continue
        shares += take_usdc / price
        spent += take_usdc
        remaining -= take_usdc
        if remaining <= 1e-9:
            break
    if remaining > 1e-9 or shares <= 0:
        return None
    return spent / shares


def _fetch_clob_book(token_id: str, clob_base: str = CLOB_BASE) -> tuple[dict[str, Any] | None, float | None, str | None]:
    url = f"{clob_base.rstrip('/')}/book?{urllib.parse.urlencode({'token_id': token_id})}"
    start = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers=READ_ONLY_HEADERS, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            latency_ms = (time.perf_counter() - start) * 1000
            return json.loads(resp.read().decode("utf-8")), latency_ms, None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        return None, latency_ms, str(exc)


def _fallback_ask_levels(cp: Checkpoint) -> list[tuple[float, float]]:
    _, ask, _, _ = _side_prices(cp)
    if ask is None or cp.poly_liquidity is None or cp.poly_liquidity <= 0:
        return []
    return [(ask, cp.poly_liquidity / ask)]


def _remaining_seconds(remaining_hours: float | None) -> float | None:
    if remaining_hours is None:
        return None
    return max(0.0, remaining_hours * 3600.0)


def _remaining_bucket(remaining_sec: float | None) -> str | None:
    if remaining_sec is None:
        return None
    if 120.0 < remaining_sec <= 300.0:
        return "T-300~120"
    if 60.0 < remaining_sec <= 120.0:
        return "T-120~60"
    if 30.0 < remaining_sec <= 60.0:
        return "T-60~30"
    if 0.0 <= remaining_sec <= 30.0:
        return "T-30~0"
    return "OUT_OF_SCOPE"


def _bucket_latency_threshold(bucket: str | None) -> float:
    for label, _, _, threshold in NEAR_EXPIRY_BUCKETS:
        if label == bucket:
            return threshold
    return LATENCY_SPIKE_THRESHOLD_MS


def _build_event(
    r: WindowRecord,
    cp: Checkpoint,
    notional_usdc: float,
    token_id: str | None = None,
    orderbook: dict[str, Any] | None = None,
    latency_ms: float | None = None,
    fetch_error: str | None = None,
    token_resolve_attempted: bool = False,
) -> ShadowEvent:
    entry_ts = _checkpoint_epoch(r, cp)
    remaining = _hours_between(entry_ts, r.end_ts)
    remaining_sec = _remaining_seconds(remaining)
    remaining_bucket = _remaining_bucket(remaining_sec)
    hold = _hours_between(entry_ts, r.resolved_ts or r.end_ts)
    paper_entry = _paper_entry_price(cp)
    paper_exit = _paper_exit_price(r, cp)
    paper_pnl = r.paper_pnl(cp)
    _, executable_price, old_mid, old_spread = _side_prices(cp)
    bids = _parse_levels(orderbook, "bids")
    asks = _parse_levels(orderbook, "asks")
    clob_available = bool(orderbook and (bids or asks))
    fallback_asks = _fallback_ask_levels(cp)
    fallback_used = not asks
    fill_levels = asks if asks else fallback_asks
    best_bid = bids[0][0] if bids else cp.poly_bid
    best_ask = asks[0][0] if asks else executable_price
    mid = ((best_bid + best_ask) / 2) if best_bid is not None and best_ask is not None else old_mid
    spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else old_spread

    fill_prices = {size: _fill_price(fill_levels, float(size)) for size in ORDER_SIZES}
    executable_by_size = {size: fill_prices[size] is not None for size in ORDER_SIZES}

    def _slippage_bps(fill: float | None) -> float | None:
        if fill is None or mid is None or mid == 0:
            return None
        return (fill - mid) / mid * 10000

    def _shadow_pnl(fill: float | None) -> float | None:
        if fill is None or paper_exit is None:
            return None
        return _fee_adjusted_pnl(fill, paper_exit)

    shadow_by_size = {size: _shadow_pnl(fill_prices[size]) for size in ORDER_SIZES}
    degradation_by_size = {
        size: (paper_pnl - shadow_by_size[size]) if paper_pnl is not None and shadow_by_size[size] is not None else None
        for size in ORDER_SIZES
    }

    reject_reason = None
    if token_id is None and token_resolve_attempted:
        reject_reason = "missing_clob_token_id"
    elif fetch_error:
        reject_reason = "book_fetch_failed"
    elif executable_price is None or mid is None or spread is None:
        reject_reason = "missing_bid_ask"
    elif not executable_by_size.get(int(notional_usdc), False):
        reject_reason = "insufficient_liquidity"

    executable = reject_reason is None
    shadow_pnl = None
    degradation = None
    if executable and paper_exit is not None:
        shadow_pnl = shadow_by_size.get(int(notional_usdc))
        if paper_pnl is not None and shadow_pnl is not None:
            degradation = paper_pnl - shadow_pnl

    return ShadowEvent(
        SHADOW_ONLY=SHADOW_ONLY,
        ts_utc=cp.ts_utc or "",
        slug=r.slug,
        side=cp.direction,
        direction=cp.direction,
        distance=cp.distance,
        checkpoint_time=f"T+{cp.offset_s}",
        remaining_time_hours=remaining,
        hold_duration_hours=hold,
        paper_entry_price=paper_entry,
        paper_exit_price=paper_exit,
        fee_adjusted_paper_pnl=paper_pnl,
        clob_token_id=token_id,
        clob_orderbook_available=clob_available,
        fallback_used=fallback_used,
        clob_fetch_latency_ms=latency_ms,
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread=spread,
        bid_levels_count=len(bids) if clob_available else None,
        ask_levels_count=len(asks) if clob_available else None,
        side_specific_depth_top1=_depth_usdc(fill_levels, 1),
        side_specific_depth_top3=_depth_usdc(fill_levels, 3),
        side_specific_depth_top5=_depth_usdc(fill_levels, 5),
        estimated_fill_price_10=fill_prices[10],
        estimated_fill_price_25=fill_prices[25],
        estimated_fill_price_50=fill_prices[50],
        executable_10=executable_by_size[10],
        executable_25=executable_by_size[25],
        executable_50=executable_by_size[50],
        slippage_bps_10=_slippage_bps(fill_prices[10]),
        slippage_bps_25=_slippage_bps(fill_prices[25]),
        slippage_bps_50=_slippage_bps(fill_prices[50]),
        shadow_adjusted_pnl_10=shadow_by_size[10],
        shadow_adjusted_pnl_25=shadow_by_size[25],
        shadow_adjusted_pnl_50=shadow_by_size[50],
        degradation_vs_paper_10=degradation_by_size[10],
        degradation_vs_paper_25=degradation_by_size[25],
        degradation_vs_paper_50=degradation_by_size[50],
        poly_bid=cp.poly_bid,
        poly_ask=cp.poly_ask,
        poly_mid=old_mid,
        estimated_taker_executable_price=fill_prices.get(int(notional_usdc)) or executable_price,
        estimated_slippage=(fill_prices.get(int(notional_usdc)) - mid) if fill_prices.get(int(notional_usdc)) is not None and mid is not None else None,
        estimated_spread=spread,
        available_depth=cp.poly_liquidity,
        fetch_latency_ms=latency_ms,
        simulated_order_size=notional_usdc,
        shadow_adjusted_pnl=shadow_pnl,
        degradation_vs_paper_pnl=degradation,
        executable=executable,
        reject_reason=reject_reason,
        remaining_time_sec=remaining_sec,
        remaining_bucket=remaining_bucket,
        latency_spike_gt_1000ms=(latency_ms > LATENCY_SPIKE_THRESHOLD_MS) if latency_ms is not None else None,
    )


def build_shadow_events(
    threshold: float = DEFAULT_THRESHOLD,
    notional_usdc: float = DEFAULT_NOTIONAL_USDC,
    signals_path: Path = SIGNALS_PATH,
    orderbook_fetcher: Any | None = None,
    token_resolver: Any | None = None,
    max_online_resolves: int | None = DEFAULT_MAX_ONLINE_RESOLVES,
) -> list[ShadowEvent]:
    events: list[ShadowEvent] = []
    fetcher = orderbook_fetcher or _fetch_clob_book
    resolver = token_resolver or resolve_clob_token_ids
    online_resolves = 0
    for raw_record in _load_raw_records(signals_path):
        r = raw_record.record
        if not r.resolved:
            continue
        for cp_index, cp in enumerate(r.checkpoints):
            if not cp.triggered or cp.error:
                continue
            if cp.distance is not None and cp.distance >= threshold:
                token_id = _extract_token_id(raw_record.raw, cp_index, cp.direction)
                token_resolve_attempted = False
                if token_id is None:
                    token_resolve_attempted = True
                    if max_online_resolves is None or online_resolves < max_online_resolves:
                        online_resolves += 1
                        yes_token, no_token, _ = resolver(r.slug)
                        token_id = no_token if cp.direction == "DOWN" else yes_token
                book = None
                latency_ms = None
                fetch_error = None
                if token_id:
                    book, latency_ms, fetch_error = fetcher(token_id)
                events.append(_build_event(
                    r,
                    cp,
                    notional_usdc,
                    token_id,
                    book,
                    latency_ms,
                    fetch_error,
                    token_resolve_attempted,
                ))
    return events


def _shadow_events_from_raw_record(
    raw: dict[str, Any],
    threshold: float,
    notional_usdc: float,
    orderbook_fetcher: Any | None = None,
    token_resolver: Any | None = None,
) -> list[ShadowEvent]:
    fetcher = orderbook_fetcher or _fetch_clob_book
    resolver = token_resolver or resolve_clob_token_ids
    try:
        r = _window_record_from_raw(raw)
    except (TypeError, ValueError):
        return []
    if not r.resolved:
        return []
    events: list[ShadowEvent] = []
    for cp_index, cp in enumerate(r.checkpoints):
        if not cp.triggered or cp.error or cp.distance is None or cp.distance < threshold:
            continue
        token_id = _extract_token_id(raw, cp_index, cp.direction)
        token_resolve_attempted = False
        if token_id is None:
            token_resolve_attempted = True
            yes_token, no_token, _ = resolver(r.slug)
            token_id = no_token if cp.direction == "DOWN" else yes_token
        book = None
        latency_ms = None
        fetch_error = None
        if token_id:
            book, latency_ms, fetch_error = fetcher(token_id)
        events.append(_build_event(
            r,
            cp,
            notional_usdc,
            token_id,
            book,
            latency_ms,
            fetch_error,
            token_resolve_attempted,
        ))
    return events


def _shadow_event_from_fresh_signal(
    raw: dict[str, Any],
    threshold: float,
    notional_usdc: float,
    orderbook_fetcher: Any | None = None,
    token_resolver: Any | None = None,
) -> ShadowEvent | None:
    if raw.get("event_type") != "signal_started":
        return None
    try:
        distance = float(raw.get("dist"))
    except (TypeError, ValueError):
        return None
    if distance < threshold:
        return None

    direction = str(raw.get("direction") or "").upper()
    if direction not in {"UP", "DOWN"}:
        return None

    slug = str(raw.get("slug") or "")
    if not slug:
        return None

    event_start_ts = int(raw.get("event_start_ts") or 0)
    market_end_ts = int(raw.get("market_end_ts") or 0)
    offset_s = int(raw.get("checkpoint_offset_s") or 0)
    checkpoint_time = str(raw.get("checkpoint") or f"T+{offset_s}")
    ts_utc = str(raw.get("ts") or raw.get("ts_utc") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    poly_bid = _to_float(raw.get("poly_bid"))
    poly_ask = _to_float(raw.get("poly_ask"))

    cp = Checkpoint(
        offset_s=offset_s,
        ts_utc=ts_utc,
        btc_live=_to_float(raw.get("btc_live")) or 0.0,
        distance=distance,
        direction=direction,
        triggered=True,
        poly_bid=poly_bid,
        poly_ask=poly_ask,
        poly_spread=_to_float(raw.get("poly_spread")),
        poly_liquidity=_to_float(raw.get("poly_liquidity")),
        tradeable=True,
    )
    rec = WindowRecord(
        slug=slug,
        event_start_ts=event_start_ts,
        end_ts=market_end_ts,
        anchor_est=_to_float(raw.get("anchor_price")),
        resolved=False,
    )

    resolver = token_resolver or resolve_clob_token_ids
    fetcher = orderbook_fetcher or _fetch_clob_book
    yes_token, no_token, _ = resolver(slug)
    token_id = no_token if direction == "DOWN" else yes_token
    book = None
    latency_ms = None
    fetch_error = None
    if token_id:
        book, latency_ms, fetch_error = fetcher(token_id)

    event = _build_event(
        rec,
        cp,
        notional_usdc,
        token_id,
        book,
        latency_ms,
        fetch_error,
        token_resolve_attempted=True,
    )
    if not token_id:
        event.reject_reason = "token_resolve_failed"
    elif fetch_error and "404" in str(fetch_error):
        event.reject_reason = "fresh_book_404"
    elif fetch_error:
        event.reject_reason = "book_endpoint_failed"
    elif event.clob_orderbook_available:
        event.reject_reason = "fresh_real_book_ok"
    return event


def _events_from_follow_line(
    raw: dict[str, Any],
    threshold: float,
    notional_usdc: float,
    orderbook_fetcher: Any | None = None,
    token_resolver: Any | None = None,
) -> list[ShadowEvent]:
    fresh = _shadow_event_from_fresh_signal(raw, threshold, notional_usdc, orderbook_fetcher, token_resolver)
    if fresh is not None:
        return [fresh]
    if raw.get("resolved") is True:
        return []
    return _shadow_events_from_raw_record(raw, threshold, notional_usdc, orderbook_fetcher, token_resolver)


def _append_shadow_events(path: Path, events: list[ShadowEvent]) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(asdict(event), ensure_ascii=False, sort_keys=True) + "\n")


def _load_shadow_events(path: Path = EVENTS_PATH) -> list[ShadowEvent]:
    if not path.exists():
        return []
    events: list[ShadowEvent] = []
    field_names = set(ShadowEvent.__dataclass_fields__.keys())
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if isinstance(raw, dict):
                events.append(ShadowEvent(**{k: v for k, v in raw.items() if k in field_names}))
        except (TypeError, json.JSONDecodeError, ValueError):
            continue
    return events


def follow_signals(
    threshold: float = DEFAULT_THRESHOLD,
    notional_usdc: float = DEFAULT_NOTIONAL_USDC,
    signals_path: Path = SIGNALS_PATH,
    events_path: Path = EVENTS_PATH,
    report_path: Path = REPORT_PATH,
    poll_interval_sec: float = 2.0,
    max_events: int | None = None,
    since_end: bool = True,
    orderbook_fetcher: Any | None = None,
    token_resolver: Any | None = None,
    ready_callback: Any | None = None,
) -> int:
    """Tail the signal JSONL and append SHADOW_ONLY events for new high-dist candidates."""
    signals_path.parent.mkdir(parents=True, exist_ok=True)
    signals_path.touch(exist_ok=True)
    processed_events = 0
    position = signals_path.stat().st_size if since_end else 0
    if ready_callback is not None:
        ready_callback()
    while True:
        with signals_path.open("r", encoding="utf-8") as f:
            f.seek(position)
            line = f.readline()
            if line:
                position = f.tell()
        if not line:
            if max_events is not None and processed_events >= max_events:
                break
            time.sleep(poll_interval_sec)
            continue
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        events = _events_from_follow_line(
            raw,
            threshold,
            notional_usdc,
            orderbook_fetcher,
            token_resolver,
        )
        _append_shadow_events(events_path, events)
        if events:
            processed_events += len(events)
            all_events = _load_shadow_events(events_path)
            report = generate_report(
                threshold,
                notional_usdc,
                signals_path,
                selected_events=[e for e in all_events if e.distance is not None and e.distance >= threshold],
                all_events=all_events,
                max_online_resolves=0,
            )
            report_path.write_text(report, encoding="utf-8")
        if max_events is not None and processed_events >= max_events:
            break
    return processed_events


def _fmt_num(v: float | None, digits: int = 4, signed: bool = False) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "N/A"
    prefix = "+" if signed else ""
    return f"{v:{prefix}.{digits}f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:.1%}"


def _profit_factor(pnls: list[float]) -> float | None:
    if not pnls:
        return None
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    if gross_loss == 0:
        return float("inf") if gross_win > 0 else None
    return gross_win / gross_loss


def _max_drawdown(pnls: list[float]) -> float | None:
    if not pnls:
        return None
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    idx = min(len(vals) - 1, max(0, math.ceil(len(vals) * q) - 1))
    return vals[idx]


def _event_latency_ms(event: ShadowEvent) -> float | None:
    return event.clob_fetch_latency_ms if event.clob_fetch_latency_ms is not None else event.fetch_latency_ms


def _event_remaining_bucket(event: ShadowEvent) -> str | None:
    if event.remaining_bucket:
        return event.remaining_bucket
    return _remaining_bucket(event.remaining_time_sec if event.remaining_time_sec is not None else _remaining_seconds(event.remaining_time_hours))


def _event_ts_epoch(event: ShadowEvent) -> float | None:
    if not event.ts_utc:
        return None
    try:
        return datetime.fromisoformat(event.ts_utc.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _longest_latency_spike_duration(events: list[ShadowEvent], threshold_ms: float = LATENCY_SPIKE_THRESHOLD_MS) -> float | None:
    spike_times = []
    for event in sorted(events, key=lambda e: _event_ts_epoch(e) or 0):
        latency = _event_latency_ms(event)
        ts = _event_ts_epoch(event)
        if latency is not None and latency > threshold_ms and ts is not None:
            spike_times.append(ts)
    if not spike_times:
        return 0.0

    longest = 0.0
    start = prev = spike_times[0]
    for ts in spike_times[1:]:
        if ts - prev <= 5.0:
            prev = ts
            continue
        longest = max(longest, prev - start)
        start = prev = ts
    return max(longest, prev - start)


def _latency_tail(events: list[ShadowEvent], threshold_ms: float = LATENCY_SPIKE_THRESHOLD_MS) -> dict[str, float | int | None]:
    latencies = [v for e in events for v in [_event_latency_ms(e)] if v is not None]
    return {
        "latency_p50_ms": _percentile(latencies, 0.50),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "latency_p99_ms": _percentile(latencies, 0.99),
        "latency_max_ms": max(latencies) if latencies else None,
        "spikes_gt_1000ms_count": sum(1 for v in latencies if v > threshold_ms),
        "longest_spike_duration_sec": _longest_latency_spike_duration(events, threshold_ms) if latencies else None,
    }


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


def _bucket_status(
    events: list[ShadowEvent],
    summary: dict[str, float | int | None],
    spread_p95: float | None,
    latency_p99: float | None,
    longest_spike_duration: float | None,
    latency_threshold: float,
) -> str:
    if not events:
        return "N/A"
    n = len(events)
    clob = summary["real_clob_book_available_rate"]
    fallback = summary["fallback_used_rate"]
    exec10 = summary["executable_10"]
    exec25 = summary["executable_25"]
    exec50 = summary["executable_50"]
    if (
        (isinstance(clob, float) and clob < 0.90)
        or (isinstance(fallback, float) and fallback > 0.10)
        or (isinstance(exec10, float) and exec10 < 0.90)
        or (isinstance(exec25, float) and exec25 < 0.80)
        or (isinstance(exec50, float) and exec50 < 0.70)
        or (spread_p95 is not None and spread_p95 > 0.05)
        or (latency_p99 is not None and latency_p99 > 1.5 * latency_threshold)
        or (longest_spike_duration is not None and longest_spike_duration > 10.0)
    ):
        return "NO-TRADE"
    if (
        n < 50
        or (isinstance(clob, float) and clob < 0.95)
        or (isinstance(fallback, float) and fallback > 0.05)
        or (isinstance(exec10, float) and exec10 < 0.95)
        or (isinstance(exec25, float) and exec25 < 0.90)
        or (isinstance(exec50, float) and exec50 < 0.80)
        or (spread_p95 is not None and spread_p95 > 0.03)
        or (latency_p99 is not None and latency_p99 > latency_threshold)
    ):
        return "WARNING"
    return "HEALTHY"


def _near_expiry_bucket_rows(events: list[ShadowEvent]) -> list[dict[str, Any]]:
    rows = []
    for label, _, _, latency_threshold in NEAR_EXPIRY_BUCKETS:
        bucket_events = [e for e in events if _event_remaining_bucket(e) == label]
        summary = _summary(bucket_events)
        spreads = [e.spread for e in bucket_events if e.spread is not None]
        depths = [e.side_specific_depth_top5 for e in bucket_events if e.side_specific_depth_top5 is not None]
        latency = _latency_tail(bucket_events)
        spread_p95 = _percentile(spreads, 0.95)
        latency_p99 = latency["latency_p99_ms"]
        longest_spike = latency["longest_spike_duration_sec"]
        rows.append(
            {
                "bucket": label,
                "n": len(bucket_events),
                "clob": summary["real_clob_book_available_rate"],
                "fallback": summary["fallback_used_rate"],
                "exec10": summary["executable_10"],
                "exec25": summary["executable_25"],
                "exec50": summary["executable_50"],
                "spread_p50": _median(spreads),
                "spread_p95": spread_p95,
                "depth5_p50": _median(depths),
                "lat_p50": latency["latency_p50_ms"],
                "lat_p95": latency["latency_p95_ms"],
                "lat_p99": latency_p99,
                "lat_max": latency["latency_max_ms"],
                "spikes": latency["spikes_gt_1000ms_count"],
                "longest_spike": longest_spike,
                "status": _bucket_status(bucket_events, summary, spread_p95, latency_p99, longest_spike, latency_threshold),
            }
        )
    return rows


def _summary(events: list[ShadowEvent]) -> dict[str, float | int | None]:
    paper_pnls = [e.fee_adjusted_paper_pnl for e in events if e.fee_adjusted_paper_pnl is not None]
    shadow_pnls = [e.shadow_adjusted_pnl_10 for e in events if e.shadow_adjusted_pnl_10 is not None]
    slippage = [e.slippage_bps_10 for e in events if e.slippage_bps_10 is not None]
    degradation = [e.degradation_vs_paper_10 for e in events if e.degradation_vs_paper_10 is not None]
    return {
        "n": len(events),
        "orderbook_available_rate": sum(1 for e in events if e.clob_orderbook_available) / len(events) if events else None,
        "real_clob_book_available_rate": sum(1 for e in events if e.clob_orderbook_available) / len(events) if events else None,
        "fallback_used_rate": sum(1 for e in events if e.fallback_used) / len(events) if events else None,
        "missing_token_count": sum(1 for e in events if e.reject_reason == "missing_clob_token_id"),
        "book_fetch_failed_count": sum(1 for e in events if e.reject_reason == "book_fetch_failed"),
        "executable_n": sum(1 for e in events if e.executable_10),
        "executable_rate": sum(1 for e in events if e.executable_10) / len(events) if events else None,
        "executable_10": sum(1 for e in events if e.executable_10) / len(events) if events else None,
        "executable_25": sum(1 for e in events if e.executable_25) / len(events) if events else None,
        "executable_50": sum(1 for e in events if e.executable_50) / len(events) if events else None,
        "avg_slippage": mean(slippage) if slippage else None,
        "avg_degradation": mean(degradation) if degradation else None,
        "paper_mean": mean(paper_pnls) if paper_pnls else None,
        "shadow_mean": mean(shadow_pnls) if shadow_pnls else None,
        "shadow_pf": _profit_factor(shadow_pnls),
        "shadow_mdd": _max_drawdown(shadow_pnls),
        "missing_liquidity_depth": sum(
            1 for e in events if e.reject_reason in {"missing_clob_token_id", "book_fetch_failed", "insufficient_liquidity"}
        ),
        "remaining_lt_1h": _share(events, lambda e: e.remaining_time_hours is not None and e.remaining_time_hours < 1),
        "remaining_lt_6h": _share(events, lambda e: e.remaining_time_hours is not None and e.remaining_time_hours < 6),
        "remaining_lt_24h": _share(events, lambda e: e.remaining_time_hours is not None and e.remaining_time_hours < 24),
        "hold_lt_5m": _share(events, lambda e: e.hold_duration_hours is not None and e.hold_duration_hours < (5 / 60)),
        "hold_5m_15m": _share(events, lambda e: e.hold_duration_hours is not None and (5 / 60) <= e.hold_duration_hours < 0.25),
        "hold_15m_1h": _share(events, lambda e: e.hold_duration_hours is not None and 0.25 <= e.hold_duration_hours < 1),
        "hold_gt_1h": _share(events, lambda e: e.hold_duration_hours is not None and e.hold_duration_hours >= 1),
    }


def _pnls_for_liquidity_stress(events: list[ShadowEvent], factor: float, size: int = 10) -> list[float]:
    pnls = []
    for e in events:
        base = e.shadow_adjusted_pnl_10 if size == 10 else e.shadow_adjusted_pnl_25 if size == 25 else e.shadow_adjusted_pnl_50
        depth = e.side_specific_depth_top5
        if base is not None and depth is not None and depth * factor >= size:
            pnls.append(base)
    return pnls


def _pnls_for_extra_slippage(events: list[ShadowEvent], extra: float, size: int = 10) -> list[float]:
    pnls = []
    for e in events:
        base = e.shadow_adjusted_pnl_10 if size == 10 else e.shadow_adjusted_pnl_25 if size == 25 else e.shadow_adjusted_pnl_50
        if base is not None:
            pnls.append(base - extra)
    return pnls


def _scenario_stats(pnls: list[float]) -> tuple[float | None, float | None, float | None]:
    return (
        mean(pnls) if pnls else None,
        _profit_factor(pnls),
        _max_drawdown(pnls),
    )


def _share(events: list[ShadowEvent], pred: Any) -> float | None:
    if not events:
        return None
    known = [e for e in events if e.remaining_time_hours is not None or e.hold_duration_hours is not None]
    denom = len(known) if known else len(events)
    return sum(1 for e in events if pred(e)) / denom if denom else None


def _fmt_ratio(v: float | None) -> str:
    if v is None:
        return "N/A"
    if math.isinf(v):
        return "inf"
    return f"{v:.2f}"


def _degradation_ratio(s: dict[str, float | int | None]) -> float | None:
    paper_mean = s["paper_mean"]
    avg_degradation = s["avg_degradation"]
    if not isinstance(paper_mean, float) or not isinstance(avg_degradation, float) or paper_mean == 0:
        return None
    return avg_degradation / abs(paper_mean)


def _risk_notes(s: dict[str, float | int | None]) -> str:
    notes = []
    shadow_mean = s["shadow_mean"]
    pf = s["shadow_pf"]
    if isinstance(shadow_mean, float) and shadow_mean > 0:
        notes.append("edge remains positive")
    else:
        notes.append("edge not confirmed after shadow adjustment")
    if isinstance(pf, float) and pf > 1.5:
        notes.append("PF > 1.5")
    else:
        notes.append("PF <= 1.5 or N/A")
    if isinstance(s["remaining_lt_6h"], float) and s["remaining_lt_6h"] > 0.70:
        notes.append("WARNING: edge may be near-expiry dominated")
    degradation_ratio = _degradation_ratio(s)
    if degradation_ratio is not None and degradation_ratio > 0.50:
        notes.append("WARNING: live execution may destroy most paper alpha")
    return "; ".join(notes)


def write_outputs(
    threshold: float = DEFAULT_THRESHOLD,
    notional_usdc: float = DEFAULT_NOTIONAL_USDC,
    signals_path: Path = SIGNALS_PATH,
    events_path: Path = EVENTS_PATH,
    report_path: Path = REPORT_PATH,
    orderbook_fetcher: Any | None = None,
    token_resolver: Any | None = None,
    max_online_resolves: int | None = DEFAULT_MAX_ONLINE_RESOLVES,
) -> tuple[list[ShadowEvent], str]:
    base_threshold = min(min(SUMMARY_THRESHOLDS), threshold)
    all_events = build_shadow_events(base_threshold, notional_usdc, signals_path, orderbook_fetcher, token_resolver, max_online_resolves)
    events = [e for e in all_events if e.distance is not None and e.distance >= threshold]
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        "\n".join(json.dumps(asdict(e), ensure_ascii=False, sort_keys=True) for e in events) + ("\n" if events else ""),
        encoding="utf-8",
    )
    report = generate_report(threshold, notional_usdc, signals_path, events, orderbook_fetcher, token_resolver, all_events, max_online_resolves)
    report_path.write_text(report, encoding="utf-8")
    return events, report


def generate_report(
    threshold: float,
    notional_usdc: float,
    signals_path: Path,
    selected_events: list[ShadowEvent] | None = None,
    orderbook_fetcher: Any | None = None,
    token_resolver: Any | None = None,
    all_events: list[ShadowEvent] | None = None,
    max_online_resolves: int | None = DEFAULT_MAX_ONLINE_RESOLVES,
) -> str:
    records = _load_records(signals_path)
    lines: list[str] = []
    a = lines.append
    a("# Shadow Execution Validation Report")
    a("")
    a(f"> SHADOW_ONLY={str(SHADOW_ONLY).lower()}")
    a(f"> Generated: {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
    a(f"> Source: {signals_path}")
    a(f"> Selected event threshold: dist >= {threshold:g}")
    a(f"> Simulated order size: {notional_usdc:g} USDC")
    a(f"> Max online clob token/book resolves: {'unlimited' if max_online_resolves is None else max_online_resolves}")
    a("")

    all_events = all_events if all_events is not None else build_shadow_events(min(min(SUMMARY_THRESHOLDS), threshold), notional_usdc, signals_path, orderbook_fetcher, token_resolver, max_online_resolves)
    selected_events = selected_events if selected_events is not None else [e for e in all_events if e.distance is not None and e.distance >= threshold]
    selected_summary = _summary(selected_events)
    a("## 1. Selected Shadow Candidates")
    a("")
    a("| Metric | Value |")
    a("|--------|-------|")
    a(f"| SHADOW_ONLY | {str(SHADOW_ONLY).lower()} |")
    a(f"| Total shadow candidates | {selected_summary['n']} |")
    a(f"| real_clob_book_available_rate | {_fmt_pct(selected_summary['real_clob_book_available_rate'])} |")
    a(f"| fallback_used_rate | {_fmt_pct(selected_summary['fallback_used_rate'])} |")
    a(f"| missing_token_count | {selected_summary['missing_token_count']} |")
    a(f"| book_fetch_failed_count | {selected_summary['book_fetch_failed_count']} |")
    a(f"| Executable rate | {_fmt_pct(selected_summary['executable_rate'])} |")
    a(f"| Executable 10 USDC | {_fmt_pct(selected_summary['executable_10'])} |")
    a(f"| Executable 25 USDC | {_fmt_pct(selected_summary['executable_25'])} |")
    a(f"| Executable 50 USDC | {_fmt_pct(selected_summary['executable_50'])} |")
    a(f"| Avg estimated slippage | {_fmt_num(selected_summary['avg_slippage'])} |")
    a(f"| Avg degradation vs paper | {_fmt_num(selected_summary['avg_degradation'], signed=True)} |")
    a(f"| Paper mean PnL | {_fmt_num(selected_summary['paper_mean'], signed=True)} |")
    a(f"| Shadow-adjusted mean PnL | {_fmt_num(selected_summary['shadow_mean'], signed=True)} |")
    a(f"| Shadow PF | {_fmt_ratio(selected_summary['shadow_pf'])} |")
    a(f"| Shadow max drawdown | {_fmt_num(selected_summary['shadow_mdd'])} |")
    a(f"| Rejected due to missing liquidity/depth | {selected_summary['missing_liquidity_depth']} |")
    a("")
    if isinstance(selected_summary["real_clob_book_available_rate"], float) and selected_summary["real_clob_book_available_rate"] < 0.80:
        a("WARNING: real CLOB execution feasibility is not yet validated")
        a("")

    latency_tail = _latency_tail(selected_events)
    a("## 2. Execution Realism Latency Tail")
    a("")
    a("| Metric | Value |")
    a("|--------|-------|")
    a(f"| latency_p50_ms | {_fmt_num(latency_tail['latency_p50_ms'], 1)} |")
    a(f"| latency_p95_ms | {_fmt_num(latency_tail['latency_p95_ms'], 1)} |")
    a(f"| latency_p99_ms | {_fmt_num(latency_tail['latency_p99_ms'], 1)} |")
    a(f"| latency_max_ms | {_fmt_num(latency_tail['latency_max_ms'], 1)} |")
    a(f"| spikes_gt_1000ms_count | {latency_tail['spikes_gt_1000ms_count']} |")
    a(f"| longest_spike_duration_sec | {_fmt_num(latency_tail['longest_spike_duration_sec'], 1)} |")
    a("")

    a("## 3. Near-expiry Bucket Aggregation")
    a("")
    a("| Bucket | N | CLOB avail | Fallback | Exec 10 | Exec 25 | Exec 50 | Spread p50 | Spread p95 | Depth5 p50 | Lat p50 | Lat p95 | Lat p99 | Lat max | Spikes >1s | Longest spike s | Status |")
    a("|--------|---|------------|----------|---------|---------|---------|------------|------------|------------|---------|---------|---------|---------|------------|-----------------|--------|")
    for row in _near_expiry_bucket_rows(selected_events):
        a(
            f"| {row['bucket']} | {row['n']} | {_fmt_pct(row['clob'])} | {_fmt_pct(row['fallback'])} |"
            f" {_fmt_pct(row['exec10'])} | {_fmt_pct(row['exec25'])} | {_fmt_pct(row['exec50'])} |"
            f" {_fmt_num(row['spread_p50'], 4)} | {_fmt_num(row['spread_p95'], 4)} | {_fmt_num(row['depth5_p50'], 1)} |"
            f" {_fmt_num(row['lat_p50'], 1)} | {_fmt_num(row['lat_p95'], 1)} | {_fmt_num(row['lat_p99'], 1)} |"
            f" {_fmt_num(row['lat_max'], 1)} | {row['spikes']} | {_fmt_num(row['longest_spike'], 1)} | {row['status']} |"
        )
    a("")
    a("Bucket status is observability-only. NO-TRADE means not suitable for future live consideration; it does not trigger runtime behavior.")
    a("")

    a("## 4. Threshold Summary")
    a("")
    a("| Threshold | N | real_clob_book_available_rate | fallback_used_rate | missing_token_count | book_fetch_failed_count | Exec 10 | Exec 25 | Exec 50 | Avg Slippage bps | Avg Degradation | Paper Mean | Shadow Mean | Shadow PF | Shadow MDD |")
    a("|-----------|---|-------------------------------|--------------------|---------------------|-------------------------|---------|---------|---------|------------------|-----------------|------------|-------------|-----------|------------|")
    threshold_summaries: dict[int, dict[str, float | int | None]] = {}
    threshold_events: dict[int, list[ShadowEvent]] = {}
    for t in SUMMARY_THRESHOLDS:
        events = [e for e in all_events if e.distance is not None and e.distance >= t]
        threshold_events[t] = events
        s = _summary(events)
        threshold_summaries[t] = s
        a(
            f"| >= {t} | {s['n']} | {_fmt_pct(s['real_clob_book_available_rate'])} |"
            f" {_fmt_pct(s['fallback_used_rate'])} | {s['missing_token_count']} | {s['book_fetch_failed_count']} |"
            f" {_fmt_pct(s['executable_10'])} | {_fmt_pct(s['executable_25'])} | {_fmt_pct(s['executable_50'])} |"
            f" {_fmt_num(s['avg_slippage'])} |"
            f" {_fmt_num(s['avg_degradation'], signed=True)} | {_fmt_num(s['paper_mean'], signed=True)} |"
            f" {_fmt_num(s['shadow_mean'], signed=True)} | {_fmt_ratio(s['shadow_pf'])} |"
            f" {_fmt_num(s['shadow_mdd'])} |"
        )
    a("")

    a("## 5. Worst-case Execution Scenario")
    a("")
    a("Liquidity scenarios are conservative stress tests. Extra slippage subtracts 0.01 / 0.02 / 0.05 PnL from each executable shadow trade. Max DD uses the running cumulative fee-adjusted PnL curve.")
    a("")
    a("| Threshold | Scenario | Mean PnL | PF | MDD |")
    a("|-----------|----------|----------|----|-----|")
    scenario_stats: dict[tuple[int, str], tuple[float | None, float | None, float | None]] = {}
    for t, events in threshold_events.items():
        base_pnls = [e.shadow_adjusted_pnl_10 for e in events if e.shadow_adjusted_pnl_10 is not None]
        scenarios: list[tuple[str, list[float]]] = [("base", base_pnls)]
        scenarios.extend((f"liquidity_{int(factor * 100)}pct", _pnls_for_liquidity_stress(events, factor)) for factor in LIQUIDITY_SCENARIOS)
        scenarios.extend((f"extra_slippage_{int(extra * 100)}c", _pnls_for_extra_slippage(events, extra)) for extra in EXTRA_SLIPPAGE_SCENARIOS)
        for label, pnls in scenarios:
            stats = _scenario_stats(pnls)
            scenario_stats[(t, label)] = stats
            a(f"| >= {t} | {label} | {_fmt_num(stats[0], signed=True)} | {_fmt_ratio(stats[1])} | {_fmt_num(stats[2])} |")
    a("")

    a("## 6. Edge Survival Analysis")
    a("")
    a("| Threshold | Edge Positive After Slippage? | PF > 1.5? | MDD | Degradation/Paper Mean | Notes |")
    a("|-----------|-------------------------------|----------|-----|------------------------|-------|")
    best_threshold = None
    best_score = -math.inf
    for t, s in threshold_summaries.items():
        shadow_mean = s["shadow_mean"]
        pf = s["shadow_pf"]
        five_c_stats = scenario_stats.get((t, "extra_slippage_5c"), (None, None, None))
        degradation_ratio = _degradation_ratio(s)
        score = (shadow_mean if isinstance(shadow_mean, float) else -999) + (pf if isinstance(pf, float) and not math.isinf(pf) else 10)
        if score > best_score:
            best_score = score
            best_threshold = t
        a(
            f"| >= {t} | {'YES' if isinstance(shadow_mean, float) and shadow_mean > 0 else 'NO'} |"
            f" {'YES' if isinstance(pf, float) and pf > 1.5 else 'NO'} | {_fmt_num(s['shadow_mdd'])} |"
            f" {_fmt_pct(degradation_ratio)} | {_risk_notes(s)}; extra_slippage_5c PF {'>' if isinstance(five_c_stats[1], float) and five_c_stats[1] > 1.5 else '<='} 1.5 |"
        )
    a("")
    a(f"Most stable shadow threshold by this simple score: {best_threshold if best_threshold is not None else 'N/A'}")
    a("")

    a("## 7. Near-expiry Distribution")
    a("")
    a("| Threshold | Remaining <1h | Remaining <6h | Remaining <24h | Risk |")
    a("|-----------|---------------|---------------|----------------|------|")
    for t, s in threshold_summaries.items():
        risk = []
        if isinstance(s["remaining_lt_6h"], float) and s["remaining_lt_6h"] > 0.70:
            risk.append("WARNING: edge may be near-expiry dominated")
        degradation_ratio = _degradation_ratio(s)
        if degradation_ratio is not None and degradation_ratio > 0.50:
            risk.append("WARNING: live execution may destroy most paper alpha")
        five_c_pf = scenario_stats.get((t, "extra_slippage_5c"), (None, None, None))[1]
        if not (isinstance(five_c_pf, float) and five_c_pf > 1.5):
            risk.append("WARNING: edge fragile under realistic slippage")
        a(
            f"| >= {t} | {_fmt_pct(s['remaining_lt_1h'])} | {_fmt_pct(s['remaining_lt_6h'])} |"
            f" {_fmt_pct(s['remaining_lt_24h'])} | {'; '.join(risk) if risk else 'OK'} |"
        )
    a("")

    a("## 8. Hold Duration Distribution")
    a("")
    a("| Threshold | Hold <5m | Hold 5m-15m | Hold 15m-1h | Hold >1h |")
    a("|-----------|----------|-------------|-------------|----------|")
    for t, s in threshold_summaries.items():
        a(
            f"| >= {t} | {_fmt_pct(s['hold_lt_5m'])} | {_fmt_pct(s['hold_5m_15m'])} |"
            f" {_fmt_pct(s['hold_15m_1h'])} | {_fmt_pct(s['hold_gt_1h'])} |"
        )
    a("")

    a("## 9. Missing Field Notes")
    a("")
    a("- SHADOW_ONLY=true for every event.")
    a("- No trading API calls are made; analysis uses local JSONL only.")
    a("- The recorder resolves missing clobTokenIds from the market slug using read-only Gamma GET calls.")
    a("- When token resolution succeeds, this module uses the read-only Polymarket CLOB /book endpoint and records clob_orderbook_available=true/false.")
    a("- If token or book lookup fails, fallback_used=true and the event falls back to the checkpoint bid/ask/liquidity estimate.")
    a("- Missing or insufficient fields are reported as N/A; the recorder does not invent book levels.")
    a("- Future minimal fields for stronger validation: market clobTokenIds, order-book levels at signal time, side-specific depth for target notional, quote fetch latency ms, and executable snapshot timestamp.")
    a("")

    if selected_events:
        a("## 10. Example Shadow Event")
        a("")
        a("```json")
        a(json.dumps(asdict(selected_events[0]), ensure_ascii=False, indent=2, sort_keys=True))
        a("```")
        a("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, choices=[100, 110, 120, 130, 150])
    parser.add_argument("--notional-usdc", type=float, default=DEFAULT_NOTIONAL_USDC)
    parser.add_argument("--signals-path", type=Path, default=SIGNALS_PATH)
    parser.add_argument("--events-path", type=Path, default=EVENTS_PATH)
    parser.add_argument("--report-path", type=Path, default=REPORT_PATH)
    parser.add_argument("--follow", action="store_true", help="Tail signals JSONL and process only new high-dist candidates")
    parser.add_argument("--poll-interval-sec", type=float, default=2.0)
    parser.add_argument("--max-events", type=int, default=None)
    parser.add_argument("--since-end", dest="since_end", action="store_true", default=True,
                        help="Start follow mode at EOF; this is the default")
    parser.add_argument("--from-start", dest="since_end", action="store_false",
                        help="Follow mode starts at beginning of file")
    parser.add_argument("--max-online-resolves", type=int, default=DEFAULT_MAX_ONLINE_RESOLVES,
                        help="Max missing-token candidates to resolve/fetch online; use -1 for unlimited")
    args = parser.parse_args()
    max_online_resolves = None if args.max_online_resolves < 0 else args.max_online_resolves

    if args.follow:
        follow_signals_path = FRESH_SIGNAL_EVENTS_PATH if args.signals_path == SIGNALS_PATH else args.signals_path
        processed = follow_signals(
            threshold=args.threshold,
            notional_usdc=args.notional_usdc,
            signals_path=follow_signals_path,
            events_path=args.events_path,
            report_path=args.report_path,
            poll_interval_sec=args.poll_interval_sec,
            max_events=args.max_events,
            since_end=args.since_end,
        )
        print(f"[shadow-follow] SHADOW_ONLY={str(SHADOW_ONLY).lower()}")
        print(f"[shadow-follow] processed_events={processed} threshold={args.threshold:g}")
        print(f"[shadow-follow] events={args.events_path}")
        print(f"[shadow-follow] report={args.report_path}")
        return

    events, _ = write_outputs(
        threshold=args.threshold,
        notional_usdc=args.notional_usdc,
        signals_path=args.signals_path,
        events_path=args.events_path,
        report_path=args.report_path,
        orderbook_fetcher=None,
        token_resolver=None,
        max_online_resolves=max_online_resolves,
    )
    print(f"[shadow] SHADOW_ONLY={str(SHADOW_ONLY).lower()}")
    print(f"[shadow] candidates={len(events)} threshold={args.threshold:g} notional={args.notional_usdc:g}")
    print(f"[shadow] events={args.events_path}")
    print(f"[shadow] report={args.report_path}")


if __name__ == "__main__":
    main()
