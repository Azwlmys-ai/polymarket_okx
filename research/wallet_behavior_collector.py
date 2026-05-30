"""
wallet_behavior_collector.py - read-only wallet trade/market collector.

Only performs HTTP GET requests against public Polymarket APIs. It does not
trade, does not authenticate, and does not integrate with strategy code.

Usage:
    python3 research/wallet_behavior_collector.py

Outputs:
    research/wallet_0xe022_trades_raw.jsonl
    research/wallet_0xe022_markets_raw.jsonl
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_USER = "0xe0229e10a858860218b6132f4234602c47bd6603"
SHORT_USER = "0xe022"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
DEFAULT_TRADES_OUT = Path(f"research/wallet_{SHORT_USER}_trades_raw.jsonl")
DEFAULT_MARKETS_OUT = Path(f"research/wallet_{SHORT_USER}_markets_raw.jsonl")
HTTP_TIMEOUT = 20
USER_AGENT = "polymarket-okx-wallet-behavior-collector/0.1"

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE


@dataclass
class ApiResult:
    url: str
    ok: bool
    http_code: int | None
    data: Any
    error_type: str | None
    error_message: str | None
    latency_ms: float


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def http_get_json(url: str) -> ApiResult:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=SSL_CONTEXT) as resp:
            body = resp.read()
            text = body.decode("utf-8", errors="replace")
            return ApiResult(
                url=url,
                ok=True,
                http_code=resp.status,
                data=json.loads(text),
                error_type=None,
                error_message=None,
                latency_ms=round((time.perf_counter() - start) * 1000, 1),
            )
    except urllib.error.HTTPError as exc:
        msg = exc.read().decode("utf-8", errors="replace")[:1000]
        return ApiResult(url, False, exc.code, None, "HTTPError", msg, round((time.perf_counter() - start) * 1000, 1))
    except urllib.error.URLError as exc:
        return ApiResult(url, False, None, None, "URLError", str(exc.reason), round((time.perf_counter() - start) * 1000, 1))
    except json.JSONDecodeError as exc:
        return ApiResult(url, False, None, None, "JSONDecodeError", str(exc), round((time.perf_counter() - start) * 1000, 1))
    except Exception as exc:
        return ApiResult(url, False, None, None, type(exc).__name__, str(exc), round((time.perf_counter() - start) * 1000, 1))


def list_payload(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("data", "trades", "markets"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(rows)


def market_text(row: dict[str, Any]) -> str:
    keys = (
        "title",
        "slug",
        "eventSlug",
        "conditionId",
        "icon",
        "question",
        "description",
        "seriesSlug",
    )
    return " ".join(str(row.get(k, "")) for k in keys).lower()


def is_btc_5m_candidate(row: dict[str, Any]) -> bool:
    text = market_text(row)
    has_btc = "btc" in text or "bitcoin" in text
    has_5m = "5m" in text
    has_updown = "updown" in text or "up or down" in text
    return has_btc and has_5m and has_updown


def dedupe_trades(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (
            str(row.get("transactionHash") or ""),
            str(row.get("asset") or row.get("tokenId") or ""),
            str(row.get("timestamp") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def trades_url(user: str, limit: int, offset: int) -> str:
    query = urllib.parse.urlencode({"user": user, "limit": limit, "offset": offset})
    return f"{DATA_API}/trades?{query}"


def gamma_condition_url(condition_id: str) -> str:
    query = urllib.parse.urlencode({"condition_ids": condition_id, "limit": 1})
    return f"{GAMMA_API}/markets?{query}"


def gamma_market_slug_url(slug: str) -> str:
    query = urllib.parse.urlencode({"slug": slug, "limit": 1})
    return f"{GAMMA_API}/markets?{query}"


def gamma_events_slug_query_url(slug: str) -> str:
    query = urllib.parse.urlencode({"slug": slug, "limit": 1})
    return f"{GAMMA_API}/events?{query}"


def gamma_event_slug_url(slug: str) -> str:
    return f"{GAMMA_API}/events/slug/{urllib.parse.quote(slug)}"


def trade_slug(trades: list[dict[str, Any]]) -> str:
    for trade in trades:
        for key in ("marketSlug", "slug", "eventSlug"):
            value = str(trade.get(key) or "")
            if value:
                return value
    return ""


def extract_event_market(event: dict[str, Any], condition_id: str) -> dict[str, Any] | None:
    markets = event.get("markets") or []
    if not isinstance(markets, list):
        return None
    exact = next((row for row in markets if isinstance(row, dict) and str(row.get("conditionId")) == condition_id), None)
    if not exact:
        return None
    merged = dict(exact)
    merged["event"] = {
        key: event.get(key)
        for key in (
            "id",
            "ticker",
            "slug",
            "title",
            "endDate",
            "closedTime",
            "startTime",
            "seriesSlug",
            "eventMetadata",
        )
        if key in event
    }
    if not merged.get("endDate") and event.get("endDate"):
        merged["endDate"] = event.get("endDate")
    if not merged.get("closedTime") and event.get("closedTime"):
        merged["closedTime"] = event.get("closedTime")
    if not merged.get("eventStartTime") and event.get("startTime"):
        merged["eventStartTime"] = event.get("startTime")
    return merged


def normalize_confirmed_market(
    market: dict[str, Any],
    source: str,
    attempts: list[dict[str, Any]],
    condition_id: str,
    trades: list[dict[str, Any]],
) -> dict[str, Any]:
    out = dict(market)
    out["conditionId"] = out.get("conditionId") or condition_id
    out["metadata_missing"] = False
    out["metadata_confirmed"] = True
    out["metadata_source"] = source
    out["metadata_resolution_attempts"] = attempts
    out["source"] = source
    if not out.get("slug"):
        out["slug"] = trade_slug(trades)
    if not out.get("question"):
        out["question"] = out.get("title") or (trades[0].get("title") if trades else None)
    return out


def fallback_market(
    condition_id: str,
    trades: list[dict[str, Any]],
    reason: str,
    attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    first = trades[0] if trades else {}
    return {
        "conditionId": condition_id,
        "metadata_missing": True,
        "metadata_confirmed": False,
        "metadata_missing_reason": reason,
        "metadata_resolution_attempts": attempts or [],
        "question": first.get("title"),
        "title": first.get("title"),
        "slug": first.get("marketSlug") or first.get("slug") or first.get("eventSlug"),
        "eventSlug": first.get("eventSlug"),
        "icon": first.get("icon"),
        "source": "trade_fallback",
    }


def result_attempt(name: str, result: ApiResult, count: int, exact: bool) -> dict[str, Any]:
    item = {
        "name": name,
        "url": result.url,
        "ok": result.ok,
        "http_code": result.http_code,
        "count": count,
        "exact": exact,
        "latency_ms": result.latency_ms,
    }
    if not result.ok:
        item["error_type"] = result.error_type
        item["error_message"] = result.error_message
    return item


def find_market_metadata(condition_id: str, trades: list[dict[str, Any]]) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []

    result = http_get_json(gamma_condition_url(condition_id))
    rows = list_payload(result.data) if result.ok else []
    exact = next((row for row in rows if str(row.get("conditionId")) == condition_id), None)
    attempts.append(result_attempt("markets_condition_ids", result, len(rows), exact is not None))
    if exact and is_btc_5m_candidate({**trades[0], **exact}):
        return normalize_confirmed_market(exact, "gamma_markets_condition_ids", attempts, condition_id, trades)

    slug = trade_slug(trades)
    if slug:
        result = http_get_json(gamma_market_slug_url(slug))
        rows = list_payload(result.data) if result.ok else []
        exact = next((row for row in rows if str(row.get("conditionId")) == condition_id), None)
        attempts.append(result_attempt("markets_slug", result, len(rows), exact is not None))
        if exact and is_btc_5m_candidate({**trades[0], **exact}):
            return normalize_confirmed_market(exact, "gamma_markets_slug", attempts, condition_id, trades)

        result = http_get_json(gamma_events_slug_query_url(slug))
        events = list_payload(result.data) if result.ok else []
        event_market = None
        for event in events:
            event_market = extract_event_market(event, condition_id)
            if event_market:
                break
        attempts.append(result_attempt("events_slug_query", result, len(events), event_market is not None))
        if event_market and is_btc_5m_candidate({**trades[0], **event_market}):
            return normalize_confirmed_market(event_market, "gamma_events_slug_query", attempts, condition_id, trades)

        result = http_get_json(gamma_event_slug_url(slug))
        event = result.data if isinstance(result.data, dict) else {}
        event_market = extract_event_market(event, condition_id) if result.ok else None
        count = len(event.get("markets") or []) if isinstance(event, dict) else 0
        attempts.append(result_attempt("events_slug_path", result, count, event_market is not None))
        if event_market and is_btc_5m_candidate({**trades[0], **event_market}):
            return normalize_confirmed_market(event_market, "gamma_events_slug_path", attempts, condition_id, trades)

    failed = "; ".join(
        f"{a['name']}: ok={a['ok']} count={a['count']} exact={a['exact']}"
        for a in attempts
    )
    return fallback_market(condition_id, trades, failed or "no conditionId/slug metadata path succeeded", attempts)


def collect_trades(user: str, limit: int, max_pages: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for page in range(max_pages):
        url = trades_url(user, limit, page * limit)
        result = http_get_json(url)
        if not result.ok:
            errors.append({
                "_collector_error": True,
                "stage": "trades",
                "url": result.url,
                "http_code": result.http_code,
                "error_type": result.error_type,
                "error_message": result.error_message,
                "latency_ms": result.latency_ms,
                "ts": utc_now_iso(),
            })
            break
        page_rows = list_payload(result.data)
        all_rows.extend(page_rows)
        if len(page_rows) < limit:
            break
    candidates = [row for row in dedupe_trades(all_rows) if is_btc_5m_candidate(row)]
    return candidates, errors


def collect_markets(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        cid = str(trade.get("conditionId") or "")
        if cid:
            grouped.setdefault(cid, []).append(trade)

    markets: list[dict[str, Any]] = []
    for condition_id, condition_trades in sorted(grouped.items()):
        markets.append(find_market_metadata(condition_id, condition_trades))
    return markets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only wallet behavior collector.")
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--out-trades", type=Path, default=DEFAULT_TRADES_OUT)
    parser.add_argument("--out-markets", type=Path, default=DEFAULT_MARKETS_OUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    trades, errors = collect_trades(args.user, args.limit, args.max_pages)
    trade_rows = errors + trades
    markets = collect_markets(trades) if trades else []

    if errors and not markets:
        markets = [{
            "_collector_error": True,
            "stage": "markets",
            "metadata_missing": True,
            "metadata_missing_reason": "trades collection failed; no conditionId available",
            "ts": utc_now_iso(),
        }]

    n_trades = write_jsonl(args.out_trades, trade_rows)
    n_markets = write_jsonl(args.out_markets, markets)
    print(f"trades_raw: {n_trades} rows -> {args.out_trades}")
    print(f"markets_raw: {n_markets} rows -> {args.out_markets}")
    print("scope: read-only HTTP GET; no trading; no strategy integration")
    return 0


if __name__ == "__main__":
    sys.exit(main())
