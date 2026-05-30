# Shadow Execution Validation Report

> SHADOW_ONLY=true
> Generated: 2026-05-25T02:37:35Z
> Source: research/paper_anchor_signals.jsonl
> Selected event threshold: dist >= 130
> Simulated order size: 10 USDC
> Max online clob token/book resolves: 50

## 1. Selected Shadow Candidates

| Metric | Value |
|--------|-------|
| SHADOW_ONLY | true |
| Total shadow candidates | 240 |
| real_clob_book_available_rate | 0.0% |
| fallback_used_rate | 100.0% |
| missing_token_count | 240 |
| book_fetch_failed_count | 0 |
| Executable rate | 99.2% |
| Executable 10 USDC | 99.2% |
| Executable 25 USDC | 99.2% |
| Executable 50 USDC | 99.2% |
| Avg estimated slippage | 103.2082 |
| Avg degradation vs paper | +0.0000 |
| Paper mean PnL | +0.3926 |
| Shadow-adjusted mean PnL | +0.4004 |
| Shadow PF | 14.47 |
| Shadow max drawdown | 1.6329 |
| Rejected due to missing liquidity/depth | 240 |

WARNING: real CLOB execution feasibility is not yet validated

## 2. Threshold Summary

| Threshold | N | real_clob_book_available_rate | fallback_used_rate | missing_token_count | book_fetch_failed_count | Exec 10 | Exec 25 | Exec 50 | Avg Slippage bps | Avg Degradation | Paper Mean | Shadow Mean | Shadow PF | Shadow MDD |
|-----------|---|-------------------------------|--------------------|---------------------|-------------------------|---------|---------|---------|------------------|-----------------|------------|-------------|-----------|------------|
| >= 120 | 331 | 0.0% | 100.0% | 331 | 0 | 99.4% | 99.4% | 99.4% | 105.2689 | +0.0000 | +0.3583 | +0.3638 | 8.34 | 1.6329 |
| >= 130 | 240 | 0.0% | 100.0% | 240 | 0 | 99.2% | 99.2% | 99.2% | 103.2082 | +0.0000 | +0.3926 | +0.4004 | 14.47 | 1.6329 |
| >= 150 | 134 | 0.0% | 100.0% | 134 | 0 | 99.3% | 99.3% | 99.3% | 103.5607 | +0.0000 | +0.4325 | +0.4398 | 54.73 | 0.5443 |

## 3. Worst-case Execution Scenario

Liquidity scenarios are conservative stress tests. Extra slippage subtracts 0.01 / 0.02 / 0.05 PnL from each executable shadow trade. Max DD uses the running cumulative fee-adjusted PnL curve.

| Threshold | Scenario | Mean PnL | PF | MDD |
|-----------|----------|----------|----|-----|
| >= 120 | base | +0.3638 | 8.34 | 1.6329 |
| >= 120 | liquidity_70pct | +0.3638 | 8.34 | 1.6329 |
| >= 120 | liquidity_50pct | +0.3638 | 8.34 | 1.6329 |
| >= 120 | liquidity_30pct | +0.3638 | 8.34 | 1.6329 |
| >= 120 | extra_slippage_1c | +0.3538 | 8.01 | 1.6629 |
| >= 120 | extra_slippage_2c | +0.3438 | 7.69 | 1.6929 |
| >= 120 | extra_slippage_5c | +0.3138 | 6.80 | 1.7829 |
| >= 130 | base | +0.4004 | 14.47 | 1.6329 |
| >= 130 | liquidity_70pct | +0.4004 | 14.47 | 1.6329 |
| >= 130 | liquidity_50pct | +0.4004 | 14.47 | 1.6329 |
| >= 130 | liquidity_30pct | +0.4004 | 14.47 | 1.6329 |
| >= 130 | extra_slippage_1c | +0.3904 | 13.89 | 1.6629 |
| >= 130 | extra_slippage_2c | +0.3804 | 13.34 | 1.6929 |
| >= 130 | extra_slippage_5c | +0.3504 | 11.79 | 1.7829 |
| >= 150 | base | +0.4398 | 54.73 | 0.5443 |
| >= 150 | liquidity_70pct | +0.4398 | 54.73 | 0.5443 |
| >= 150 | liquidity_50pct | +0.4398 | 54.73 | 0.5443 |
| >= 150 | liquidity_30pct | +0.4398 | 54.73 | 0.5443 |
| >= 150 | extra_slippage_1c | +0.4298 | 52.56 | 0.5543 |
| >= 150 | extra_slippage_2c | +0.4198 | 50.47 | 0.5643 |
| >= 150 | extra_slippage_5c | +0.3898 | 44.61 | 0.5943 |

## 4. Edge Survival Analysis

| Threshold | Edge Positive After Slippage? | PF > 1.5? | MDD | Degradation/Paper Mean | Notes |
|-----------|-------------------------------|----------|-----|------------------------|-------|
| >= 120 | YES | YES | 1.6329 | 0.0% | edge remains positive; PF > 1.5; WARNING: edge may be near-expiry dominated; extra_slippage_5c PF > 1.5 |
| >= 130 | YES | YES | 1.6329 | 0.0% | edge remains positive; PF > 1.5; WARNING: edge may be near-expiry dominated; extra_slippage_5c PF > 1.5 |
| >= 150 | YES | YES | 0.5443 | 0.0% | edge remains positive; PF > 1.5; WARNING: edge may be near-expiry dominated; extra_slippage_5c PF > 1.5 |

Most stable shadow threshold by this simple score: 150

## 5. Near-expiry Distribution

| Threshold | Remaining <1h | Remaining <6h | Remaining <24h | Risk |
|-----------|---------------|---------------|----------------|------|
| >= 120 | 100.0% | 100.0% | 100.0% | WARNING: edge may be near-expiry dominated |
| >= 130 | 100.0% | 100.0% | 100.0% | WARNING: edge may be near-expiry dominated |
| >= 150 | 100.0% | 100.0% | 100.0% | WARNING: edge may be near-expiry dominated |

## 6. Hold Duration Distribution

| Threshold | Hold <5m | Hold 5m-15m | Hold 15m-1h | Hold >1h |
|-----------|----------|-------------|-------------|----------|
| >= 120 | 3.9% | 96.1% | 0.0% | 0.0% |
| >= 130 | 3.3% | 96.7% | 0.0% | 0.0% |
| >= 150 | 3.7% | 96.3% | 0.0% | 0.0% |

## 7. Missing Field Notes

- SHADOW_ONLY=true for every event.
- No trading API calls are made; analysis uses local JSONL only.
- The recorder resolves missing clobTokenIds from the market slug using read-only Gamma GET calls.
- When token resolution succeeds, this module uses the read-only Polymarket CLOB /book endpoint and records clob_orderbook_available=true/false.
- If token or book lookup fails, fallback_used=true and the event falls back to the checkpoint bid/ask/liquidity estimate.
- Missing or insufficient fields are reported as N/A; the recorder does not invent book levels.
- Future minimal fields for stronger validation: market clobTokenIds, order-book levels at signal time, side-specific depth for target notional, quote fetch latency ms, and executable snapshot timestamp.

## 8. Example Shadow Event

```json
{
  "SHADOW_ONLY": true,
  "ask_levels_count": null,
  "available_depth": 13574.1224,
  "best_ask": 0.51,
  "best_bid": 0.5,
  "bid_levels_count": null,
  "checkpoint_time": "T+120",
  "clob_fetch_latency_ms": null,
  "clob_orderbook_available": false,
  "clob_token_id": null,
  "degradation_vs_paper_10": 0.0,
  "degradation_vs_paper_25": 0.0,
  "degradation_vs_paper_50": 0.0,
  "degradation_vs_paper_pnl": null,
  "direction": "UP",
  "distance": 130.77,
  "estimated_fill_price_10": 0.51,
  "estimated_fill_price_25": 0.51,
  "estimated_fill_price_50": 0.51,
  "estimated_slippage": 0.0050000000000000044,
  "estimated_spread": 0.010000000000000009,
  "estimated_taker_executable_price": 0.51,
  "executable": false,
  "executable_10": true,
  "executable_25": true,
  "executable_50": true,
  "fallback_used": true,
  "fee_adjusted_paper_pnl": 0.4557,
  "fetch_latency_ms": null,
  "hold_duration_hours": 0.14305555555555555,
  "mid": 0.505,
  "paper_entry_price": 0.51,
  "paper_exit_price": 1.0,
  "poly_ask": 0.51,
  "poly_bid": 0.5,
  "poly_mid": 0.505,
  "reject_reason": "missing_clob_token_id",
  "remaining_time_hours": 0.04972222222222222,
  "shadow_adjusted_pnl": null,
  "shadow_adjusted_pnl_10": 0.4557,
  "shadow_adjusted_pnl_25": 0.4557,
  "shadow_adjusted_pnl_50": 0.4557,
  "side": "UP",
  "side_specific_depth_top1": 13574.1224,
  "side_specific_depth_top3": 13574.1224,
  "side_specific_depth_top5": 13574.1224,
  "simulated_order_size": 10.0,
  "slippage_bps_10": 99.0099009900991,
  "slippage_bps_25": 99.0099009900991,
  "slippage_bps_50": 99.0099009900991,
  "slug": "btc-updown-5m-1779332700",
  "spread": 0.010000000000000009,
  "ts_utc": "2026-05-21T03:07:01Z"
}
```
