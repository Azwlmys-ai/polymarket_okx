# AGENT_TASKS.md

## Active Task

Status: APPROVED_FOR_CLAUDE
Step: F-5 — Clarify open_no_exit cash accounting
Assigned to: Claude Code

## Task Brief

F-4 is approved. The next task is F-5: make the paper trading summary clearer
when simulated positions are `open_no_exit`.

Problem:
- `paper_trader.format_summary()` can show a confusing simulated cash change
  when there are `open_no_exit` positions.
- Accounting appears paradoxical because `open_no_exit` deducts notional and
  fees while the position remains unresolved and excluded from closed-trade PnL.

Requirements:
- Stay inside `/Users/libo/polymarket_okx`.
- Keep the change limited to output wording and focused tests.
- Do not change simulation accounting unless a real bug is found and documented.
- Add a concise note to the paper summary explaining that `open_no_exit`
  positions are unresolved, have not recovered notional, and are excluded from
  closed-trade PnL metrics.
- Do not claim real profitability.
- Do not use `/tmp`, `/private/tmp`, project-external paths, browser automation,
  API keys, private keys, wallet signing, withdrawals, real trading, or strategy
  optimization.
- Run a focused relevant test that does not require project-external SQLite.
- Update `AGENT_STATE.md` with files changed, command, exit code, and result.
- Stop after F-5 and wait for Codex review.

## Recently Approved

F-4 — Document OKX market_id format:
- `src/market_mapper.py` and `src/lag_recorder.py` document that OKX market IDs
  must be bare instrument IDs such as `BTC-USDT`, not prefixed IDs such as
  `okx:BTC-USDT`.
- Focused verification passed:
  `.venv/bin/python -m pytest tests/test_lag_recorder.py::TestMarketMapper tests/test_lag_recorder.py::TestDetectMoves::test_move_asset_derived_from_market_id tests/test_e2e_in_memory_substitute.py -q`
  -> `15 passed in 0.15s`.

## Later Follow-Up Candidates

- Re-run official E2E with live public data after OKX/Polymarket network
  connectivity is available.
- Decide whether Phase 1 should move to the next roadmap step after F-5.

## Non-Goals

- Real order execution.
- Strategy optimization.
- New trading advice.
- Real profitability claims.
