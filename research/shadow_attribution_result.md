# Shadow PnL Attribution Result

> Generated: 2026-05-29T14:20:47Z  
> Events file: 956 records  
> Read-only analysis. No orders, no VPS changes.

---

## 1. Event Breakdown

| Category | Count | % of total |
|----------|-------|-----------|
| Total events | 956 | 100% |
| Real CLOB (fallback=False, pnl computed) | 906 | 94.8% |
| Fallback (poly_ask used as fill) | 50 | 5.2% |
| Unresolved (paper_exit_price=None) | 0 | 0.0% |

## 2. Real-CLOB Attribution (primary result)

### Key Numbers

| Metric | Value |
|--------|-------|
| Real-CLOB events (n) | 906 |
| **Mean shadow_adjusted_pnl_10** | **-0.0014** |
| Sum shadow PnL | -1.2588 |
| Shadow win rate | 0.806 (80.6%) |
| Paper win rate (same events) | 0.806 (80.6%) |
| Mean fill price (CLOB ask) | 0.7926 |
| Mean paper entry price | 0.5100 |
| Mean price degradation (fill - paper) | +0.2826 |
| Break-even fill @ actual win rate | 0.791 |
| Mean degradation vs paper PnL | +0.2628 |

### Verdict: 🔴 **NO-GO** — Mean shadow PnL ≤ 0. Strategy edge does not survive real CLOB fills.

## 3. Fill Price Distribution (real CLOB)

| Stat | Value |
|------|-------|
| n | 906 |
| mean | 0.7926 |
| sum | 718.1063 |
| min | 0.3600 |
| max | 0.9900 |
| p25 | 0.7000 |
| p50 | 0.8149 |
| p75 | 0.9100 |

| Fill bucket | Count | EV at that price |
|-------------|-------|-----------------|
| <0.80 | 406 | +0.0382 |
| 0.80–0.89 | 251 | -0.0455 |
| 0.90–0.93 | 89 | -0.1152 |
| 0.94 | 33 | -0.1385 |
| 0.95–0.97 | 77 | -0.1571 |
| 0.98–0.99 | 50 | -0.1803 |

## 4. Breakdown by Checkpoint

| Checkpoint | n | Win rate | Mean fill | Mean shadow PnL | Verdict |
|------------|---|----------|-----------|-----------------|---------|
| T+120 | 296 | 79.1% | 0.785 | -0.0092 | ❌ |
| T+180 | 325 | 85.8% | 0.837 | +0.0098 | ✅ |
| T+90 | 285 | 76.1% | 0.750 | -0.0060 | ❌ |

## 5. Breakdown by Distance Bucket

| Dist bucket | n | Win rate | Mean fill | Mean shadow PnL | Verdict |
|-------------|---|----------|-----------|-----------------|---------|
| dist 130–149 | 401 | 73.6% | 0.710 | +0.0051 | ✅ |
| dist 150–179 | 284 | 82.4% | 0.818 | -0.0068 | ❌ |
| dist 180–219 | 162 | 90.7% | 0.895 | +0.0049 | ⚠️ |
| dist 220+ | 59 | 91.5% | 0.949 | -0.0372 | ❌ |

## 6. Fallback Events (non-attributable)

Fallback events: 50. These used poly_ask (~0.51) as fill — not attributable.
Paper win rate on fallback events: 86.0% (for comparison only)

⚠️ Fallback win rate (86.0%) differs from real-CLOB win rate (80.6%) by >5pp — selection bias risk. Monitor.

## 7. Hard Gate Status for Live Consideration

| Gate | Threshold | Actual | Status |
|------|-----------|--------|--------|
| G1: Mean shadow PnL | >+0.020 | -0.0014 | ❌ |
| G2: Real-CLOB event count | ≥300 | 906 | ✅ |
| G3: Fallback rate | <20% | 5.2% | ✅ |
| G4: Real-CLOB win rate | ≥90% | 80.6% | ❌ |

**Gates passed: 2/4**

🔴 GO conditions not met. Do not proceed to live trading.

---
*Attribution is read-only. No code was changed. No orders were placed.*