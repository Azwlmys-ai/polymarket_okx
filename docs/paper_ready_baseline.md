# Paper-Ready Baseline — 2026-05-22

## Current Status

> **PAPER READY — NOT MICRO LIVE READY**

The system can run safely as a paper trading simulator. Real-money live trading is structurally blocked.

---

## Live Readiness Gate Checklist

| # | Blocker | Status |
|---|---------|--------|
| 1 | **No Polymarket execution layer** — no CLOB signing, no wallet, no POST /order | ❌ **Does not exist.** Not scheduled. |
| 2 | **No session loss cap** | ✅ Fixed 2026-05-22 |
| 3 | **Safety gate not enforced in runner** | ✅ Fixed 2026-05-22 |

Full checklist: `reports/live_readiness_gate.md`

---

## What Was Fixed (v0.2)

### Blocker 2 — Session loss cap (`mvp_runner.py`)

New constant:
```python
MAX_SESSION_LOSS_PCT = 0.20
```

New helpers:
- `_get_session_equity()` — cash + mark-to-market value of all open positions
- `_check_session_loss_cap()` — triggers `state.shutdown` when equity drops ≥ 20%

Called before every new position open. Uses full MTM equity, not just cash balance.

### Blocker 3 — Safety gate (`mvp_runner.py → main()`)

```python
from src.config import get_settings
get_settings().safety_flags.enforce_phase_one()   # first line of main()
```

Raises `SafetyBoundaryError` and aborts if `ALLOW_REAL_TRADING`, `ALLOW_PRIVATE_KEYS`, or other unsafe env vars are set. Default is always safe.

---

## Paper Run Evidence

From 14-hour run (2026-05-14):

| Metric | Value |
|--------|-------|
| Trades | 117 |
| Win rate | 28.2% |
| Net P&L | +23.9 USDC / 1000 |
| Max drawdown | 9.9 USDC (≈ 1%) |
| Stop-losses fired | 3 |
| OKX ticks | 395,376 |

Not statistically significant yet. Need ≥500 trades for edge confirmation.

---

## What Is Not Allowed

- Real Polymarket orders
- Wallet connection / private key handling
- Rebate market making
- Inventory hedging
- Live CLOB access
- Any executor that POSTs to any exchange

---

## Next Recommended Step

**24–72 hour dry-run** to accumulate ≥300 paper trades and generate a statistically-testable P&L distribution.

Command:
```bash
python3 mvp_runner.py --duration 86400 --report reports/24h_paper_run.md
```

Do not proceed toward live trading until:
1. Win rate is validated over ≥500 trades
2. Multi-exchange consensus filter is implemented (see `docs/strategy_review_for_external_models.md`)
3. Blocker 1 is deliberately built with full safety review

---

## Test Status (v0.2)

```
789 passed  (pytest, 2026-05-22)
```

Files:
- `tests/test_session_loss_cap.py` — 21 tests for loss cap + safety gate
- `tests/test_mvp_runner_stop_loss.py` — existing stop-loss tests
- All other existing tests unmodified
