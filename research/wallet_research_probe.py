"""
wallet_research_probe.py - read-only Polymarket data-api wallet probe.

Only performs HTTP GET requests. It does not place orders, does not connect to
live trading, and does not integrate with the main runner.

Usage:
    python research/wallet_research_probe.py

Outputs:
    research/wallet_0xe022_trades_sample.jsonl
    research/wallet_0xe022_probe_report.md
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


WALLET = "0xe0229e10a858860218b6132f4234602c47bd6603"
SHORT_WALLET = "0xe022"
BASE_URL = "https://data-api.polymarket.com"
DEFAULT_LIMIT = 10
DEFAULT_OUT = Path(f"research/wallet_{SHORT_WALLET}_trades_sample.jsonl")
DEFAULT_REPORT = Path(f"research/wallet_{SHORT_WALLET}_probe_report.md")
HTTP_TIMEOUT = 20
USER_AGENT = "polymarket-okx-wallet-research-probe/0.1"

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

    @property
    def count(self) -> int | None:
        if isinstance(self.data, list):
            return len(self.data)
        if isinstance(self.data, dict):
            for key in ("data", "trades", "positions"):
                value = self.data.get(key)
                if isinstance(value, list):
                    return len(value)
        return None

    @property
    def non_empty(self) -> bool:
        count = self.count
        return bool(count and count > 0)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_url(path: str, wallet: str, limit: int) -> str:
    query = urllib.parse.urlencode({"user": wallet, "limit": limit})
    return f"{BASE_URL}/{path}?{query}"


def http_get_json(url: str) -> ApiResult:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=SSL_CONTEXT) as resp:
            body = resp.read()
            latency_ms = round((time.perf_counter() - start) * 1000, 1)
            text = body.decode("utf-8", errors="replace")
            return ApiResult(
                url=url,
                ok=True,
                http_code=resp.status,
                data=json.loads(text),
                error_type=None,
                error_message=None,
                latency_ms=latency_ms,
            )
    except urllib.error.HTTPError as exc:
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        message = exc.read().decode("utf-8", errors="replace")[:1000]
        return ApiResult(url, False, exc.code, None, "HTTPError", message, latency_ms)
    except urllib.error.URLError as exc:
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        return ApiResult(url, False, None, None, "URLError", str(exc.reason), latency_ms)
    except json.JSONDecodeError as exc:
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        return ApiResult(url, False, None, None, "JSONDecodeError", str(exc), latency_ms)
    except Exception as exc:  # Defensive: keep probe/report generation alive.
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        return ApiResult(url, False, None, None, type(exc).__name__, str(exc), latency_ms)


def list_payload(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("data", "trades", "positions"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(rows)


def sample_contains_btc_5m(rows: list[dict[str, Any]]) -> bool:
    needles = ("bitcoin up or down", "btc", "5m")
    for row in rows:
        blob = json.dumps(row, ensure_ascii=False).lower()
        if any(needle in blob for needle in needles):
            return True
    return False


def format_result(result: ApiResult | None, skipped_reason: str | None = None) -> str:
    if result is None:
        return f"skipped ({skipped_reason or 'not checked'})"
    if result.ok:
        return f"ok, HTTP {result.http_code}, count={result.count}, latency_ms={result.latency_ms}"
    return (
        f"error, HTTP {result.http_code}, {result.error_type}: "
        f"{result.error_message}, latency_ms={result.latency_ms}"
    )


def write_report(
    path: Path,
    out_path: Path,
    executed_at: str,
    trades: ApiResult,
    positions: ApiResult | None,
    positions_skipped_reason: str | None,
    saved_count: int,
    has_btc_5m: bool,
) -> None:
    data_api_user_ok = trades.non_empty
    suggest_next = trades.non_empty and has_btc_5m
    positions_non_empty = positions.non_empty if positions else None
    next_note = (
        "Yes: trades are non-empty and the sample includes BTC/5m-style market text."
        if suggest_next
        else "No: keep this at probe level until the data-api user mapping and sample relevance are clear."
    )

    lines = [
        "# Wallet 0xe022 Polymarket Data-API Probe",
        "",
        f"- Execution time: `{executed_at}`",
        f"- Wallet: `{WALLET}`",
        f"- Trades endpoint: `{trades.url}`",
        f"- Positions endpoint: `{positions.url if positions else 'not requested'}`",
        "",
        "## Results",
        "",
        f"- Trades verification: {format_result(trades)}",
        f"- Trades non-empty: `{trades.non_empty}`",
        f"- Positions verification: {format_result(positions, positions_skipped_reason)}",
        f"- Positions non-empty: `{positions_non_empty}`",
        f"- Can this address be used directly as Polymarket data-api `user`: `{data_api_user_ok}`",
        f"- Trades saved: `{saved_count}` rows to `{out_path}`",
        f"- Sample contains Bitcoin Up or Down / BTC / 5m text: `{has_btc_5m}`",
        f"- Recommend next collector/enrich/report step: {next_note}",
        "",
        "## Scope Confirmation",
        "",
        "- No orders were placed.",
        "- No live trading connection was made.",
        "- `mvp_runner.py` was not modified.",
        "- Existing VPS systemd services were not modified.",
        "- The running `paper_anchor_sim.py` process/files were not touched by this probe.",
        "- No strategy logic or trading decision logic was added.",
    ]

    if not data_api_user_ok:
        lines.extend([
            "",
            "## Next Mapping Note",
            "",
            "The trades endpoint did not confirm this address as a usable data-api user.",
            "Next step would be to find the corresponding proxy wallet address before enrichment.",
        ])

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only wallet probe for Polymarket data-api.")
    parser.add_argument("--wallet", default=WALLET)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--always-check-positions",
        action="store_true",
        help="Also GET positions when trades are non-empty.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    executed_at = utc_now_iso()

    trades = http_get_json(build_url("trades", args.wallet, args.limit))
    rows = list_payload(trades.data) if trades.ok else []
    saved_count = write_jsonl(args.out, rows)
    has_btc_5m = sample_contains_btc_5m(rows)

    positions: ApiResult | None = None
    positions_skipped_reason: str | None = None
    if args.always_check_positions or not trades.non_empty:
        positions = http_get_json(build_url("positions", args.wallet, args.limit))
    else:
        positions_skipped_reason = "trades endpoint was non-empty"

    write_report(
        args.report,
        args.out,
        executed_at,
        trades,
        positions,
        positions_skipped_reason,
        saved_count,
        has_btc_5m,
    )

    print(f"trades: {format_result(trades)}")
    print(f"positions: {format_result(positions, positions_skipped_reason)}")
    print(f"saved_trades: {saved_count} -> {args.out}")
    print(f"report: {args.report}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
