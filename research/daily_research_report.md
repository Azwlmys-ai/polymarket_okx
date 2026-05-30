# Daily Research Report — Polymarket Microstructure Alpha

> **STATS_ONLY / PAPER_SIM — No real orders. No capital at risk.**

**Generated:** 2026-05-21T00:58:02Z  
**Session Duration:** 300s  
**Total Signals:** 10  
**News Events:** 0  

---

## 1. Executive Summary

| Experiment | N | Win Rate | Mean | Median | Expectancy | Fee-Adj PnL |
|---|---|---|---|---|---|---|
| **poly_price_lag** | 10 | 20.0000% | -0.0134% | -0.0238% | -0.0134% | -10.5671% |

### ❌ No Consistent Edge Detected

Continue data collection. Increase sample size. Adjust detection thresholds if needed.

---

## 2. Regime Breakdown

### Market Phase Distribution
| Phase | Count | Pct |
|---|---|
| early | 0 | 0.0% |
| mid | 0 | 0.0% |
| late | 2 | 20.0% |
| settlement | 8 | 80.0% |

### Volatility Regime Distribution
| Regime | Count | Pct |
|---|---|
| low | 10 | 100.0% |
| medium | 0 | 0.0% |
| high | 0 | 0.0% |

---

## 3. Experiment A: Settlement Reversion

Analysis of BTC mean reversion following Polymarket settlement when YES price remains near 50±5 while BTC has moved directionally.

| Horizon | N | Win Rate | Mean Return | Median | Expectancy |
|---|---|---|---|---|---|
| 15s | 0 | N/A | N/A | N/A | N/A |
| 30s | 0 | N/A | N/A | N/A | N/A |
| 60s | 0 | N/A | N/A | N/A | N/A |

---

## 7. Condition Ranking

### poly_price_lag

**Top Conditions:**

| Phase | Regime | N | Win Rate | Mean | Expectancy |
|---|---|---|---|---|---|
| settlement | low | 3 | 0.0000% | -0.0214% | -0.0214% |

---

## 8. GO / NO-GO Gate

### Verdict: **NO-GO**

**Reason:** Failed criteria: c1_signal_count_ge_50, c2_expectancy_gt_0_after_fee, c3_median_gt_0_after_fee, c4_win_rate_vs_baseline, c5_min_2_sessions, c7_not_concentrated

| Criterion | Result | Value | Threshold |
|---|---|---|---|
| c1_signal_count_ge_50 | ❌ | 10 | 50 |
| c2_expectancy_gt_0_after_fee | ❌ | -0.021134 | 0 |
| c3_median_gt_0_after_fee | ❌ | -0.021238 | 0 |
| c4_win_rate_vs_baseline | ❌ | 0.200000 | 0.05 |
| c6_max_drawdown_ok | ✅ | 0.001153 | < 0.10 (10%) |
| c7_not_concentrated | ❌ | 10/10 | ≤ 70% |

> ⚠️ **Multi-session**: ≥2 independent sessions required before paper execution.

---

## 9. Conclusion

_This report is auto-generated for alpha research purposes only. No real orders were placed. All statistics are computed from paper-simulated signal tracking._
