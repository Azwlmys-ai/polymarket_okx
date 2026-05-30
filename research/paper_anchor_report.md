# Paper Anchor Simulation Report

> Generated: 2026-05-25T01:25:37Z
> Strategy: anchor_est = Binance_T_open − rolling_mean (current=89.46, N=100, window=100)
> Signal threshold: $40.0
> Check offsets: T+90s / T+120s / T+180s
> Fee: 7% taker, break-even = 53.5%

## 1. Dataset

| | Value |
|---|---|
| Total windows recorded | 706 |
| Resolved windows | 701 |
| Total checkpoints with signal | 1793 |
| Signals with resolvable PnL | 1793 |
| Unique windows with ≥1 signal | 656 |
| Time range | 2026-05-21 02:15 → 2026-05-23 21:40 UTC |

### JSONL Field Coverage

| Field | Status |
|-------|--------|
| market slug | available (slug) |
| market title | N/A |
| market category | N/A |
| entry time | available (checkpoint ts_utc or event_start_ts + offset_s) |
| exit time / hold duration | available (resolved_ts or end_ts) |
| market close / expiry / end time | available (end_ts) |
| dist | available (checkpoint distance) |
| fee-adjusted PnL | available (computed from outcome and Poly price fields) |

## 2. Paper Trading Results (All Signals)

| Metric | Value |
|--------|-------|
| Total triggered signals | 1793 |
| Resolved windows | 701 |
| Wins (direction correct) | 1014 |
| Win rate | 56.6% |
| Mean fee-adj PnL/signal | +0.0204 |
| Median fee-adj PnL | +0.4464 |
| Cumulative PnL | +36.5263 |
| Max drawdown | 36.3851 |
| Best trade | +0.5208 |
| Worst trade | -1.0000 |
| StdDev PnL | 0.4969 |
| Break-even PnL threshold | -0.0350 (fee at 0.50) |

## 3. Per-Offset Breakdown

| Offset | N | Win Rate | Mean PnL | Median PnL | Tradeable |
|--------|---|---------|---------|-----------|-----------|
| T+90s | 620 | 53.7% | -0.0084 | +0.4464 | 614/620 |
| T+120s | 600 | 56.2% | +0.0163 | +0.4464 | 594/600 |
| T+180s | 573 | 60.0% | +0.0558 | +0.4464 | 569/573 |

## 4. CLOB Tradability

| Metric | Value |
|--------|-------|
| Signals with spread data | 1791 |
| Spread ≤ 0.03 | 1777/1791 (99.2%) |
| Mean spread | 0.012 |
| Median spread | 0.010 |
| Mean CLOB liquidity | $13,430 |
| Min CLOB liquidity | $4,930 |
| Tradeable signals (spread ≤ 0.03) | 1777 |
| Tradeable win rate | 56.6% |
| Tradeable mean PnL | +0.0210 |

## 5. Distance Threshold Performance

Distance = |BTC_live − anchor_est| at checkpoint time.

| Threshold | N | Win Rate | Mean PnL | Median PnL | Sum PnL | Avg Win | Avg Loss | Profit Factor | Max Drawdown |
|-----------|---|----------|----------|------------|---------|---------|----------|---------------|--------------|
| ≥ $40 | 1793 | 56.6% | +0.0204 | +0.4464 | +36.5263 | +0.4555 | -0.5460 | 1.09 | 36.3851 |
| ≥ $60 | 1473 | 62.4% | +0.0792 | +0.4557 | +116.6137 | +0.4553 | -0.5447 | 1.39 | 10.3569 |
| ≥ $80 | 1020 | 72.4% | +0.1785 | +0.4557 | +182.0700 | +0.4548 | -0.5445 | 2.19 | 7.5830 |
| ≥ $100 | 621 | 81.8% | +0.2729 | +0.4557 | +169.4875 | +0.4546 | -0.5437 | 3.76 | 3.2658 |
| ≥ $110 | 443 | 86.0% | +0.3150 | +0.4557 | +139.5496 | +0.4547 | -0.5433 | 5.14 | 2.7215 |
| ≥ $120 | 331 | 90.3% | +0.3583 | +0.4557 | +118.6135 | +0.4548 | -0.5428 | 7.83 | 1.6329 |
| ≥ $130 | 240 | 93.8% | +0.3926 | +0.4557 | +94.2192 | +0.4550 | -0.5431 | 12.57 | 1.6329 |
| ≥ $150 | 134 | 97.8% | +0.4325 | +0.4557 | +57.9522 | +0.4548 | -0.5412 | 36.69 | 0.5443 |
| ≥ $200 | 46 | 100.0% | +0.4573 | +0.4557 | +21.0366 | +0.4573 | N/A | inf | 0.0000 |

Max Drawdown in this section is the largest peak-to-trough drop on the running cumulative fee-adjusted PnL curve.

### Potential Live Threshold Simulation

| Threshold | N | Signal Share | Est Signals/Day | Est Signals/Year | WR | Mean PnL | Median PnL | Sum PnL | Profit Factor | Max Drawdown | Recovery Factor |
|-----------|---|--------------|-----------------|------------------|----|----------|------------|---------|---------------|--------------|-----------------|
| ≥ $100 | 621 | 34.6% | 220.80 | 80592 | 81.8% | +0.2729 | +0.4557 | +169.4875 | 3.76 | 3.2658 | 51.90 |
| ≥ $110 | 443 | 24.7% | 157.51 | 57492 | 86.0% | +0.3150 | +0.4557 | +139.5496 | 5.14 | 2.7215 | 51.28 |
| ≥ $120 | 331 | 18.5% | 117.69 | 42956 | 90.3% | +0.3583 | +0.4557 | +118.6135 | 7.83 | 1.6329 | 72.64 |
| ≥ $130 | 240 | 13.4% | 85.33 | 31147 | 93.8% | +0.3926 | +0.4557 | +94.2192 | 12.57 | 1.6329 | 57.70 |
| ≥ $150 | 134 | 7.5% | 47.64 | 17390 | 97.8% | +0.4325 | +0.4557 | +57.9522 | 36.69 | 0.5443 | 106.47 |

### High Distance Hold Time and PnL Distribution

| Threshold | Avg Hold Hours | Median Hold Hours | Hold >72h Share | PnL p25 | PnL p50 | PnL p75 | PnL p90 |
|-----------|----------------|-------------------|-----------------|---------|---------|---------|---------|
| ≥ $100 | 0.14 | 0.14 | 0.0% | +0.4464 | +0.4557 | +0.4557 | +0.4557 |
| ≥ $110 | 0.14 | 0.14 | 0.0% | +0.4464 | +0.4557 | +0.4557 | +0.4650 |
| ≥ $120 | 0.14 | 0.14 | 0.0% | +0.4464 | +0.4557 | +0.4557 | +0.4650 |
| ≥ $130 | 0.14 | 0.13 | 0.0% | +0.4464 | +0.4557 | +0.4557 | +0.4650 |
| ≥ $150 | 0.14 | 0.13 | 0.0% | +0.4557 | +0.4557 | +0.4557 | +0.4650 |

### Remaining Time Analysis

Remaining time = market end time minus checkpoint entry time.

#### dist ≥ $100

| Remaining Time | N | Share | WR | Mean PnL | Median PnL | Profit Factor | Max Drawdown |
|----------------|---|-------|----|----------|------------|---------------|--------------|
| <1h | 621 | 100.0% | 81.8% | +0.2729 | +0.4557 | 3.76 | 3.2658 |
| 1h-6h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| 6h-12h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| 12h-24h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| 24h-48h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| 48h-72h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| >72h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |

#### dist ≥ $120

| Remaining Time | N | Share | WR | Mean PnL | Median PnL | Profit Factor | Max Drawdown |
|----------------|---|-------|----|----------|------------|---------------|--------------|
| <1h | 331 | 100.0% | 90.3% | +0.3583 | +0.4557 | 7.83 | 1.6329 |
| 1h-6h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| 6h-12h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| 12h-24h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| 24h-48h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| 48h-72h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| >72h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |

#### dist ≥ $130

| Remaining Time | N | Share | WR | Mean PnL | Median PnL | Profit Factor | Max Drawdown |
|----------------|---|-------|----|----------|------------|---------------|--------------|
| <1h | 240 | 100.0% | 93.8% | +0.3926 | +0.4557 | 12.57 | 1.6329 |
| 1h-6h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| 6h-12h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| 12h-24h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| 24h-48h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| 48h-72h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| >72h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |

#### dist ≥ $150

| Remaining Time | N | Share | WR | Mean PnL | Median PnL | Profit Factor | Max Drawdown |
|----------------|---|-------|----|----------|------------|---------------|--------------|
| <1h | 134 | 100.0% | 97.8% | +0.4325 | +0.4557 | 36.69 | 0.5443 |
| 1h-6h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| 6h-12h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| 12h-24h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| 24h-48h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| 48h-72h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |
| >72h | 0 | 0.0% | N/A | N/A | N/A | N/A | N/A |

### Hold Duration Analysis

| Threshold | Avg Hours | Median Hours | p25 Hours | p75 Hours | p90 Hours | Hold <5m | Hold 5m-15m | Hold 15m-1h | Hold >1h | PnL/Hold Pearson r |
|-----------|-----------|--------------|-----------|-----------|-----------|----------|-------------|-------------|----------|--------------------|
| ≥ $100 | 0.14 | 0.14 | 0.12 | 0.17 | 0.18 | 2.9% | 97.1% | 0.0% | 0.0% | -0.0341 |
| ≥ $120 | 0.14 | 0.14 | 0.11 | 0.16 | 0.18 | 3.9% | 96.1% | 0.0% | 0.0% | -0.0923 |
| ≥ $130 | 0.14 | 0.13 | 0.11 | 0.16 | 0.18 | 3.3% | 96.7% | 0.0% | 0.0% | -0.0695 |
| ≥ $150 | 0.14 | 0.13 | 0.11 | 0.16 | 0.18 | 3.7% | 96.3% | 0.0% | 0.0% | -0.0629 |

### Near-expiry Risk Summary

| Threshold | Remaining <6h Share | Remaining <24h Share | Risk Note |
|-----------|----------------------|-----------------------|-----------|
| ≥ $120 | 100.0% | 100.0% | WARNING: edge may be near-expiry dominated; CAUTION: live execution may be sensitive to latency/slippage |
| ≥ $130 | 100.0% | 100.0% | WARNING: edge may be near-expiry dominated; CAUTION: live execution may be sensitive to latency/slippage |
| ≥ $150 | 100.0% | 100.0% | WARNING: edge may be near-expiry dominated; CAUTION: live execution may be sensitive to latency/slippage |

### Event Type / Market Group

N/A - current JSONL does not include market title or category fields.

### Distance/PnL Correlation

| Metric | Value |
|--------|-------|
| Pearson r | 0.3818 |
| N | 1793 |

### Distance Bucket Distribution

| Bucket | N | Win Rate | Mean PnL | Median PnL | Sum PnL | Avg Win | Avg Loss | Profit Factor | Max Drawdown |
|--------|---|----------|----------|------------|---------|---------|----------|---------------|--------------|
| 40-60 | 320 | 29.7% | -0.2503 | -0.5443 | -80.0874 | +0.4574 | -0.5491 | 0.35 | 81.7095 |
| 60-80 | 453 | 40.0% | -0.1445 | -0.5443 | -65.4563 | +0.4573 | -0.5450 | 0.56 | 66.3114 |
| 80-100 | 399 | 57.6% | +0.0315 | +0.4464 | +12.5825 | +0.4552 | -0.5450 | 1.14 | 10.3861 |
| 100-120 | 290 | 72.1% | +0.1754 | +0.4557 | +50.8740 | +0.4543 | -0.5441 | 2.15 | 3.2844 |
| 120-150 | 197 | 85.3% | +0.3079 | +0.4557 | +60.6613 | +0.4548 | -0.5430 | 4.85 | 1.6329 |
| 150-200 | 88 | 96.6% | +0.4195 | +0.4557 | +36.9156 | +0.4534 | -0.5412 | 23.74 | 0.5443 |
| >=200 | 46 | 100.0% | +0.4573 | +0.4557 | +21.0366 | +0.4573 | N/A | inf | 0.0000 |

## 6. Anchor Proxy Quality (Post-hoc)

| Statistic | Value |
|-----------|-------|
| N anchors measured | 665 |
| Mean (Binance_T_open − priceToBeat) | +81.54 USD |
| Median | +81.38 USD |
| StdDev | 8.07 USD |
| Active rolling correction | 89.46 USD (N=100) |
| All-time mean delta | +81.54 USD |

## 7. GO / NO-GO

### Verdict: **❌ NO-GO** (insufficient data or edge not confirmed)

| Criterion | Value | Pass? |
|-----------|-------|-------|
| Windows resolved ≥ 50 (full run ≥ 200) | 701 | ✅ |
| Triggered signals ≥ 30 | 1793 | ✅ |
| Win rate ≥ 75% | 56.6% | ❌ |
| Fee-adj mean PnL > 0 | +0.0204 | ✅ |
| Median PnL > 0 | +0.4464 | ✅ |
| CLOB tradeable spread (≥ 50% signals) | 99% | ✅ |

## 8. Recent Signals (last 20)

| Window | Offset | BTC | Anchor | Dist | Dir | Outcome | PnL |
|--------|--------|-----|--------|------|-----|---------|-----|
| 1779569700 | T+120 | 77127 | 76803 | 325 | UP | UP ✅ | +0.456 |
| 1779569700 | T+180 | 77178 | 76803 | 375 | UP | UP ✅ | +0.474 |
| 1779570000 | T+90 | 77300 | 77198 | 101 | UP | DOWN ❌ | -0.526 |
| 1779570000 | T+120 | 77317 | 77198 | 119 | UP | DOWN ❌ | -0.526 |
| 1779570000 | T+180 | 77124 | 77198 | 74 | DOWN | DOWN ✅ | +0.437 |
| 1779570300 | T+120 | 77010 | 76931 | 79 | UP | UP ✅ | +0.521 |
| 1779570300 | T+180 | 77154 | 76931 | 223 | UP | UP ✅ | +0.521 |
| 1779570600 | T+90 | 77055 | 76992 | 64 | UP | DOWN ❌ | -0.526 |
| 1779570600 | T+120 | 77056 | 76992 | 64 | UP | DOWN ❌ | -0.526 |
| 1779570900 | T+180 | 76934 | 76891 | 43 | UP | DOWN ❌ | -0.526 |
| 1779571200 | T+90 | 77080 | 76900 | 180 | UP | UP ✅ | +0.474 |
| 1779571200 | T+120 | 77090 | 76900 | 190 | UP | UP ✅ | +0.474 |
| 1779571200 | T+180 | 77065 | 76900 | 165 | UP | UP ✅ | +0.474 |
| 1779571800 | T+120 | 76720 | 76664 | 56 | UP | UP ✅ | +0.474 |
| 1779571800 | T+180 | 76720 | 76664 | 56 | UP | UP ✅ | +0.474 |
| 1779572100 | T+90 | 76878 | 76745 | 134 | UP | DOWN ❌ | -0.544 |
| 1779572100 | T+120 | 76900 | 76745 | 155 | UP | DOWN ❌ | -0.544 |
| 1779572100 | T+180 | 76870 | 76745 | 126 | UP | DOWN ❌ | -0.544 |
| 1779572400 | T+120 | 76567 | 76631 | 64 | DOWN | DOWN ✅ | +0.474 |
| 1779572400 | T+180 | 76673 | 76631 | 43 | UP | DOWN ❌ | -0.554 |

---
*Paper simulation only. No real trades. No wallet access.*

Max Drawdown is calculated from the running cumulative fee-adjusted PnL curve.