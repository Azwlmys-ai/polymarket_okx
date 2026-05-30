# Shadow PnL Attribution Result

> Generated: 2026-05-27T01:13:43Z  
> Events file: 175 records  
> Read-only analysis. No orders, no VPS changes.

---

## 1. Event Breakdown

| Category | Count | % of total |
|----------|-------|-----------|
| Total events | 175 | 100% |
| Real CLOB (fallback=False, pnl computed) | 145 | 82.9% |
| Fallback (poly_ask used as fill) | 30 | 17.1% |
| Unresolved (paper_exit_price=None) | 0 | 0.0% |

## 2. Real-CLOB Attribution (primary result)

### Key Numbers

| Metric | Value |
|--------|-------|
| Real-CLOB events (n) | 145 |
| **Mean shadow_adjusted_pnl_10** | **+0.0455** |
| Sum shadow PnL | +6.5956 |
| Shadow win rate | 0.917 (91.7%) |
| Paper win rate (same events) | 0.917 (91.7%) |
| Mean fill price (CLOB ask) | 0.8621 |
| Mean paper entry price | 0.5097 |
| Mean price degradation (fill - paper) | +0.3524 |
| Break-even fill @ actual win rate | 0.911 |
| Mean degradation vs paper PnL | +0.3277 |

### Verdict: 🟢 **TENTATIVE GO** — Mean shadow PnL positive and meaningful. Gather 300+ events before live.

## 3. Fill Price Distribution (real CLOB)

| Stat | Value |
|------|-------|
| n | 145 |
| mean | 0.8621 |
| sum | 125.0047 |
| min | 0.4800 |
| max | 0.9900 |
| p25 | 0.8000 |
| p50 | 0.8800 |
| p75 | 0.9400 |

| Fill bucket | Count | EV at that price |
|-------------|-------|-----------------|
| <0.80 | 35 | +0.1497 |
| 0.80–0.89 | 46 | +0.0660 |
| 0.90–0.93 | 21 | -0.0037 |
| 0.94 | 7 | -0.0270 |
| 0.95–0.97 | 24 | -0.0456 |
| 0.98–0.99 | 12 | -0.0688 |

## 4. Breakdown by Checkpoint

| Checkpoint | n | Win rate | Mean fill | Mean shadow PnL | Verdict |
|------------|---|----------|-----------|-----------------|---------|
| T+120 | 44 | 90.9% | 0.859 | +0.0400 | ✅ |
| T+180 | 56 | 94.6% | 0.915 | +0.0253 | ✅ |
| T+90 | 45 | 88.9% | 0.799 | +0.0760 | ✅ |

## 5. Breakdown by Distance Bucket

| Dist bucket | n | Win rate | Mean fill | Mean shadow PnL | Verdict |
|-------------|---|----------|-----------|-----------------|---------|
| dist 130–149 | 64 | 82.8% | 0.802 | +0.0119 | ✅ |
| dist 150–179 | 40 | 97.5% | 0.888 | +0.0789 | ✅ |
| dist 180–219 | 29 | 100.0% | 0.918 | +0.0761 | ✅ |
| dist 220+ | 12 | 100.0% | 0.958 | +0.0389 | ✅ |

## 6. Fallback Events (non-attributable)

Fallback events: 30. These used poly_ask (~0.51) as fill — not attributable.
Paper win rate on fallback events: 76.7% (for comparison only)

⚠️ Fallback win rate (76.7%) differs from real-CLOB win rate (91.7%) by >5pp — selection bias risk. Monitor.

## 7. Hard Gate Status for Live Consideration

| Gate | Threshold | Actual | Status |
|------|-----------|--------|--------|
| G1: Mean shadow PnL | >+0.020 | +0.0455 | ✅ |
| G2: Real-CLOB event count | ≥300 | 145 | ❌ |
| G3: Fallback rate | <20% | 17.1% | ✅ |
| G4: Real-CLOB win rate | ≥90% | 91.7% | ✅ |

**Gates passed: 3/4**

🟡 G1 positive but not all gates cleared. Continue accumulating data. Do not go live.

---
*Attribution is read-only. No code was changed. No orders were placed.*