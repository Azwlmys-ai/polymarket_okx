# Anchor Proxy Validation Report

> Generated: 2026-05-21T01:47:35Z
> Sample: 100 resolved BTC 5m Polymarket markets
> Oracle: Chainlink BTC/USD Data Streams
> Proxy candidates: Binance BTCUSDT 1m OHLC

## 1. Dataset

| | Value |
|---|---|
| Total markets | 100 |
| UP outcomes | 59 (59.0%) |
| DOWN outcomes | 41 (41.0%) |
| Time range | 2026-05-20 17:05 → 2026-05-21 01:20 UTC |
| Avg BTC price (Chainlink anchor) | $77,507 |

## 2. Proxy Rankings (sorted by StdDev, lower = better)

**Key**: Systematic bias (mean Δ) is correctable by subtracting it. Variance (StdDev) is NOT correctable — it defines the residual error after correction.
→ **StdDev is the correct ranking metric for direction trading**, not MAE.

**Direction A**: sign(final − proxy) == sign(final − true_anchor)
  → 'If I use this Binance price directly as the anchor, is my direction correct?'

| Proxy | N | Mean Δ (USD) | Median Δ | StdDev | MAE | RMSE | Dir-A |
|-------|---|------------|---------|--------|-----|------|-------|
| `T_open` | 100 | +76.75 | +76.61 | 4.14 | 76.75 | 76.86 | 51.0% |
| `avg_Tm1c_Topen` | 100 | +76.74 | +76.61 | 4.15 | 76.74 | 76.86 | 51.0% |
| `Tm1_close` | 100 | +76.74 | +76.61 | 4.15 | 76.74 | 76.85 | 51.0% |
| `Tm1_typical` | 100 | +75.25 | +75.93 | 10.11 | 75.25 | 75.92 | 51.0% |
| `avg_Tm1mid_Tmid` | 100 | +74.97 | +74.47 | 10.44 | 74.97 | 75.68 | 53.0% |
| `Tm1_mid` | 100 | +74.51 | +75.32 | 14.61 | 74.51 | 75.92 | 53.0% |
| `T_mid` | 100 | +75.43 | +75.14 | 15.35 | 75.43 | 76.96 | 51.0% |
| `T_high` | 100 | +90.52 | +84.36 | 16.97 | 90.52 | 92.08 | 46.0% |
| `T_typical` | 100 | +75.42 | +75.90 | 18.22 | 75.42 | 77.57 | 51.0% |
| `T_low` | 100 | +60.33 | +65.81 | 19.96 | 60.78 | 63.52 | 55.0% |
| `T_close` | 100 | +75.41 | +76.68 | 25.64 | 75.41 | 79.61 | 50.0% |
| `Tm2_close` | 100 | +70.62 | +73.08 | 27.95 | 71.46 | 75.89 | 55.0% |
| `Tm1_open` | 100 | +70.62 | +73.09 | 27.95 | 71.46 | 75.89 | 55.0% |

## 3. Best Proxy Detail: `T_open`

| Statistic | Value | Interpretation |
|-----------|-------|----------------|
| N | 100 | |
| Mean Δ (proxy − anchor) | +76.75 USD | Systematic bias |
| Median Δ | +76.61 USD | |
| StdDev of Δ | 4.14 USD | Random noise after bias correction |
| MAE | 76.75 USD | Average absolute error |
| RMSE | 76.86 USD | |
| Corr (proxy vs anchor) | 0.999706 | |
| Dir-A accuracy | 51.00% | Using proxy directly as anchor |

**Corrected proxy** (subtract mean Δ = +76.75 USD):
- Corrected anchor estimate = `T_open` − 76.75
- Dir-B accuracy (corrected anchor vs true anchor): **96.00%**
- Residual std after correction: **4.14 USD**

**Theoretical Dir-B** (Monte Carlo, N=200k, σ_chainlink=59.0):
- With σ_residual=4.14: **Dir-B = 97.74%**
- Interpretation: after subtracting mean bias, 97.7% of direction bets are correct (static, at T+0)
- In live trading at T+K with known BTC drift, accuracy is HIGHER (as drift increases, uncertainty shrinks)

**Theoretical edge by BTC drift at T+120s** (σ_remaining = σ_chainlink × √(180/300)):

| BTC above est. anchor | P(UP) | Fee break-even | Edge |
|----------------------|-------|----------------|------|
| +$20 | 66.9% | 53.5% | +13.4% ✅ |
| +$40 | 80.9% | 53.5% | +27.4% ✅ |
| +$60 | 90.5% | 53.5% | +37.0% ✅ |
| +$100 | 98.6% | 53.5% | +45.1% ✅ |
| +$150 | 99.9% | 53.5% | +46.4% ✅ |

## 4. Binance 5-Minute Return Direction vs Chainlink Outcome

Tests: does sign(Binance_close[T+5min] − Binance_open[T]) == Polymarket settlement direction?

| N | Direction Agreement |
|---|---|
| 100 | **96.00%** |

> ✅ HIGH: Binance 5min return direction strongly agrees with Chainlink settlement.

## 5. Corrected Proxy: Direction Accuracy by Residual Error Threshold

Threshold = residual error |corrected_proxy − true_anchor| must EXCEED threshold.
(Low threshold = low confidence; High threshold = high confidence, fewer signals.)

| Min Residual (USD) | N signals | Trigger Rate | Direction Accuracy |
|-------------------|-----------|--------------|-------------------|
| ≥$0 | 100 | 100.0% | 96.0% |
| ≥$5 | 18 | 18.0% | 94.4% |
| ≥$10 | 2 | 2.0% | 100.0% |
| ≥$15 | 1 | 1.0% | 100.0% |
| ≥$20 | 0 | 0.0% | N/A |
| ≥$30 | 0 | 0.0% | N/A |
| ≥$50 | 0 | 0.0% | N/A |

## 6. Fee Structure and Edge Requirements

| Parameter | Value |
|-----------|-------|
| Taker fee rate | 7% of (1 − price) |
| At price = 0.50: fee/unit | 0.0350 (3.50¢) |
| Break-even probability | 0.5350 (53.50%) |
| BTC 5min 1σ (est. 0.15%) | $116 |
| BTC move for 55% confidence | ~$15 |
| BTC move for 60% confidence | ~$29 |

## 7. GO / NO-GO Assessment

### Verdict: **✅ PROXY VALIDATED — trading blocked (read-only)**

| Criterion | Value | Pass? |
|-----------|-------|-------|
| Bias stability: StdDev < $20 (correctable) | σ = $4.1 | ✅ |
| Dir-B (corrected proxy) ≥ 80% | 96.0% | ✅ |
| Binance 5min direction ≥ 80% | 96.0% | ✅ |
| Trading infrastructure: CLOB orders (NOT read-only) | ❌ Read-only (no wallet) | ❌ |

**Proxy quality is VALIDATED.** The anchor can be reconstructed with 96% direction accuracy.

**Blocked by**: Trading infrastructure (current project is read-only).
The theoretical edge exists (Dir-B=96%, drift $40 → +27% edge at T+120s).
To capture it requires: CLOB order placement, wallet signing — outside current scope.

## 8. Key Findings

1. **Best proxy**: `T_open` with MAE=76.75 USD, σ=4.14 USD
2. **Systematic bias**: Binance is +76.75 USD vs Chainlink anchor
3. **Bias stability**: σ=4.14 USD — STABLE
4. **Direction (raw proxy)**: 51.0% agreement using proxy directly as anchor
5. **Binance 5min direction**: 96.0% — Binance return matches Chainlink outcome

**Critical gap**: `eventMetadata.priceToBeat` is only readable AFTER window closes (17–21s lag).
During the live window, the anchor is NOT exposed by any Polymarket API field.
The only way to know the live anchor is: Binance proxy OR Chainlink on-chain RPC.

## 9. Next Hypothesis

**H1 (highest priority)**: Apply the measured Binance → Chainlink correction factor in a paper simulation:
  - At each window start T, read `Binance_T_open`
  - Estimate anchor = `Binance_T_open − mean_delta`
  - At T+90s and T+180s (mid-window), compare live BTC to estimated anchor
  - Bet the direction if |BTC − est_anchor| > $40 (fee break-even region)
  - Measure direction accuracy over 200+ windows

**H2**: Chainlink Data Streams uses a specific subset of exchanges. If we know which exchanges (e.g., Coinbase + Kraken + Gemini, NOT Binance), a composite of those exchanges may give near-zero systematic bias.

---
*Read-only research. No trading. No wallet access.*