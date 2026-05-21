# Paper Anchor Simulation Report

> Generated: 2026-05-21T02:31:05Z
> Strategy: anchor_est = Binance_T_open − 76.75
> Signal threshold: $40.0
> Check offsets: T+90s / T+120s / T+180s
> Fee: 7% taker, break-even = 53.5%

## 1. Dataset

| | Value |
|---|---|
| Total windows recorded | 2 |
| Resolved windows | 2 |
| Total checkpoints with signal | 6 |
| Unique windows with ≥1 signal | 2 |
| Time range | 2026-05-21 02:15 → 2026-05-21 02:20 UTC |

## 2. Paper Trading Results (All Signals)

| Metric | Value |
|--------|-------|
| Total triggered signals | 6 |
| Wins (direction correct) | 6 |
| Win rate | 100.0% |
| Mean fee-adj PnL/signal | +0.4557 |
| Median fee-adj PnL | +0.4557 |
| StdDev PnL | 0.0000 |
| Break-even PnL threshold | -0.0350 (fee at 0.50) |

## 3. Per-Offset Breakdown

| Offset | N | Win Rate | Mean PnL | Median PnL | Tradeable |
|--------|---|---------|---------|-----------|-----------|
| T+90s | 2 | 100.0% | +0.4557 | +0.4557 | 2/2 |
| T+120s | 2 | 100.0% | +0.4557 | +0.4557 | 2/2 |
| T+180s | 2 | 100.0% | +0.4557 | +0.4557 | 2/2 |

## 4. CLOB Tradability

| Metric | Value |
|--------|-------|
| Signals with spread data | 6 |
| Spread ≤ 0.03 | 6/6 (100.0% if spreads else 'N/A') |
| Mean spread | 0.010 |
| Median spread | 0.010 |
| Mean CLOB liquidity | $12,617 |
| Min CLOB liquidity | $12,225 |
| Tradeable signals (spread ≤ 0.03) | 6 |
| Tradeable win rate | 100.0% |
| Tradeable mean PnL | +0.4557 |

## 5. Signal Distance Distribution

Distance = |BTC_live − anchor_est| at checkpoint time.

**Distance ≥ $40** (N=6, 100% of signals): win rate = 100.0%, mean PnL = +0.4557
**Distance ≥ $60** (N=6, 100% of signals): win rate = 100.0%, mean PnL = +0.4557
**Distance ≥ $80** (N=6, 100% of signals): win rate = 100.0%, mean PnL = +0.4557
**Distance ≥ $100** (N=3, 50% of signals): win rate = 100.0%, mean PnL = +0.4557

## 6. Anchor Proxy Quality (Post-hoc)

| Statistic | Value |
|-----------|-------|
| N anchors measured | 2 |
| Mean (Binance_T_open − priceToBeat) | +72.61 USD |
| Median | +72.61 USD |
| StdDev | 0.62 USD |
| vs calibrated correction (76.75) | diff=-4.14 USD |

## 7. GO / NO-GO

### Verdict: **⏳ IN PROGRESS** — 2/50 resolved windows

| Criterion | Value | Pass? |
|-----------|-------|-------|
| Windows resolved ≥ 50 (full run ≥ 200) | 2 | ❌ |
| Triggered signals ≥ 30 | 6 | ❌ |
| Win rate ≥ 75% | 100.0% | ✅ |
| Fee-adj mean PnL > 0 | +0.4557 | ✅ |
| Median PnL > 0 | +0.4557 | ✅ |
| CLOB tradeable spread (≥ 50% signals) | 100% | ✅ |

## 8. Recent Signals (last 20)

| Window | Offset | BTC | Anchor | Dist | Dir | Outcome | PnL |
|--------|--------|-----|--------|------|-----|---------|-----|
| 1779329700 | T+90 | 77897 | 77797 | 100 | UP | UP ✅ | +0.456 |
| 1779329700 | T+120 | 77908 | 77797 | 111 | UP | UP ✅ | +0.456 |
| 1779329700 | T+180 | 77922 | 77797 | 125 | UP | UP ✅ | +0.456 |
| 1779330000 | T+90 | 77998 | 77907 | 91 | UP | UP ✅ | +0.456 |
| 1779330000 | T+120 | 78007 | 77907 | 99 | UP | UP ✅ | +0.456 |
| 1779330000 | T+180 | 77991 | 77907 | 84 | UP | UP ✅ | +0.456 |

---
*Paper simulation only. No real trades. No wallet access.*