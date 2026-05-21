# Strategy Review — For External Model / Grok Audit

**Project:** polymarket_okx  
**Date:** 2026-05-22  
**Status:** Paper trading only. No real capital at risk.

---

## 1. Project Objective

Build and validate a **directional alpha system** that:
- Uses OKX / Binance / Bybit price momentum as a leading signal
- Targets Polymarket YES/NO prediction markets as the execution venue
- Operates paper-only until edge is statistically confirmed
- Does **not** pursue rebate market-making, passive quoting, or inventory hedging

The hypothesis: short-term CEX price momentum leads Polymarket option-like prices by a measurable lag, creating a brief window to trade the direction at a stale price.

---

## 2. Current Architecture

```
[OKX WebSocket / REST]
  └── okx_history[asset] (rolling 60s deque of ticks)
  └── Binance REST (price fallback only, not yet a signal source)

[Polymarket Gamma API]
  └── poly_cached_ids (discovery: tier, TTL, end_date)
  └── poly_latest[market_id] (live YES price, 3s poll)

[Strategy — _detect_signals(), 1s cadence]
  Conditions to open a paper position:
    • OKX pct_move > 0.1% in last 60s
    • Direction: UP only
    • YES price ∈ [0.47, 0.53]  (near 50/50, liquid zone)
    • ≥5 min remaining before market expiry
    • 30s per-asset cooldown
    • session equity ≥ 80% of INITIAL_CASH (loss cap)

[Paper execution]
  Entry: YES_price + 0.2% slippage
  Risk:  2% of session cash per trade
  Exit:  hold 5 min OR stop-loss (−12% relative or YES ≤ 0.40)

[Safety layer]
  • src/safety.py: SafetyFlags(allow_real_trading=False)
  • enforce_phase_one() called at mvp_runner.py startup
  • MAX_SESSION_LOSS_PCT = 0.20 (hard shutdown on 20% drawdown)
  • SIGINT/SIGTERM → graceful close + report

[Storage]
  • In-session: in-memory RunState
  • End-of-session: MVP_RUN_REPORT.md (Markdown, no SQLite from runner)
  • Research data: SQLite via src/main.py lag/paper commands
```

Core runner: `mvp_runner.py` (single file, ~1300 lines)  
Research scripts: `research/` (standalone, do not affect trading logic)

---

## 3. Directions Validated as Dead Ends

| Direction | What Was Tried | Why It Failed |
|-----------|---------------|---------------|
| **Rebate market making** | Dual-sided YES/NO quoting on xi-jinping market | 8-round sim: Net PnL = −551 USD. Hedge cost ($562) >> rebate ($9.6). Inventory one-sided accumulation is fatal. |
| **Passive quoting / inventory hedging** | Paper quote simulator with cap | Single-side fills with no offsetting flow → unhedgeable directional exposure. |
| **FIFA World Cup rewards markets** | Spread_bps 1333–6667, depth=0 | No tradeable depth near mid. All paper profit is theoretical. |
| **Chainlink anchor proxy as MM signal** | Binance T_open – $76.75 as anchor est. | priceToBeat not readable during live window. Cannot act on it without on-chain RPC. |
| **Hyperparameter search without edge** | Threshold/window sweeps | With win rate 28%, optimising parameters only overfits noise. |

---

## 4. Current Active Hypothesis

**Hypothesis:** CEX price momentum (OKX BTC/ETH/SOL) leads Polymarket binary market pricing by 30–120 seconds during active moves.

Evidence to date:
- 14-hour paper run: 117 trades, 28.2% win rate, net +23.9 USDC (on 1000 USDC)
- Positive P&L driven by ~5 fat-tail winners (single trades: +$14.5, +$11.0, +$4.6)
- Median trade: −$0.08 (fees + no-edge trades dominate by count)
- Not yet statistically significant (need ≥500 trades for 95% CI on edge)

**Key uncertainty:** Is the positive P&L from genuine alpha or from fat-tail luck in a 28% win-rate system over 117 trades?

---

## 5. Risk Controls (current state)

| Control | Status | Detail |
|---------|--------|--------|
| DRY_RUN / no real orders | ✅ Structural | Zero POST calls. No wallet, no signing, no CLOB executor. |
| Per-trade stop-loss | ✅ Active | −12% relative from entry OR YES ≤ 0.40 absolute |
| Signal cooldown | ✅ Active | 30s per asset |
| Session loss cap | ✅ Fixed (2026-05-22) | Shutdown when equity drops ≥ 20% from start. Uses MTM not just cash. |
| Safety gate | ✅ Fixed (2026-05-22) | `enforce_phase_one()` called at startup. Raises if ALLOW_REAL_TRADING=true. |
| Max concurrent positions | ⚠️ Soft | 1 per asset (max 3 concurrent). No global cap. |
| Daily loss limit | ⚠️ Not explicit | Session loss cap (20%) is the nearest proxy. |
| Trade persistence | ⚠️ In-memory | Report written at shutdown, not durable mid-session. |

---

## 6. Questions for Grok / External Model Review

### On the edge hypothesis

1. **Is the directional alpha hypothesis structurally sound?**  
   OKX momentum → Polymarket lag assumes Polymarket pricing is slow to update. Given Polymarket market makers are also watching CEX feeds, how quickly should this edge be arbitraged away?

2. **Could the 28% win rate + positive P&L be pure noise?**  
   With 117 trades and 5 fat-tail winners driving all profit, what's the probability this is a lucky draw from a zero-edge distribution?  
   What sample size and shape of P&L distribution would provide 95% confidence of a real edge?

3. **What statistical tests are most appropriate here?**  
   - Bootstrap on per-trade PnL?
   - Permutation test (shuffle signal timing)?
   - Sharpe ratio significance?

### On signal architecture

4. **Should Binance consensus be the highest-priority next fix?**  
   Current system uses OKX as the sole momentum source. Adding a Binance confirmation (both must agree direction within 5s) would reduce false signals. Is this the right lever, or is the bigger issue the YES price filter [0.47, 0.53]?

5. **Is the 5-minute hold window evidence-based?**  
   The hold window is a constant, not calibrated to measured OKX→Polymarket lag distribution. What's the correct methodology to calibrate it?

6. **Is "only UP" direction correct?**  
   Current code skips all DOWN signals. Is there a structural reason Polymarket YES prices lag more on upward moves? Or is this an untested assumption?

### On live readiness

7. **What additional risk controls are missing before micro live?**  
   Current gaps: no durable trade log mid-session, no global concurrent position cap, no Polymarket execution layer.

8. **What does a minimal Polymarket CLOB execution layer require?**  
   For context: EIP-712 signing, USDC on Polygon, Polymarket CLOB POST /order. What are the key failure modes to guard against?

---

*This document is for external review only. No trading decisions should be based solely on this summary.*
