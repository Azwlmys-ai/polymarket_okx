# Paper Anchor Executor Report

> Generated: 2026-05-22T15:37:44Z  |  Strategy: T+180s  |  Anchor correction: −76.75 USD
> Source: paper_anchor_signals.jsonl  |  Resolved windows: 443
> **NO REAL ORDERS PLACED. PAPER MODE ONLY.**

## 1. Threshold Comparison

| Metric | dist ≥ $150 (obs) | dist ≥ $120 (**default**) | dist ≥ $100 (baseline) |
|--------|------------------|--------------------------|----------------------|
| dist ≥ $150 obs | 27 | 100.0% | +0.4519 | +12.2016 | 0.000 | 0 |
| dist ≥ $120 default | 66 | 97.0% | +0.4240 | +27.9832 | 0.544 | 1 |
| dist ≥ $100 baseline | 119 | 89.9% | +0.3540 | +42.1260 | 1.061 | 2 |

| Metric | dist ≥ $150 | dist ≥ $120 | dist ≥ $100 |
|--------|------------|------------|------------|
| N trades | 27 | 66 | 119 |
| Win rate | 100.0% | 97.0% | 89.9% |
| Mean PnL | +0.4519 | +0.4240 | +0.3540 |
| Median PnL | +0.4557 | +0.4557 | +0.4557 |
| Cumulative PnL | +12.2016 | +27.9832 | +42.1260 |
| Max drawdown | 0.000 | 0.544 | 1.061 |
| Longest loss run | 0 | 1 | 2 |
| Skipped | 0 | 365 | 312 |

## 2. Sample Coverage vs Targets

| Threshold | Signals | Target | Met? |
|-----------|---------|--------|------|
| dist ≥ $100 | 119 | 200 | ❌ need 81 more |
| dist ≥ $120 | 66 | 100 | ❌ need 34 more |
| dist ≥ $150 | 27 | 50  | ❌ need 23 more |

## 3. Last 20 Executed Trades (dist ≥ $120)

| # | Dir | Dist | Spread | Entry | Outcome | PnL |
|---|-----|------|--------|-------|---------|-----|
| 1 | UP | $121 | 0.01 | 0.510 | UP ✅ | +0.4557 |
| 2 | UP | $127 | 0.01 | 0.510 | UP ✅ | +0.4557 |
| 3 | UP | $130 | 0.01 | 0.510 | UP ✅ | +0.4557 |
| 4 | UP | $155 | 0.01 | 0.550 | UP ✅ | +0.4185 |
| 5 | UP | $176 | 0.01 | 0.510 | UP ✅ | +0.4557 |
| 6 | UP | $123 | 0.01 | 0.510 | UP ✅ | +0.4557 |
| 7 | UP | $128 | 0.01 | 0.510 | UP ✅ | +0.4557 |
| 8 | UP | $149 | 0.01 | 0.510 | UP ✅ | +0.4557 |
| 9 | UP | $164 | 0.01 | 0.510 | UP ✅ | +0.4557 |
| 10 | UP | $132 | 0.01 | 0.510 | UP ✅ | +0.4557 |
| 11 | UP | $203 | 0.01 | 0.510 | UP ✅ | +0.4557 |
| 12 | UP | $137 | 0.01 | 0.510 | UP ✅ | +0.4557 |
| 13 | UP | $160 | 0.01 | 0.510 | UP ✅ | +0.4557 |
| 14 | UP | $166 | 0.01 | 0.510 | UP ✅ | +0.4557 |
| 15 | UP | $135 | 0.01 | 0.510 | UP ✅ | +0.4557 |
| 16 | UP | $152 | 0.01 | 0.520 | UP ✅ | +0.4464 |
| 17 | UP | $127 | 0.01 | 0.520 | UP ✅ | +0.4464 |
| 18 | UP | $180 | 0.01 | 0.520 | UP ✅ | +0.4464 |
| 19 | UP | $139 | 0.01 | 0.520 | UP ✅ | +0.4464 |
| 20 | UP | $247 | 0.01 | 0.510 | UP ✅ | +0.4557 |

## 4. Status and Conclusions

| Item | Status |
|------|--------|
| T+90s | ❌ Blocked from execution path |
| T+120s | ❌ Blocked from execution path |
| T+180s (all) | ❌ Too wide distribution |
| T+180s / dist ≥ $120 | ✅ Active default |
| T+180s / dist ≥ $100 | Baseline reference |
| Real orders | ❌ PROHIBITED — paper mode only |
| Live runner | ❌ mvp_runner.py NOT modified |

**dist ≥ $120 meets win-rate threshold.** Pending: ≥100 trade sample and BTC up-regime coverage before live consideration.

---
*Paper execution prototype. No wallet. No real orders. Read-only data source.*