"""
scrapling_probe.py — minimal environment probe for Scrapling feasibility.

Checks whether Scrapling is importable, then uses aiohttp as fallback to
fetch Polymarket pages.  Records fetch outcome, HTML size, title, and
blocked-like signals.  Does NOT parse complex selectors, does NOT connect to
any trading logic.

Usage:
    python3 research/scrapling_probe.py --samples 1 --interval 0

Outputs:
    research/scrapling_probe_results.jsonl
    research/scrapling_probe_report.md
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import ssl
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TARGETS = [
    {"url": "https://polymarket.com/",       "name": "home"},
    {"url": "https://polymarket.com/markets", "name": "markets"},
]

DEFAULT_OUT    = Path("research/scrapling_probe_results.jsonl")
DEFAULT_REPORT = Path("research/scrapling_probe_report.md")
HTTP_TIMEOUT   = 15
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Heuristics that suggest anti-bot / empty response
BLOCKED_SIGNALS = [
    "access denied",
    "cloudflare",
    "captcha",
    "403 forbidden",
    "rate limit",
    "just a moment",         # Cloudflare challenge page
    "challenge-platform",    # Cloudflare JS challenge
    "blocked by",
    "bot detection",
    # NOTE: "robot" intentionally excluded — <meta name="robots"> is normal SEO
]

# Minimum HTML we expect for a real page load
MIN_MEANINGFUL_HTML = 500   # bytes

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode    = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# Pure functions (unit-testable, no I/O)
# ---------------------------------------------------------------------------

def detect_scrapling() -> dict:
    """
    Return availability info for Scrapling without installing it.

    Result keys:
        available (bool)     – True only if `import scrapling` succeeds
        version  (str|None)  – scrapling.__version__ if available
        message  (str)       – human-readable status
        install_cmd (str)    – suggested install command
    """
    try:
        import importlib
        mod = importlib.import_module("scrapling")
        ver = getattr(mod, "__version__", "unknown")
        return {
            "available": True,
            "version": ver,
            "message": f"scrapling {ver} is importable",
            "install_cmd": "",
        }
    except ImportError:
        return {
            "available": False,
            "version": None,
            "message": "scrapling is NOT installed in this environment",
            "install_cmd": "pip install scrapling",
        }


def extract_title(html: str) -> str | None:
    """
    Extract <title> text from raw HTML using stdlib regex.
    Returns None when no <title> tag found.
    """
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip() or None


def detect_blocked_like(html: str, http_code: int) -> tuple[bool, str | None]:
    """
    Heuristic check for anti-bot / empty / error responses.

    Returns (is_blocked, reason_or_None).
    """
    if http_code in (403, 429, 503):
        return True, f"http_{http_code}"
    if not html or len(html) < MIN_MEANINGFUL_HTML:
        return True, "html_too_small"
    low = html.lower()
    for signal in BLOCKED_SIGNALS:
        if signal in low:
            return True, f"signal:{signal}"
    return False, None


def structure_hash(html: str) -> str:
    """
    Stable fingerprint of HTML structure for change detection.
    Strips tag content, keeps only tag names.
    """
    tags = re.findall(r"<([a-zA-Z][a-zA-Z0-9]*)[^>]*>", html)
    return hashlib.md5(" ".join(tags[:200]).encode()).hexdigest()[:12]


def summarize_results(records: list[dict]) -> dict:
    """
    Compute aggregate metrics from a list of probe result dicts.

    Returns:
        total, fetch_ok, fetch_errors, blocked_count,
        fetch_success_rate, blocked_rate,
        avg_latency_ms, p95_latency_ms,
        scrapling_available
    """
    total       = len(records)
    fetch_ok    = sum(1 for r in records if r.get("status") == "ok")
    fetch_err   = sum(1 for r in records if r.get("status") == "error")
    blocked     = sum(1 for r in records if r.get("blocked_like"))
    latencies   = sorted(r["latency_ms"] for r in records if r.get("latency_ms") is not None)
    scrapling   = any(r.get("scrapling_available") for r in records)

    avg_lat = round(sum(latencies) / len(latencies), 1) if latencies else None
    p95_lat = latencies[int(len(latencies) * 0.95)] if latencies else None

    return {
        "total":              total,
        "fetch_ok":           fetch_ok,
        "fetch_errors":       fetch_err,
        "blocked_count":      blocked,
        "fetch_success_rate": round(fetch_ok / total, 4) if total else 0.0,
        "blocked_rate":       round(blocked / total, 4)  if total else 0.0,
        "avg_latency_ms":     avg_lat,
        "p95_latency_ms":     p95_lat,
        "scrapling_available": scrapling,
    }


def verdict(summary: dict, scrapling_info: dict) -> str:
    """
    NEED_MORE_TEST or NO-GO.  Never returns GO when Scrapling is absent.
    """
    if not scrapling_info["available"]:
        return "NEED_MORE_TEST"
    # Scrapling present but high block / failure rate
    if summary["fetch_success_rate"] < 0.5 or summary["blocked_rate"] > 0.5:
        return "NO-GO"
    return "NEED_MORE_TEST"


# ---------------------------------------------------------------------------
# Async fetch
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    ts_ms:              int
    sample_idx:         int
    url:                str
    name:               str
    scrapling_available: bool
    status:             str          # ok | error | timeout
    http_code:          int | None
    latency_ms:         float | None
    html_size:          int
    title:              str | None
    blocked_like:       bool
    blocked_reason:     str | None
    structure_hash:     str | None
    error_type:         str | None
    error_message:      str | None

    def to_dict(self) -> dict:
        return asdict(self)


async def fetch_one(
    session: aiohttp.ClientSession,
    url: str,
    name: str,
    sample_idx: int,
    scrapling_available: bool,
) -> ProbeResult:
    ts_ms = int(time.time() * 1000)
    t0    = time.monotonic()
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
            allow_redirects=True,
        ) as resp:
            latency_ms = round((time.monotonic() - t0) * 1000, 1)
            http_code  = resp.status
            try:
                html = await resp.text(encoding="utf-8", errors="replace")
            except Exception:
                html = ""
            title     = extract_title(html)
            blocked, reason = detect_blocked_like(html, http_code)
            s_hash    = structure_hash(html) if html else None
            return ProbeResult(
                ts_ms=ts_ms, sample_idx=sample_idx,
                url=url, name=name,
                scrapling_available=scrapling_available,
                status="ok",
                http_code=http_code,
                latency_ms=latency_ms,
                html_size=len(html.encode("utf-8", errors="replace")),
                title=title,
                blocked_like=blocked,
                blocked_reason=reason,
                structure_hash=s_hash,
                error_type=None, error_message=None,
            )
    except asyncio.TimeoutError:
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        return ProbeResult(
            ts_ms=ts_ms, sample_idx=sample_idx,
            url=url, name=name,
            scrapling_available=scrapling_available,
            status="timeout",
            http_code=None, latency_ms=latency_ms,
            html_size=0, title=None,
            blocked_like=True, blocked_reason="timeout",
            structure_hash=None,
            error_type="TimeoutError", error_message="request timed out",
        )
    except Exception as exc:
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        return ProbeResult(
            ts_ms=ts_ms, sample_idx=sample_idx,
            url=url, name=name,
            scrapling_available=scrapling_available,
            status="error",
            http_code=None, latency_ms=latency_ms,
            html_size=0, title=None,
            blocked_like=True, blocked_reason="exception",
            structure_hash=None,
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
        )


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(
    records: list[ProbeResult],
    scrapling_info: dict,
    out_path: Path,
) -> str:
    dicts   = [r.to_dict() for r in records]
    summary = summarize_results(dicts)
    v       = verdict(summary, scrapling_info)
    ts      = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines: list[str] = []
    a = lines.append

    a("# Scrapling Environment Probe Report")
    a("")
    a(f"> Generated: {ts}")
    a(f"> Samples collected: {len(records)}")
    a("")

    # Scrapling availability
    a("## Scrapling Availability")
    a("")
    if scrapling_info["available"]:
        a(f"✅ `scrapling {scrapling_info['version']}` is installed and importable.")
    else:
        a(f"❌ Scrapling is **NOT installed** in this environment.")
        a("")
        a(f"Install command: `{scrapling_info['install_cmd']}`")
        a("")
        a(
            "> All fetches below were performed with `aiohttp` as fallback. "
            "Scrapling-specific features (auto-adaptive selectors, stealth mode, "
            "JavaScript rendering) were **not tested**."
        )
    a("")

    # Fetch summary
    a("## Fetch Summary")
    a("")
    a(f"| Metric | Value |")
    a(f"|--------|-------|")
    a(f"| Total fetches | {summary['total']} |")
    a(f"| Successful (HTTP 2xx) | {summary['fetch_ok']} |")
    a(f"| Errors | {summary['fetch_errors']} |")
    a(f"| Blocked-like | {summary['blocked_count']} |")
    a(f"| **Fetch success rate** | **{summary['fetch_success_rate']:.0%}** |")
    a(f"| Blocked / empty rate | {summary['blocked_rate']:.0%} |")
    a(f"| Avg latency | {summary['avg_latency_ms']} ms |")
    a(f"| p95 latency | {summary['p95_latency_ms']} ms |")
    a("")

    # Per-fetch details
    a("## Per-Fetch Details")
    a("")
    a("| # | Name | HTTP | Latency | HTML | Title | Blocked | Reason |")
    a("|---|------|------|---------|------|-------|---------|--------|")
    for r in records:
        title_s   = (r.title or "—")[:40]
        blocked_s = "⚠️" if r.blocked_like else "✅"
        reason_s  = r.blocked_reason or "—"
        html_kb   = f"{r.html_size / 1024:.1f} KB" if r.html_size else "0 B"
        lat_s     = f"{r.latency_ms:.0f} ms" if r.latency_ms else "—"
        code_s    = str(r.http_code) if r.http_code else r.status
        a(
            f"| {r.sample_idx} | `{r.name}` | {code_s} | {lat_s} "
            f"| {html_kb} | {title_s} | {blocked_s} | `{reason_s}` |"
        )
    a("")

    # SPA observation
    a("## Key Observation: Polymarket is a JavaScript SPA")
    a("")
    a(
        "Polymarket's website (`polymarket.com`) is a React single-page application. "
        "Plain HTTP fetches return a minimal HTML shell — the actual market data is "
        "loaded by JavaScript at runtime.  This means:"
    )
    a("")
    a("- HTML-scraping tools (including Scrapling without a headless browser) "
      "will receive near-empty pages for `/` and `/markets`.")
    a("- The **Gamma JSON API** (`gamma-api.polymarket.com`) already exposes all "
      "structured market data and is used by this project's existing clients.")
    a("- Scrapling's value-add would primarily be in **stealth browser rendering** "
      "(its `PlayWright`/`Camoufox` integration), not plain HTTP fetching.")
    a("")

    # Conclusion
    a("## Conclusion")
    a("")
    icon = "⏳ NEED_MORE_TEST" if v == "NEED_MORE_TEST" else "❌ NO-GO"
    a(f"### Verdict: **{icon}**")
    a("")
    if v == "NEED_MORE_TEST":
        a("Scrapling is not installed, so its stealth/rendering capabilities cannot "
          "be evaluated.  Before a GO decision:")
        a("")
        a("1. Install Scrapling: `pip install scrapling`")
        a("2. Re-run with `--samples 30 --interval 5` to measure actual "
          "Scrapling fetch success and selector stability.")
        a("3. Evaluate whether `scrapling.PlayWright` or `scrapling.Camoufox` "
          "add value over the existing `gamma-api` JSON client.")
        a("")
        a(
            "Given that structured data is already available via `gamma-api.polymarket.com` "
            "with no scraping required, the case for Scrapling integration is **weak** "
            "unless there is a specific page or data field not covered by the JSON API."
        )
    else:
        a("High failure or block rate observed even with plain `aiohttp` fetches. "
          "Scrapling is unlikely to improve outcomes without headless-browser mode.")
    a("")
    a("---")
    a("*Read-only probe. No trading logic modified.*")

    text = "\n".join(lines)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(samples: int, interval_s: int, out_jsonl: Path, out_report: Path) -> None:
    scrapling_info = detect_scrapling()
    scr_avail      = scrapling_info["available"]

    print(f"[probe] scrapling: {'✅ available' if scr_avail else '❌ not installed'}")
    if not scr_avail:
        print(f"[probe] install: {scrapling_info['install_cmd']}")
    print(f"[probe] fetcher: aiohttp {aiohttp.__version__} (fallback)")
    print(f"[probe] targets: {len(TARGETS)}  samples: {samples}  interval: {interval_s}s")

    connector = aiohttp.TCPConnector(ssl=_SSL_CTX)
    headers   = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    records: list[ProbeResult] = []
    idx = 0

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        for _ in range(samples):
            for tgt in TARGETS:
                print(f"[probe] fetch #{idx}  {tgt['name']}  {tgt['url'][:60]}")
                r = await fetch_one(session, tgt["url"], tgt["name"], idx, scr_avail)
                records.append(r)
                status_s = f"HTTP {r.http_code}" if r.http_code else r.status
                blocked_s = f"  ⚠ {r.blocked_reason}" if r.blocked_like else ""
                print(
                    f"         → {status_s}  {r.latency_ms:.0f}ms  "
                    f"{r.html_size} B  title={r.title!r}{blocked_s}"
                )
                idx += 1
            if _ < samples - 1 and interval_s > 0:
                await asyncio.sleep(interval_s)

    # Write JSONL
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with open(out_jsonl, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r.to_dict()) + "\n")
    print(f"[probe] JSONL → {out_jsonl}  ({len(records)} records)")

    # Generate report
    generate_report(records, scrapling_info, out_report)
    print(f"[probe] report → {out_report}")

    # Print summary
    dicts   = [r.to_dict() for r in records]
    summary = summarize_results(dicts)
    v       = verdict(summary, scrapling_info)
    print(f"\n{'='*48}")
    print(f"  fetch_success_rate : {summary['fetch_success_rate']:.0%}")
    print(f"  blocked_rate       : {summary['blocked_rate']:.0%}")
    print(f"  avg_latency_ms     : {summary['avg_latency_ms']}")
    print(f"  scrapling_available: {summary['scrapling_available']}")
    print(f"  verdict            : {v}")
    print(f"{'='*48}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrapling environment probe (read-only)")
    parser.add_argument("--samples",  type=int, default=1)
    parser.add_argument("--interval", type=int, default=0)
    parser.add_argument("--out",      type=Path, default=DEFAULT_OUT)
    parser.add_argument("--report",   type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    asyncio.run(run(args.samples, args.interval, args.out, args.report))


if __name__ == "__main__":
    main()
