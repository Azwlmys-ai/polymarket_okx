# Paper Quote Simulator — Report

> Market: `xi-jinping-out-before-2027`
> Generated: 2026-05-21T15:30:30Z
> Rounds: 8  |  Quote size: $200/side

## 1. Fill Statistics

| | Value |
|---|---|
| Total rounds | 8 |
| Rounds with ≥1 fill | 5 (62%) |
| Both-sides filled | 2 (25%) |
| One-side only | 3 (38%) |
| No fills | 3 (38%) |
| Fill prob YES (avg) | 61.2% |
| Fill prob NO (avg)  | 3.5% |

## 2. P&L Summary

| Component | USD |
|-----------|-----|
| Realized spread P&L | +0.4000 |
| Total rebate earned  | +9.6200 |
| Hedge costs incurred | -561.6000 |
| **Net P&L after hedge** | **-551.5800** |
| Unrealized P&L (mark) | +0.3000 |
| Total fills executed  | 7 |

## 3. Inventory Status

| | YES | NO |
|---|---|---|
| Final inventory (USD) | 600.00 | 0.00 |
| Avg cost | 0.0640 | 0.9350 |
| Inventory cap reached | No | No |

## 4. Risk Metrics

| Metric | Value |
|--------|-------|
| VaR 95% (final round) | $0.0084 |
| Toxic flow events | 0 / 8 rounds |
| In-rewards-band ratio | 100% |
| Spread bps (mean/min/max) | 155.0 / 155.0 / 155.0 |
| Depth near mid (mean) | $2,048 |

## 5. Round-by-Round Log

| Round | Scenario | YES inv | NO inv | Rebate | Real PnL | VaR | Toxic |
|-------|----------|---------|--------|--------|----------|-----|-------|
| 1 | both       |     0.0 |    0.0 | +2.00200 |  +0.2000 | 0.0000 | — |
| 2 | yes_only   |   200.0 |    0.0 | +1.87200 |  +0.2000 | 0.0028 | — |
| 3 | none       |   200.0 |    0.0 | +0.00000 |  +0.2000 | 0.0028 | — |
| 4 | none       |   200.0 |    0.0 | +0.00000 |  +0.2000 | 0.0028 | — |
| 5 | both       |   200.0 |    0.0 | +2.00200 |  +0.4000 | 0.0028 | — |
| 6 | yes_only   |   400.0 |    0.0 | +1.87200 |  +0.4000 | 0.0056 | — |
| 7 | yes_only   |   600.0 |    0.0 | +1.87200 |  +0.4000 | 0.0084 | — |
| 8 | none       |   600.0 |    0.0 | +0.00000 |  +0.4000 | 0.0084 | — |

## 6. GO / WATCH / NO-GO

### Verdict: ❌ NO-GO — negative net P&L

| Condition | Result |
|-----------|--------|
| Net P&L > 0 | ❌ |
| Fill ratio > 0 | ✅ |
| No inventory cap breach | ✅ |
| Toxic events < 20% of rounds | ✅ |
| In-rewards-band > 80% | ✅ |
| Depth > 0 every round | ✅ |

---
*Paper simulation only. No real orders placed. No wallet required.*