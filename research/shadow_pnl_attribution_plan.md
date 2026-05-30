# Shadow PnL Attribution Plan

> Authored: 2026-05-27  
> Status: **RESEARCH ONLY — no code change, no live trading, no VPS modification**  
> Scope: Strictly minimal. One question only.

---

## 0. The Only Question That Matters

> **Does the anchor strategy produce positive EV when entry uses real Polymarket CLOB prices instead of the paper WebSocket quote?**

Everything else is irrelevant until this is answered with a number.

---

## 1. What Has Already Been Verified

| Item | Status | Evidence |
|------|--------|----------|
| Main service stable | ✅ | 23h+ uptime, 0 journal errors, paper_anchor_signal_events writing continuously |
| Paper signal win rate (dist≥130) | ✅ | 94.5% (225/238 checkpoints) — paper_anchor_signals.jsonl |
| Shadow recorder operational | ✅ | 169 events in 24h, 82.2% real CLOB orderbooks fetched |
| 100% CLOB executability | ✅ | All 169 shadow events marked executable at $10/$25/$50 |
| Near-expiry risk | ✅ CLEARED | All signals at 120–210s remaining. Not a "panic last second" play. |
| CLOB latency acceptable | ✅ | p50=343ms, p95=369ms |
| No lookahead bias in attribution | ✅ | Entry signal uses only T+90/120/180 information. Outcome (1.0/0.0) is post-hoc ground truth, not a prediction. |
| SSH security | ✅ | PasswordAuthentication=no, key-only login |

---

## 2. The One Unverified Risk

**The paper entry price (poly_ask ≈ 0.51) is NOT the real CLOB execution price.**

At signal time (T+90 to T+180 into a 300s window, dist≥130), the Polymarket CLOB has already repriced the YES token to reflect the BTC move. The WebSocket-based `poly_ask` used for paper simulation lags the CLOB.

Real CLOB asks observed in VPS shadow events: **0.94 – 0.99**

Fee formula: `pnl = payout − entry − 0.07 × (1 − entry)`

| Fill price | Win PnL | Loss PnL | Break-even win rate |
|------------|---------|----------|---------------------|
| 0.51 (paper) | +0.4557 | −0.5443 | 54.4% |
| 0.90 | +0.0384 | −0.9384 | 96.1%… wait |
| 0.94 | +0.0558 | −0.9442 | **94.1%** |
| 0.95 | +0.0472 | −0.9528 | **95.0%** |
| 0.97 | +0.0299 | −0.9701 | **97.0%** |
| 0.99 | +0.0093 | −0.9907 | **99.1%** |

**Actual paper win rate: 94.54%**  
**Break-even fill price at that win rate: 0.941**

This means:
- At mean fill = 0.94 → EV = **+0.0012 per trade** (near zero, noise-level positive)
- At mean fill = 0.95 → EV = **−0.0081 per trade** (loss)
- At mean fill = 0.97 → EV = **−0.0267 per trade** (clear loss)

The strategy's paper-apparent edge of +0.40/trade collapses to near-zero or negative at realistic CLOB prices. This is the **only unverified risk**.

---

## 3. Data Inventory

### 3a. What We Have Locally

| File | Events | CLOB data | Attribution-ready? |
|------|--------|-----------|-------------------|
| `research/shadow_execution_events.jsonl` | 240 | ❌ ALL fallback (May 21–23, pre-fix) | ❌ No — fill price = paper price |
| `research/paper_anchor_signals.jsonl` | 706 windows | — | ✅ Win rates, outcomes, poly prices |
| `research/paper_anchor_executor_trades.jsonl` | 431 (66 entered) | ❌ paper only | Partial — paper entry ≈ 0.51 only |

### 3b. What Is on VPS (not yet pulled)

| File | Events | CLOB data | Attribution-ready? |
|------|--------|-----------|-------------------|
| `/opt/polymarket_okx/research/shadow_execution_events.jsonl` | 169 (24h report) | ✅ 82.2% real (139 events) | **✅ YES — this is the dataset** |

### 3c. Schema Status for Attribution Fields

| Target field | Source field | Available? | Notes |
|---|---|---|---|
| `entry_yes_price` | `paper_entry_price` | ✅ 100% | Paper uses poly_ask (~0.51) |
| `theoretical_fill_price` | `estimated_fill_price_10` | ✅ 98.3% | Real CLOB ask for $10 order |
| `exit_outcome` | `paper_exit_price` | ✅ 100% | 1.0 (win) or 0.0 (loss) |
| `fee_adjusted_shadow_pnl` | `shadow_adjusted_pnl_10` | ✅ 98.3% | Already computed by recorder |
| `paper_pnl` | `fee_adjusted_paper_pnl` | ✅ 100% | Already computed |
| `degradation` | `degradation_vs_paper_10` | ✅ 98.3% | `paper_pnl - shadow_pnl_10` |
| `hold_duration_s` | `hold_duration_hours × 3600` | ✅ 100% | |
| `win_loss` | `shadow_adjusted_pnl_10 > 0` | ✅ derivable | |
| `slippage_bps` | `slippage_bps_10` | ✅ 98.3% | |
| `executable_size` | `executable_10/25/50` | ✅ 100% | All events executable at all sizes |
| `fallback_flag` | `fallback_used` | ✅ 100% | Separates real-CLOB vs fallback |
| `shadow_adjusted_pnl` | `shadow_adjusted_pnl` | ❌ 100% null | Labeling bug — use `_10` instead |
| `degradation_vs_paper_pnl` | `degradation_vs_paper_pnl` | ❌ 100% null | Same bug — derive from `_10` |

**Bottom line: all required attribution fields exist in `shadow_adjusted_pnl_10` and `degradation_vs_paper_10`. No schema changes needed. Just need VPS data.**

---

## 4. Minimum Implementation Plan

### Step 1 — Pull VPS shadow file (5 minutes, user action)

```bash
scp root@158.247.220.86:/opt/polymarket_okx/research/shadow_execution_events.jsonl \
    ~/polymarket_okx/research/shadow_execution_events_vps_$(date +%Y%m%d).jsonl
```

This is a read-only file pull. No VPS change.

### Step 2 — Run attribution script (already designed, ~30 lines)

Script: `research/run_shadow_attribution.py`

Logic:
1. Load VPS shadow file
2. Split: `real_clob` (fallback_used=False), `fallback` (fallback_used=True)
3. For `real_clob` events:
   - Compute mean/sum `shadow_adjusted_pnl_10`
   - Compute win rate, mean fill price, mean degradation
   - Break down by checkpoint (T+90/T+120/T+180) and dist bucket
4. For `fallback` events: note separately, flag as non-attributable
5. Output: single markdown table with GO/NO-GO metrics

### Step 3 — Interpret the number

The output is one number: **mean `shadow_adjusted_pnl_10` across real-CLOB events.**

- `> +0.02` → Potentially viable. Gather 500+ events before live.
- `+0.005 to +0.02` → Marginal. Win rate and fill price are dangerously correlated. NO-GO until more data.
- `< +0.005 (near zero or negative)` → NO-GO. Strategy edge does not survive real fills.

---

## 5. Lookahead Bias Assessment

**No lookahead bias exists in the attribution.**

- The entry signal fires at T+90/T+120/T+180 using only BTC price, anchor estimate, and poly quote available at that moment.
- The `paper_exit_price` (1.0 or 0.0) is the realized market resolution, recorded after the fact.
- The shadow recorder does not know the outcome when it records the signal — it fills `paper_exit_price` only after the market window closes.
- Attribution is purely backward-looking: "given this entry and this fill price, what would have happened?" That is valid statistical evaluation.

The one subtle risk: **selection bias**. Shadow events only exist for signals with dist≥130 that also happened to have a resolvable CLOB token. If the 17.8% fallback events have systematically different outcomes (e.g., they fire on weirder market conditions), excluding them could bias the result. **Mitigation**: report fallback events separately, check whether their paper win rate differs from real-CLOB events.

---

## 6. What Does NOT Need to Be Done

The following are explicitly out of scope and must not be started:

| Out of scope | Reason |
|---|---|
| Any change to `paper_anchor_sim.py` | Not needed for attribution |
| Any change to `shadow_execution_recorder.py` | Not needed; schema already captures what's needed |
| New systemd service / VPS config change | Operational; not research |
| Multi-asset expansion (ETH, SOL) | Premature; BTC edge not yet validated |
| Alternative entry signals (e.g., earlier T+30) | Premature; current signal not validated |
| Execution layer / CLOB signing | No — only after clear GO verdict with 500+ events |
| "Optimizing" fill threshold or dist cutoff | Curve-fitting on 139 events; premature |
| New monitoring dashboards | Operational; not research |
| Any live order | Hard block |

---

## 7. Hard Gate for GO Decision

**All four of the following must be true before any live consideration:**

| Gate | Threshold | Current status |
|------|-----------|----------------|
| G1: Mean shadow_adjusted_pnl_10 (real-CLOB events) | > +0.02 | **Unknown — need VPS pull** |
| G2: Real-CLOB event count | ≥ 300 (for statistical confidence) | 139 (need ~2 more days of data) |
| G3: Fallback rate | < 20% | 17.8% (borderline — monitor) |
| G4: Win rate on real-CLOB events matches paper | ≥ 90% | **Unknown — need VPS pull** |

**Current status: 0/4 gates cleared.**

---

## 8. Preliminary GO / NO-GO Assessment

### Based on available data only:

**⚠️ CONDITIONAL NO-GO — insufficient evidence for GO, not enough evidence to declare NO-GO.**

Reasoning:
- Paper win rate (94.5%) is barely above the break-even fill price (94.1%) at typical CLOB prices
- The margin is ~0.4 percentage points — within noise of 238 events (σ ≈ 1.5 pp for n=238)
- Real-CLOB `shadow_adjusted_pnl_10` distribution is **unknown** — this is the single missing number
- The VPS has 139 real-CLOB events with `shadow_adjusted_pnl_10` already computed; pulling that file takes 5 minutes

**This is not a strategy-kill finding. It is a data-availability finding.**

The strategy could be positive EV at fill=0.94 (EV=+0.001 per trade, ~$0.01 on $10 notional) or it could be negative. The answer is already sitting in the VPS file. Pull it.

---

## 9. Next Action (Single, Minimal)

**One action required:**

```bash
scp root@158.247.220.86:/opt/polymarket_okx/research/shadow_execution_events.jsonl \
    ~/polymarket_okx/research/shadow_execution_events_vps_$(date +%Y%m%d).jsonl
```

Then run `research/run_shadow_attribution.py` against that file.

**If mean shadow_adjusted_pnl_10 > 0 across real-CLOB events → continue accumulating data.**  
**If mean shadow_adjusted_pnl_10 ≤ 0 → NO-GO. Stop. Do not optimize. Do not tune.**

---

*This document is the research plan. It does not authorize any live trade, VPS change, or scope expansion.*
