# X.com Post Probe Report

> Generated: 2026-05-22T14:56:48Z  |  URLs tested: 3  |  Method: aiohttp static GET

## Summary Metrics

| Metric | Value |
|--------|-------|
| URLs tested | 3 |
| Fetch success (2xx) | 0 / 3 — 0% |
| Content parsed | 0 / 3 — 0% |
| Blocked / login-wall | 3 / 3 — 100% |
| Login wall hits | 3 |
| Avg latency | 557.0 ms |

## Per-URL Details

| URL | Code | Quality | Blocked | Login Wall | Texts | Latency |
|-----|------|---------|---------|------------|-------|---------|
| `…1786127861592228059` | 200 | blocked | ⚠️ | ⚠️ | — | 957 ms |
| `…1767612345678901234` | 200 | blocked | ⚠️ | ⚠️ | — | 318 ms |
| `…1234567890123456789` | 200 | blocked | ⚠️ | ⚠️ | — | 396 ms |

## Findings

- **Login wall detected** on 3/3 URLs. X.com requires authentication to view post content via static GET.
- **All fetches were blocked.** Static HTML scraping of X.com posts is not viable without authentication.
- **No post text was extracted** from any fetch. X.com's SPA renders content via JavaScript; static GET returns a near-empty shell.

## Conclusion

### ❌ NO-GO for static HTML scraping

Plain `aiohttp` fetches cannot reliably retrieve X.com post content. All or most requests hit login walls or return empty shells. **Do not attempt to bypass the login wall** — this violates X's ToS and the project's hard constraints.

**Recommended paths (in order of preference):**

1. **X API v2 (official)** — `GET /2/tweets/:id` returns structured JSON with full tweet text, author, and engagement metrics. Free tier: 500k tweets/month read. Required for any production use.
2. **Stop** — if X post content is not strictly required for the research objective, drop this data source entirely.
3. **Headless browser (Scrapling/Playwright)** — technically feasible but requires careful ToS review and is fragile; not recommended before evaluating the official API.

> **Verdict: NO-GO (static HTML)**

---
*Read-only probe. No login attempted. No trading logic modified.*