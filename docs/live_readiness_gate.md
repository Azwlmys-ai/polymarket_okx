# Live Readiness Gate

**Generated:** 2026-05-22  
**System:** OKX → Polymarket directional alpha (mvp_runner.py)  
**Target:** 50–100 USDC micro live readiness assessment

---

## Verdict

> # ✅ PAPER READY — Blockers 2 & 3 resolved (2026-05-22)
>
> Session loss cap and safety gate are now enforced.  
> **Still NOT MICRO LIVE READY**: Blocker 1 (execution layer) does not exist.

---

## 1. Safety Checklist

| # | Check | Status | Detail |
|---|-------|--------|--------|
| 1 | Forced DRY_RUN / no real-order code | ✅ | Zero POST calls anywhere. All HTTP is GET-only. `polymarket_client.py` and `okx_ws.py` explicitly state no order placement. |
| 2 | Real order code path exists | ✅ SAFE | No Polymarket CLOB signing, no wallet, no `place_order`. There is no code path that can trigger a real trade. |
| 3 | Accidental live trade risk | ✅ SAFE | Impossible at present — execution layer does not exist. |
| 4 | Single-trade max risk | ✅ | `RISK_PER_TRADE_PCT = 0.02` (2% of session cash). On 100 USDC: max $2.00/trade. |
| 5 | Daily / session loss cap | ✅ FIXED | `MAX_SESSION_LOSS_PCT = 0.20`. `_check_session_loss_cap()` computes full equity (cash + MTM open positions) and triggers `state.shutdown` when ≥ 20% drawdown. Called before every new position open. |
| 6 | Max concurrent positions | ⚠️ SOFT | Per-asset guard (`already_open` check): max 1 position per asset = max 3 simultaneous (BTC+ETH+SOL). No global cap. |
| 7 | Stop-loss exists | ✅ | Dual stop: relative (−12% from entry) and absolute (YES ≤ 0.40). Triggered 3× in 14h paper run. |
| 8 | Signal cooldown | ✅ | 30s per-asset cooldown. Prevents rapid re-entry on same asset. |
| 9 | Paper trade records | ⚠️ PARTIAL | In-memory only during session. Written to `MVP_RUN_REPORT.md` at shutdown. **Not persisted to SQLite from mvp_runner — a crash loses all trade history.** |
| 10 | PnL report generation | ✅ | `_generate_report()` writes Markdown with full trade log, win rate, drawdown, per-asset stats. |
| 11 | Paper / live distinction | ✅ STRUCTURAL | The distinction is absolute: live trading infrastructure does not exist (see blocker #1). |
| 12 | Kill switch | ✅ | `state.shutdown` asyncio.Event. SIGINT/SIGTERM triggers graceful close of all positions + report. |
| 13 | Safety framework enforced | ✅ FIXED | `main()` now calls `get_settings().safety_flags.enforce_phase_one()` as its first action. Raises `SafetyBoundaryError` and aborts if any unsafe env var is set. |

---

## 2. Paper Run Evidence

From `MVP_RUN_REPORT_14h_stoploss.md` (2026-05-14, 14 hours):

| Metric | Value |
|--------|-------|
| Duration | 14h (50,400s) |
| Trades | 117 |
| Win rate | 28.2% |
| Net P&L | +23.92 USDC on 1,000 |
| Max drawdown | 9.86 USDC (≈ 1%) |
| Stop-loss fires | 3 |
| OKX ticks processed | 395,376 |
| Poly markets polled | 6,293 |
| Errors | 11 |

**Observations:**
- Positive P&L is driven by 4–5 fat-tail winners (max +14.5 USDC). Median trade is −$0.08.
- 72% of trades lose small, ~5% win large. This is acceptable for a momentum strategy but needs 500+ trades before statistical confidence.
- Signal quality risk: single-source OKX-only, no consensus filter. Confirmed from code.

---

## 3. Blockers for MICRO LIVE READY

Only 3. Nothing else.

### 🔴 BLOCKER 1 — No execution layer (fatal)

**Polymarket CLOB order placement does not exist.**

- No EIP-712 / wallet signing.
- No private key handling.
- No `POST /order` to Polymarket CLOB.
- `src/safety.py` has `allow_real_trading: bool = False` hardcoded — there is no bypass mechanism even if one wanted one.

This is not a config switch. Building the execution layer requires: wallet integration, Polymarket CLOB API signing (EIP-712), USDC balance management on Polygon. This is weeks of work and is a deliberate architectural choice to not exist yet.

**Fix:** Implement `src/polymarket_executor.py` (read the Polymarket CLOB API docs for authenticated order placement). Gate it behind `settings.safety_flags.allow_real_trading`. Only unblock after blockers 2 and 3 are resolved.

---

### 🔴 BLOCKER 2 — No session loss cap

**There is no hard ceiling on session losses.**

Current behavior on a 50 USDC account:
- 3 assets × 2% risk = 6% max theoretical per-signal
- If stop-losses fire on 3 concurrent positions → −15–25% loss in minutes
- No automatic shutdown on drawdown

**Fix (5 lines in mvp_runner.py):**

```python
# After each position close, check session drawdown
MAX_SESSION_LOSS_PCT = 0.20   # 20% of initial cash → emergency stop
if (INITIAL_CASH - state.cash) / INITIAL_CASH >= MAX_SESSION_LOSS_PCT:
    log.warning("SESSION LOSS CAP HIT (%.1f%%) — shutting down",
                MAX_SESSION_LOSS_PCT * 100)
    state.shutdown.set()
```

This is the minimum circuit breaker before any real capital is risked.

---

### 🔴 BLOCKER 3 — Safety framework bypassed in mvp_runner

**`mvp_runner.py` never calls `get_settings()` or `enforce_phase_one()`.**

`src/safety.py` and `src/config.py` are the correct safety layer. They are called by `src/main.py`. But `mvp_runner.py` is a self-contained runner that starts directly without loading `Settings` — meaning `SafetyBoundaryError` can never fire from the runner.

When `allow_real_trading` capability is eventually added, this gap would mean the safety check never runs.

**Fix (2 lines at top of `mvp_runner.main()`):**

```python
from src.config import get_settings
get_settings()   # raises SafetyBoundaryError if unsafe flags set
```

This makes the safety layer active from day one, before execution capability is added.

---

## 4. What Has Been Validated (not worth continuing)

| Direction | Result |
|-----------|--------|
| Rebate market making | ❌ Net PnL = −551 USDC. Hedge cost >> rebate. Dead end. |
| Passive MM / inventory hedging | ❌ One-sided inventory accumulation is fatal for small capital. |
| Chainlink anchor proxy MM | ❌ priceToBeat unavailable during live window. Cannot execute. |

---

## 5. Next Minimum Action

**Fix blocker 2 first (session loss cap). It is 5 lines of code.**  
Do not proceed to execution layer until both blocker 2 and blocker 3 are resolved.

Do not expand the system. Do not add features. Fix the 3 blockers in order.

---

*This report is a static readiness assessment. No code was changed. No orders were placed.*
