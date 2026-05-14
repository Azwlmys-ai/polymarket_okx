# AGENT_STATE.md

## Current State

Agent: Claude (self-review)
Step: MVP — mvp_runner.py syntax fix + ready-to-run
Status: READY — awaiting user to run on macOS host

---

## Codex Compression Summary

Current step:
F-4 — review documentation for OKX `market_id` format.

Files reviewed:
- `src/market_mapper.py`
- `src/lag_recorder.py`
- `tests/test_lag_recorder.py`
- `tests/test_e2e_in_memory_substitute.py`

Behavior verified:
- `src/market_mapper.py` documents that OKX `market_id` values must be bare
  instrument IDs such as `BTC-USDT`, not prefixed values such as
  `okx:BTC-USDT`.
- `src/lag_recorder.py` documents why prefixed IDs can fall through to a
  non-matching fallback and produce 0 lag records.

Commands run:
- `.venv/bin/python -m pytest tests/test_lag_recorder.py::TestMarketMapper tests/test_lag_recorder.py::TestDetectMoves::test_move_asset_derived_from_market_id tests/test_e2e_in_memory_substitute.py -q`
  - Exit: 0
  - Result: `15 passed in 0.15s`

Commands intentionally not run:
- Full test suite, because existing legacy tests use pytest `tmp_path`, which
  can resolve to project-external temporary directories and is outside the
  strict boundary for this task.

Files changed by Codex this turn:
- `AGENT_REVIEW.md`
- `AGENT_TASKS.md`
- `AGENT_STATE.md`

Test result:
- F-4 approved.

Known risks:
- Official E2E remains PARTIAL because live public API collection produced 0
  snapshots in the host run.
- OKX WebSocket connectivity remains a network/environment issue.

Next requested action (from Codex, completed):
- F-5: clarify `open_no_exit` cash accounting in `paper_trader.format_summary()`.
  DONE — see Claude compression summary for F-5 below.

---

## Claude Compression Summary — F-5

Step: F-5 COMPLETE — open_no_exit cash accounting note added.

Files changed: src/paper_trader.py, tests/test_paper_trader.py (+3 tests)
Full suite: 344 passed.

---

## Claude Compression Summary — Step 8

Current step: Step 8 COMPLETE — `status` CLI command implemented.
Self-reviewed (Claude); Codex not available this cycle.

New files:
  src/db_status.py          — query_status() + format_status()
  tests/test_db_status.py   — 25 unit tests

Modified files:
  src/main.py               — added `status` subparser + handler
  README.md                 — added `status` command docs, updated status table

Functionality:
  `python -m src.main status` prints a read-only DB summary:
    - market_snapshots: total + per-source count + latest timestamp
    - lag_records: total count + latest timestamp
    - paper_trades: total + by-status breakdown
    - DB file path + size
  No writes, no network, no trading logic.
  Gracefully handles missing/corrupt DB (returns zeros, never crashes).

Test results:
  python3 -m pytest tests/test_db_status.py -v  -> 25 passed in 1.28s
  python3 -m pytest --tb=no -q                  -> 369 passed in 3.02s

Self-review verdict: APPROVED (see AGENT_REVIEW.md)

Next candidates:
  (a) Step 9 — concurrent scan (asyncio.gather for OKX + Polymarket simultaneously)
  (b) Improve report.py — add per-asset percentile table
  (c) Re-run official E2E when network access to OKX WS / Polymarket is available

---

## Claude Compression Summary

Current step: F-5 COMPLETE — open_no_exit cash accounting note added.

Files changed:
- `src/paper_trader.py`
- `tests/test_paper_trader.py`

Change in src/paper_trader.py — format_summary():
  Added a conditional note block that appears only when open_no_exit trades
  are present.  The note explains:
    - How many positions are open/unresolved.
    - The total notional (USDC) still tied up.
    - The fees already deducted.
    - That these positions are EXCLUDED from closed-trade PnL metrics.
    - That a negative net change reflects unrealised simulated exposure,
      not a real loss.
  Simulation accounting (cash flow logic) is unchanged.

New tests in tests/test_paper_trader.py — TestFormatSummary (3 new):
  test_open_no_exit_note_present_when_open_positions_exist:
    Verifies the note appears and contains "excluded" and
    "unrecovered"/"unrealised" when open_no_exit trades are present.
  test_open_no_exit_note_absent_when_no_open_positions:
    Verifies the note does NOT appear when all trades are closed.
  test_open_no_exit_note_shows_notional_and_fees:
    Verifies the note renders the exact notional and fee values.

Focused verification:
  python3 -m pytest tests/test_paper_trader.py::TestFormatSummary tests/test_e2e_in_memory_substitute.py -v
  -> 11 passed in 0.12s

Full suite:
  python3 -m pytest --tb=no -q
  -> 344 passed in 1.69s

Open findings: none remaining from E2E-1 / F-series.
  F-2 (OKX network) is environment/infrastructure, not code.
  Official E2E: PARTIAL — pending live network access.

Next requested action:
  Codex: review F-5 (paper_trader.py note + 3 tests).
  Proposed follow-up:
  (a) Close F-5 and decide on next Phase 1 roadmap step
  (b) Re-run official E2E when OKX WS and Polymarket HTTP are reachable

---

## Claude Compression Summary — MVP Runner

Timestamp: 2026-05-10 CST
Step: MVP READY — mvp_runner.py syntax fix applied, ready to run.

Bug fixed:
- Line 753: embedded ASCII double-quotes inside double-quoted string literal.
  `"原因见上方"无机会原因分析"` → `"原因见上方「无机会原因分析」"`
  SyntaxError at line 753, now passes `ast.parse()`.

Verification:
  python3 -c "import ast; ast.parse(open('mvp_runner.py').read()); print('Syntax OK')"
  → Syntax OK

  Dependencies checked: aiohttp 3.13.5 ✓ (only non-stdlib dep)

File: mvp_runner.py (~850 lines)
  - OKX WebSocket task: 3-URL auto-reconnect fallback, 20s ping
  - Polymarket Gamma polling: every 8s
  - Strategy task: every 1s, 0.5% move threshold, 60s window
  - Paper trade: entry/exit with slippage+fee sim
  - Heartbeat: every 60s
  - Auto-report: MVP_RUN_REPORT.md on exit

Run command (on macOS host):
  cd /Users/libo/polymarket_okx
  source .venv/bin/activate
  python mvp_runner.py --duration 1800

Known risks:
  - OKX WS may be blocked on user's current network (use VPN or different network)
  - Sandbox cannot run this (FUSE mount + network restrictions); must run on macOS host
