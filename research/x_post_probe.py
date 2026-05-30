"""
x_post_probe.py — read-only probe for public X.com post fetchability.

Attempts a plain aiohttp GET on each supplied post URL and records whether
content is reachable without authentication.  Does NOT log in, does NOT use
cookies, does NOT bypass blocks or CAPTCHAs.  All blocks are recorded as-is.

NO TRADING LOGIC.  NO AUTH.  READ-ONLY.

Usage:
    python3 research/x_post_probe.py \
        --urls https://x.com/Polymarket/status/1234 \
               https://x.com/elonmusk/status/5678 \
        --interval 5

Outputs:
    research/x_post_probe_results.jsonl
    research/x_post_probe_report.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import ssl
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_URLS = [
    "https://x.com/Polymarket/status/1786127861592228059",
    "https://x.com/Polymarket/status/1767612345678901234",
    "https://x.com/elonmusk/status/1234567890123456789",
]
DEFAULT_OUT    = Path("research/x_post_probe_results.jsonl")
DEFAULT_REPORT = Path("research/x_post_probe_report.md")
HTTP_TIMEOUT   = 15
MIN_CONTENT_BYTES = 200
INTER_REQUEST_S   = 5     # minimum gap between fetches (respect rate limits)

UA_BROWSER = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode    = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# Pure functions (fully testable offline)
# ---------------------------------------------------------------------------

def normalize_x_url(url: str) -> str:
    """
    Canonicalise twitter.com and x.com post URLs to x.com form.

    Returns the input unchanged if it doesn't match either domain.
    """
    url = url.strip()
    url = re.sub(r"https?://(www\.)?(twitter\.com|x\.com)", "https://x.com", url)
    return url


def detect_block_signals(html: str, status_code: int) -> dict[str, bool]:
    """
    Scan HTML and status code for evidence of walls / rate limits.

    Returns a dict with boolean flags:
        has_login_wall   – redirect / prompt asking for login
        has_captcha      – CAPTCHA challenge detected
        has_rate_limit   – 429 or rate-limit language
        blocked          – any of the above is True
    """
    low = html.lower() if html else ""

    has_rate_limit = (
        status_code == 429
        or "rate limit" in low
        or "too many requests" in low
    )
    has_captcha = any(s in low for s in (
        "captcha",
        "challenge-platform",
        "recaptcha",
        "hcaptcha",
    ))
    _login_strings = (
        "sign in to x",
        "log in to twitter",
        "loginredirect",
        "this page requires you to log in",
        "you need to log in",
        "authwall",
        "auth_flow",
        "twitter login",
        "create account",
        "don't have an account",
    )
    # Thin SPA shell: "log in" present but no blue-verified marker → login wall
    _sparse_login_wall = (
        "log in" in low
        and '"isblueVerified"' not in low
        and 0 < len(low) < 20_000
    )
    # X.com SPA shell: large HTML with no og: meta tags (all content needs JS)
    # All post URLs return identical boilerplate → no post-specific content
    _spa_shell = (
        len(low) > 50_000
        and "og:title" not in low
        and "og:description" not in low
        and low.count("login") >= 5     # login appears many times in the JS bundle
    )
    has_login_wall = (
        status_code in (401, 403)
        or any(s in low for s in _login_strings)
        or _sparse_login_wall
        or _spa_shell
    )

    empty_response = not html or len(html.strip()) == 0
    blocked = (
        has_login_wall or has_captcha or has_rate_limit
        or status_code in (401, 403, 429, 503)
        or empty_response
    )
    return {
        "has_login_wall": bool(has_login_wall),
        "has_captcha":    bool(has_captcha),
        "has_rate_limit": bool(has_rate_limit),
        "blocked":        bool(blocked),
    }


def extract_meta_text(html: str) -> dict[str, str | None]:
    """
    Extract human-readable text from HTML meta tags and hydration JSON.

    Priority:
      1. og:title
      2. og:description
      3. <title>
      4. First readable text from inline JSON/hydration blobs

    Returns dict with keys: og_title, og_description, title, hydration_text
    """
    def _og(prop: str) -> str | None:
        m = re.search(
            rf'<meta[^>]+property=["\']og:{prop}["\'][^>]+content=["\']([^"\']+)["\']',
            html, re.IGNORECASE
        )
        if not m:
            # also try reversed attribute order
            m = re.search(
                rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:{prop}["\']',
                html, re.IGNORECASE
            )
        return _clean(m.group(1)) if m else None

    def _title() -> str | None:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        return _clean(m.group(1)) if m else None

    def _hydration() -> str | None:
        # Look for tweet text embedded in window.__INITIAL_STATE__ or script JSON
        for pat in (
            r'"full_text"\s*:\s*"([^"]{20,280})"',
            r'"text"\s*:\s*"([^"]{20,280})"',
            r'"body"\s*:\s*"([^"]{20,280})"',
        ):
            m = re.search(pat, html)
            if m:
                candidate = _clean(m.group(1))
                # skip obvious non-tweet strings (URLs, JSON noise)
                if candidate and not candidate.startswith("http") and " " in candidate:
                    return candidate
        return None

    return {
        "og_title":        _og("title"),
        "og_description":  _og("description"),
        "title":           _title(),
        "hydration_text":  _hydration(),
    }


def classify_fetch_result(
    status_code: int | None,
    html: str,
    signals: dict[str, bool],
    meta: dict[str, str | None],
) -> dict[str, Any]:
    """
    Combine raw signals and meta into a classification summary.

    Returns:
        success           – True only if we got readable post content
        blocked           – any wall/rate-limit detected
        has_content       – at least one meta field is non-None
        text_candidates   – list of non-None extracted texts
        quality           – "full" | "partial" | "empty" | "blocked"
    """
    texts = [
        v for v in (
            meta.get("og_description"),
            meta.get("og_title"),
            meta.get("hydration_text"),
            meta.get("title"),
        )
        if v and len(v) > 10
    ]

    blocked     = signals.get("blocked", False)
    has_content = bool(texts) and not blocked

    if blocked:
        quality = "blocked"
    elif not texts:
        quality = "empty"
    elif meta.get("og_description"):
        quality = "full"
    else:
        quality = "partial"

    success = quality in ("full", "partial")

    return {
        "success":        success,
        "blocked":        blocked,
        "has_content":    has_content,
        "text_candidates": texts,
        "quality":        quality,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean(s: str) -> str | None:
    if not s:
        return None
    import html as html_mod
    s = html_mod.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


# ---------------------------------------------------------------------------
# Async fetch
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    url:           str
    status_code:   int | None
    success:       bool
    blocked:       bool
    latency_ms:    float | None
    html_size:     int
    title:         str | None
    text_candidates: list[str]
    has_login_wall: bool
    has_captcha:   bool
    has_rate_limit: bool
    quality:       str        # full | partial | empty | blocked
    error:         str | None

    def to_dict(self) -> dict:
        return asdict(self)


async def fetch_post(
    session: aiohttp.ClientSession,
    url: str,
) -> ProbeResult:
    url = normalize_x_url(url)
    t0  = time.monotonic()

    def _elapsed() -> float:
        return round((time.monotonic() - t0) * 1000, 1)

    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
            allow_redirects=True,
        ) as resp:
            latency_ms  = _elapsed()
            status_code = resp.status
            try:
                html = await resp.text(encoding="utf-8", errors="replace")
            except Exception:
                html = ""

        signals = detect_block_signals(html, status_code)
        meta    = extract_meta_text(html)
        cls     = classify_fetch_result(status_code, html, signals, meta)

        return ProbeResult(
            url           = url,
            status_code   = status_code,
            success       = cls["success"],
            blocked       = cls["blocked"],
            latency_ms    = latency_ms,
            html_size     = len(html.encode("utf-8", errors="replace")),
            title         = meta.get("title"),
            text_candidates = cls["text_candidates"],
            has_login_wall = signals["has_login_wall"],
            has_captcha    = signals["has_captcha"],
            has_rate_limit = signals["has_rate_limit"],
            quality        = cls["quality"],
            error          = None,
        )

    except asyncio.TimeoutError:
        return ProbeResult(
            url=url, status_code=None, success=False, blocked=True,
            latency_ms=_elapsed(), html_size=0, title=None,
            text_candidates=[], has_login_wall=False,
            has_captcha=False, has_rate_limit=False,
            quality="blocked", error="TimeoutError",
        )
    except Exception as exc:
        return ProbeResult(
            url=url, status_code=None, success=False, blocked=True,
            latency_ms=_elapsed(), html_size=0, title=None,
            text_candidates=[], has_login_wall=False,
            has_captcha=False, has_rate_limit=False,
            quality="blocked", error=f"{type(exc).__name__}: {str(exc)[:150]}",
        )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(results: list[ProbeResult]) -> str:
    n        = len(results)
    ok       = sum(1 for r in results if r.success)
    blocked  = sum(1 for r in results if r.blocked)
    wall     = sum(1 for r in results if r.has_login_wall)
    parsed   = sum(1 for r in results if r.text_candidates)
    lats     = sorted(r.latency_ms for r in results if r.latency_ms)
    avg_lat  = round(sum(lats) / len(lats), 0) if lats else None
    ts       = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines: list[str] = []
    a = lines.append

    a("# X.com Post Probe Report")
    a("")
    a(f"> Generated: {ts}  |  URLs tested: {n}  |  Method: aiohttp static GET")
    a("")
    a("## Summary Metrics")
    a("")
    a(f"| Metric | Value |")
    a(f"|--------|-------|")
    a(f"| URLs tested | {n} |")
    a(f"| Fetch success (2xx) | {ok} / {n} — {ok/n:.0%} |" if n else "| Fetch success | — |")
    a(f"| Content parsed | {parsed} / {n} — {parsed/n:.0%} |" if n else "| Content parsed | — |")
    a(f"| Blocked / login-wall | {blocked} / {n} — {blocked/n:.0%} |" if n else "| Blocked | — |")
    a(f"| Login wall hits | {wall} |")
    a(f"| Avg latency | {avg_lat} ms |")
    a("")

    a("## Per-URL Details")
    a("")
    a("| URL | Code | Quality | Blocked | Login Wall | Texts | Latency |")
    a("|-----|------|---------|---------|------------|-------|---------|")
    for r in results:
        short_url = r.url.split("status/")[-1][:20]
        code_s    = str(r.status_code) if r.status_code else (r.error or "err")[:12]
        texts_s   = f"{len(r.text_candidates)} found" if r.text_candidates else "—"
        lat_s     = f"{r.latency_ms:.0f} ms" if r.latency_ms else "—"
        a(
            f"| `…{short_url}` | {code_s} | {r.quality} "
            f"| {'⚠️' if r.blocked else '✅'} "
            f"| {'⚠️' if r.has_login_wall else '—'} "
            f"| {texts_s} | {lat_s} |"
        )
    a("")

    a("## Findings")
    a("")
    if wall > 0:
        a(f"- **Login wall detected** on {wall}/{n} URLs. "
          "X.com requires authentication to view post content via static GET.")
    if blocked == n:
        a("- **All fetches were blocked.** Static HTML scraping of X.com posts "
          "is not viable without authentication.")
    elif ok > 0:
        a(f"- {ok}/{n} URLs returned HTTP 2xx. "
          "Some meta tags may be populated by server-side rendering.")
    if parsed == 0:
        a("- **No post text was extracted** from any fetch. "
          "X.com's SPA renders content via JavaScript; "
          "static GET returns a near-empty shell.")
    a("")

    a("## Conclusion")
    a("")
    # Determine verdict
    if blocked == n or parsed == 0:
        verdict = "NO-GO (static HTML)"
        a("### ❌ NO-GO for static HTML scraping")
        a("")
        a("Plain `aiohttp` fetches cannot reliably retrieve X.com post content. "
          "All or most requests hit login walls or return empty shells. "
          "**Do not attempt to bypass the login wall** — this violates X's ToS "
          "and the project's hard constraints.")
        a("")
        a("**Recommended paths (in order of preference):**")
        a("")
        a("1. **X API v2 (official)** — `GET /2/tweets/:id` returns structured JSON "
          "with full tweet text, author, and engagement metrics. "
          "Free tier: 500k tweets/month read. "
          "Required for any production use.")
        a("2. **Stop** — if X post content is not strictly required for the "
          "research objective, drop this data source entirely.")
        a("3. **Headless browser (Scrapling/Playwright)** — technically feasible "
          "but requires careful ToS review and is fragile; "
          "not recommended before evaluating the official API.")
    elif ok == n and parsed >= n // 2:
        verdict = "NEED_MORE_TEST"
        a("### ⏳ NEED_MORE_TEST")
        a("Some content was extracted. Run a larger sample (≥ 30 URLs) "
          "before deciding on headless or API approaches.")
    else:
        verdict = "NEED_MORE_TEST"
        a("### ⏳ NEED_MORE_TEST")
        a("Partial results. Insufficient data for a GO decision.")

    a("")
    a(f"> **Verdict: {verdict}**")
    a("")
    a("---")
    a("*Read-only probe. No login attempted. No trading logic modified.*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(urls: list[str], interval_s: int, out_jsonl: Path, out_report: Path) -> None:
    # X.com sends very large CSP headers; raise aiohttp's per-field limit.
    connector = aiohttp.TCPConnector(ssl=_SSL_CTX, limit=1)
    headers   = {
        "User-Agent":      UA_BROWSER,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    # 64 KB per header field (default is 8190 B, too small for X.com's CSP)
    _MAX_FIELD = 65_536

    results: list[ProbeResult] = []
    print(f"[x-probe] Testing {len(urls)} URL(s)  interval={interval_s}s")

    async with aiohttp.ClientSession(
        connector=connector, headers=headers,
        max_field_size=_MAX_FIELD,
    ) as session:
        for i, url in enumerate(urls):
            if i > 0 and interval_s > 0:
                print(f"[x-probe] sleeping {interval_s}s …")
                await asyncio.sleep(interval_s)

            norm = normalize_x_url(url)
            print(f"[x-probe] [{i+1}/{len(urls)}] {norm}")
            r = await fetch_post(session, norm)
            results.append(r)

            flag = "blocked" if r.blocked else "ok"
            print(
                f"         → {r.status_code or r.error}  {r.latency_ms:.0f}ms  "
                f"{r.html_size}B  quality={r.quality}  [{flag}]"
            )
            if r.text_candidates:
                print(f"         → texts: {r.text_candidates[0][:80]!r}")

    # Write JSONL
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with open(out_jsonl, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r.to_dict()) + "\n")
    print(f"[x-probe] JSONL → {out_jsonl}")

    # Write report
    report = build_report(results)
    out_report.parent.mkdir(parents=True, exist_ok=True)
    out_report.write_text(report, encoding="utf-8")
    print(f"[x-probe] report → {out_report}")


def main() -> None:
    parser = argparse.ArgumentParser(description="X.com post probe (read-only)")
    parser.add_argument("--urls",     nargs="+", default=DEFAULT_URLS,
                        help="Post URLs to probe (max 3 recommended)")
    parser.add_argument("--interval", type=int, default=INTER_REQUEST_S,
                        help="Seconds between fetches (default 5)")
    parser.add_argument("--out",      type=Path, default=DEFAULT_OUT)
    parser.add_argument("--report",   type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    urls = args.urls[:3]    # hard cap: never more than 3 in one run
    asyncio.run(run(urls, args.interval, args.out, args.report))


if __name__ == "__main__":
    main()
