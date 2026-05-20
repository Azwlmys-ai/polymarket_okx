# Daily Research Report — Polymarket Microstructure Alpha

> **STATS_ONLY / PAPER_SIM — No real orders. No capital at risk.**

**Generated:** 2026-05-20T20:09:22Z  
**Session Duration:** 2.0h  
**Total Signals:** 115  
**News Events:** 0  

---

## 1. Executive Summary

| Experiment | N | Win Rate | Mean | Median | Expectancy | Fee-Adj PnL |
|---|---|---|---|---|---|---|
| **poly_price_lag** | 97 | 40.4494% | -0.0093% | -0.0072% | -0.0093% | -187.7233% |
| **settlement_reversion** | 18 | 55.5556% | -0.0104% | 0.0033% | -0.0104% | -37.9876% |

### ❌ No Consistent Edge Detected

Continue data collection. Increase sample size. Adjust detection thresholds if needed.

---

## 2. Regime Breakdown

### Market Phase Distribution
| Phase | Count | Pct |
|---|---|
| early | 0 | 0.0% |
| mid | 0 | 0.0% |
| late | 0 | 0.0% |
| settlement | 115 | 100.0% |

### Volatility Regime Distribution
| Regime | Count | Pct |
|---|---|
| low | 115 | 100.0% |
| medium | 0 | 0.0% |
| high | 0 | 0.0% |

---

## 3. Experiment A: Settlement Reversion

Analysis of BTC mean reversion following Polymarket settlement when YES price remains near 50±5 while BTC has moved directionally.

| Horizon | N | Win Rate | Mean Return | Median | Expectancy |
|---|---|---|---|---|---|
| 15s | 18 | 44.4444% | -0.0076% | -0.0037% | -0.0076% |
| 30s | 18 | 50.0000% | -0.0029% | -0.0010% | -0.0029% |
| 60s | 18 | 55.5556% | -0.0104% | 0.0033% | -0.0104% |

---

## 7. Condition Ranking

### poly_price_lag

**Top Conditions:**

| Phase | Regime | N | Win Rate | Mean | Expectancy |
|---|---|---|---|---|---|
| settlement | low | 89 | 40.4494% | -0.0093% | -0.0093% |

### settlement_reversion

**Top Conditions:**

| Phase | Regime | N | Win Rate | Mean | Expectancy |
|---|---|---|---|---|---|
| settlement | low | 18 | 55.5556% | -0.0104% | -0.0104% |

---

## 8. GO / NO-GO Gate

### Verdict: **NO-GO**

**Reason:** Failed criteria: c2_expectancy_gt_0_after_fee, c3_median_gt_0_after_fee, c4_win_rate_vs_baseline, c5_min_2_sessions

| Criterion | Result | Value | Threshold |
|---|---|---|---|
| c1_signal_count_ge_50 | ✅ | 97 | 50 |
| c2_expectancy_gt_0_after_fee | ❌ | -0.021093 | 0 |
| c3_median_gt_0_after_fee | ❌ | -0.021072 | 0 |
| c4_win_rate_vs_baseline | ❌ | 0.404494 | 0.6239130434782609 |
| c6_max_drawdown_ok | ✅ | 0.009197 | < 0.10 (10%) |
| c7_not_concentrated | ✅ | 21/115 | ≤ 70% |

### Randomized Baseline (bootstrap)

- Mean win rate: 49.6087%
- P95 win rate: **57.3913%**
- Mean expectancy: -0.000004
- Pool: 107 returns | bootstrap (n=115, pool=107, iter=500)

> ⚠️ **Multi-session**: ≥2 independent sessions required before paper execution.

---

## 9. Conclusion

_This report is auto-generated for alpha research purposes only. No real orders were placed. All statistics are computed from paper-simulated signal tracking._
