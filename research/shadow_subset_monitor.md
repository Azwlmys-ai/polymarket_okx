# Shadow Subset Monitor

> Generated: 2026-05-29T14:31:36Z  
> Source: shadow_events_backfilled_20260529.jsonl  
> Full real-CLOB events: 906  
> Read-only. No code changed. No orders placed.

---

## Hard Gate Rules

下列五项**全部满足**才允许 GO；任意一项不满足即 NO-GO 或降为 WATCH：

| Gate | 阈值 |
|------|------|
| H-G1 n | ≥ 300 |
| H-G2 mean PnL/trade | ≥ +0.020 |
| H-G3 fill margin vs BE | ≥ +2 pp |
| H-G4 max drawdown | ≤ 5.0 USDC |
| H-G5 longest losing streak | ≤ 4 |

---

## Subset Results

### ①  T+180，全 dist（基准）

| 指标 | 值 | Gate |
|------|----|------|
| n | 325 | ✅ (≥300) |
| Win rate | 85.8% | — |
| Mean PnL/trade | +0.00977 | ❌ (≥+0.020) |
| Median PnL/trade | +0.07440 | — |
| Cumulative PnL ($10/trade) | +3.1755 | — |
| Fill mean | 0.8373 | — |
| Breakeven fill | 0.8470 | — |
| Fill margin vs BE | +0.97 pp | ❌ (≥+2pp) |
| Max drawdown | 5.2187 | ❌ (≤5.0) |
| Longest losing streak | 3 | ✅ (≤4) |
| Gates passed | 2/5 | — |

**Verdict: 🟡 WATCH**

### ②  T+180，排除 fill 0.75–0.84

| 指标 | 值 | Gate |
|------|----|------|
| n | 270 | ❌ (≥300) |
| Win rate | 86.7% | — |
| Mean PnL/trade | +0.01219 | ❌ (≥+0.020) |
| Median PnL/trade | +0.05580 | — |
| Cumulative PnL ($10/trade) | +3.2922 | — |
| Fill mean | 0.8435 | — |
| Breakeven fill | 0.8560 | — |
| Fill margin vs BE | +1.25 pp | ❌ (≥+2pp) |
| Max drawdown | 4.7284 | ✅ (≤5.0) |
| Longest losing streak | 3 | ✅ (≤4) |
| Gates passed | 2/5 | — |

**Verdict: 🟡 WATCH**

### ③  T+180，排除 dist 220+

| 指标 | 值 | Gate |
|------|----|------|
| n | 300 | ✅ (≥300) |
| Win rate | 85.0% | — |
| Mean PnL/trade | +0.01042 | ❌ (≥+0.020) |
| Median PnL/trade | +0.08370 | — |
| Cumulative PnL ($10/trade) | +3.1246 | — |
| Fill mean | 0.8275 | — |
| Breakeven fill | 0.8380 | — |
| Fill margin vs BE | +1.05 pp | ❌ (≥+2pp) |
| Max drawdown | 5.4512 | ❌ (≤5.0) |
| Longest losing streak | 4 | ✅ (≤4) |
| Gates passed | 2/5 | — |

**Verdict: 🟡 WATCH**

### ④  T+180，fill < 0.75

| 指标 | 值 | Gate |
|------|----|------|
| n | 79 | ❌ (≥300) |
| Win rate | 65.8% | — |
| Mean PnL/trade | +0.00528 | ❌ (≥+0.020) |
| Median PnL/trade | +0.26040 | — |
| Cumulative PnL ($10/trade) | +0.4174 | — |
| Fill mean | 0.6268 | — |
| Breakeven fill | 0.6320 | — |
| Fill margin vs BE | +0.52 pp | ❌ (≥+2pp) |
| Max drawdown | 3.8292 | ✅ (≤5.0) |
| Longest losing streak | 4 | ✅ (≤4) |
| Gates passed | 2/5 | — |

**Verdict: 🟡 WATCH**

### ⑤  T+180，fill ≥ 0.85

| 指标 | 值 | Gate |
|------|----|------|
| n | 191 | ❌ (≥300) |
| Win rate | 95.3% | — |
| Mean PnL/trade | +0.01505 | ❌ (≥+0.020) |
| Median PnL/trade | +0.05580 | — |
| Cumulative PnL ($10/trade) | +2.8748 | — |
| Fill mean | 0.9331 | — |
| Breakeven fill | 0.9490 | — |
| Fill margin vs BE | +1.59 pp | ❌ (≥+2pp) |
| Max drawdown | 2.6352 | ✅ (≤5.0) |
| Longest losing streak | 1 | ✅ (≤4) |
| Gates passed | 2/5 | — |

**Verdict: 🟡 WATCH**

### ⑥  T+180，最近 12h

| 指标 | 值 | Gate |
|------|----|------|
| n | 30 | ❌ (≥300) |
| Win rate | 93.3% | — |
| Mean PnL/trade | +0.06473 | ✅ (≥+0.020) |
| Median PnL/trade | +0.07440 | — |
| Cumulative PnL ($10/trade) | +1.9420 | — |
| Fill mean | 0.8587 | — |
| Breakeven fill | 0.9280 | — |
| Fill margin vs BE | +6.93 pp | ✅ (≥+2pp) |
| Max drawdown | 0.9349 | ✅ (≤5.0) |
| Longest losing streak | 1 | ✅ (≤4) |
| Gates passed | 4/5 | — |

**Verdict: 🟡 WATCH**

---

## Comparison Table

| # | 子集 | n | WR | Mean PnL | Margin | Max DD | LS | Gates | Verdict |
|---|------|---|----|---------|--------|--------|-----|-------|---------|
| ① | T+180，全 dist（基准） | 325 | 85.8% | +0.00977 | +1.0pp | 5.22 | 3 | 2/5 | 🟡 WATCH |
| ② | T+180，排除 fill 0.75–0.84 | 270 | 86.7% | +0.01219 | +1.2pp | 4.73 | 3 | 2/5 | 🟡 WATCH |
| ③ | T+180，排除 dist 220+ | 300 | 85.0% | +0.01042 | +1.0pp | 5.45 | 4 | 2/5 | 🟡 WATCH |
| ④ | T+180，fill < 0.75 | 79 | 65.8% | +0.00528 | +0.5pp | 3.83 | 4 | 2/5 | 🟡 WATCH |
| ⑤ | T+180，fill ≥ 0.85 | 191 | 95.3% | +0.01505 | +1.6pp | 2.64 | 1 | 2/5 | 🟡 WATCH |
| ⑥ | T+180，最近 12h | 30 | 93.3% | +0.06473 | +6.9pp | 0.93 | 1 | 4/5 | 🟡 WATCH |

---

## Analysis Notes

### Fill 0.75–0.84 排除效果（子集② vs ①）
排除 55 件 fill 0.75–0.84 后：
- mean PnL: +0.00977 → +0.01219 (+0.00242)
- fill margin: +0.97pp → +1.25pp
- max DD: 5.22 → 4.73

### fill < 0.75 子集（④）的意义
这是 CLOB 尚未充分定价的窗口（市场 ask 仍在 0.75 以下）。
n=79，mean=+0.00528，margin=+0.52pp。
EV 不足，即使 CLOB 未定价，该子集仍未达标。

### fill ≥ 0.85 子集（⑤）的意义
市场已充分定价区间。赢率必须够高才能覆盖高 fill。
n=191，WR=95.3%，mean=+0.01505，margin=+1.59pp。

### 最近 12h（⑥）的意义
高波动窗口（近 12h）的子集表现：
n=30，mean=+0.06473，margin=+6.93pp，
max DD=0.9349，LS=1。
样本不足 300，结论不可靠，不作为决策依据。

---

## Final Verdict

**无任何子集通过全部 Hard Gate。**

**WATCH 子集（有正 EV 信号但条件不足）：**
- ①  T+180，全 dist（基准）: 缺 mean=+0.0098<0.020, margin=+1.0pp<2pp, DD=5.22>5
- ②  T+180，排除 fill 0.75–0.84: 缺 n=270<300, mean=+0.0122<0.020, margin=+1.2pp<2pp
- ③  T+180，排除 dist 220+: 缺 mean=+0.0104<0.020, margin=+1.0pp<2pp, DD=5.45>5
- ④  T+180，fill < 0.75: 缺 n=79<300, mean=+0.0053<0.020, margin=+0.5pp<2pp
- ⑤  T+180，fill ≥ 0.85: 缺 n=191<300, mean=+0.0151<0.020, margin=+1.6pp<2pp
- ⑥  T+180，最近 12h: 缺 n=30<300

### 结论

🟡 **当前 NO-GO**，但以下子集有潜力，建议重点追踪：
- ①  T+180，全 dist（基准）（2/5 gates，mean +0.0098）
- ②  T+180，排除 fill 0.75–0.84（2/5 gates，mean +0.0122）
- ③  T+180，排除 dist 220+（2/5 gates，mean +0.0104）
- ④  T+180，fill < 0.75（2/5 gates，mean +0.0053）
- ⑤  T+180，fill ≥ 0.85（2/5 gates，mean +0.0151）
- ⑥  T+180，最近 12h（4/5 gates，mean +0.0647）

每 2–3 天 checkpoint 一次，当任一 WATCH 子集满足全部 gate，再讨论上线。

---
*本文件为只读监控报告。未改任何代码，未重启服务，未下单。*
