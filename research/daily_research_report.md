# Daily Research Report — Polymarket Microstructure Alpha

> **STATS_ONLY / PAPER_SIM — No real orders. No capital at risk.**

**Generated:** 2026-05-20T18:07:52Z  
**Session Duration:** 300s  
**Total Signals:** 3  
**News Events:** 0  

---

## 1. Executive Summary

| Experiment | N | Win Rate | Mean | Median | Expectancy | Fee-Adj PnL |
|---|---|---|---|---|---|---|
| **poly_price_lag** | 3 | 100.0000% | 0.0559% | 0.0559% | 0.0559% | -2.0441% |

### ✅ Positive Edge Detected

- **poly_price_lag**: expectancy=0.0559%, win_rate=100.0000%, n=3

---

## 2. Regime Breakdown

### Market Phase Distribution
| Phase | Count | Pct |
|---|---|
| early | 0 | 0.0% |
| mid | 0 | 0.0% |
| late | 2 | 66.7% |
| settlement | 1 | 33.3% |

### Volatility Regime Distribution
| Regime | Count | Pct |
|---|---|
| low | 3 | 100.0% |
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

---

## 8. GO / NO-GO Gate

### Verdict: **NO-GO**

**Reason:** Failed criteria: c1_signal_count_ge_50, c2_expectancy_gt_0_after_fee, c3_median_gt_0_after_fee, c4_win_rate_vs_baseline, c5_min_2_sessions, c7_not_concentrated

| Criterion | Result | Value | Threshold |
|---|---|---|---|
| c1_signal_count_ge_50 | ❌ | 3 | 50 |
| c2_expectancy_gt_0_after_fee | ❌ | -0.020441 | 0 |
| c3_median_gt_0_after_fee | ❌ | -0.020441 | 0 |
| c4_win_rate_vs_baseline | ❌ | 1.000000 | 0.05 |
| c6_max_drawdown_ok | ✅ | 0.000000 | < 0.10 (10%) |
| c7_not_concentrated | ❌ | 3/3 | ≤ 70% |

> ⚠️ **Multi-session**: ≥2 independent sessions required before paper execution.

---

## 9. Conclusion

_This report is auto-generated for alpha research purposes only. No real orders were placed. All statistics are computed from paper-simulated signal tracking._
