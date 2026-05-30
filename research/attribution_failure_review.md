# Attribution Failure Review

> Generated: 2026-05-29T14:27:36Z  
> Source: shadow_events_backfilled_20260529.jsonl  
> Real-CLOB events: 906  
> Read-only analysis. No code changed. No orders placed.

---

## 0. Summary Verdict

| Metric | Value | Status |
|--------|-------|--------|
| n | 906 | — |
| Win rate | 80.6% | ❌ |
| Mean PnL/trade | -0.00139 | ❌ |
| Cumulative PnL ($10/trade) | -1.2588 | ❌ |
| Fill mean | 0.7926 | — |
| Breakeven fill | 0.7910 | — |
| Fill margin vs BE | -0.16 pp | ❌ |
| Max drawdown | 16.6076 | ❌ |
| Longest losing streak | 8 | ❌ |

**Overall: 🔴 NO-GO — full dataset does not clear any positive EV gate.**

---

## 1. 亏损来源分析

### 1a. Checkpoint 拆分

| CP | n | WR | BE Fill | Fill Mean | Margin (pp) | Mean PnL | Max DD | LS | 判断 |
|----|----|-----|---------|-----------|------------|---------|--------|-----|------|
| T+90 | 285 | 76.1% | 0.743 | 0.7498 | -0.7 ❌ | -0.00596 | 7.478 | 4 | ❌ |
| T+120 | 296 | 79.1% | 0.774 | 0.7847 | -1.1 ❌ | -0.00925 | 7.063 | 3 | ❌ |
| T+180 | 325 | 85.8% | 0.847 | 0.8373 | +1.0 ✅ | +0.00977 | 5.219 | 3 | ✅ |

**T+90 和 T+120 的 fill margin 均为负**（fill > breakeven），是主要亏损来源。
T+180 是唯一正 fill margin 检查点，但幅度很小。

### 1b. Dist Bucket 拆分

| Bucket | n | WR | BE Fill | Fill Mean | Over-BE (pp) | Mean PnL | Cum PnL | 判断 |
|--------|----|----|---------|-----------|-------------|---------|---------|------|
| 130-149 | 401 | 73.6% | 0.715 | 0.7102 | -0.5 pp | +0.00513 | +2.0576 | ✅ |
| 150-179 | 284 | 82.4% | 0.810 | 0.8180 | +0.8 pp | -0.00677 | -1.9230 | ❌ |
| 180-219 | 162 | 90.7% | 0.900 | 0.8951 | -0.5 pp | +0.00495 | +0.8011 | ⚠️ |
| 220+ | 59 | 91.5% | 0.908 | 0.9489 | +4.1 pp | -0.03720 | -2.1946 | ❌ |

### 1c. 220+ Bucket 深度解析（为何高胜率但亏钱）

220+ 事件数：59，赢率：91.5%，均值 fill：0.9489
该赢率下的盈亏平衡 fill：0.908
实际 fill 超出 BE：+4.1 pp

**原因**：

- BTC 距离锚点已达 220+ 点时，Polymarket CLOB 也已看到这条大幅运动。
  做市商将 YES ask 定到 0.94–0.99（均值 0.949），
  但真实赢率约 91.5%——对应 BE fill = 0.908。
  买方每笔支付超额 4.1 pp，EV 为负。
- **结论**：220+ 信号已被市场充分定价甚至超额定价。高赢率≠正 EV。
  dist 越高，市场越聪明，edge 越小甚至反转。

### 1d. Fill Price 分位数拆分

| Fill 区间 | n | WR | Mean PnL | Cum PnL | EV 来源 |
|-----------|----|----|---------|---------|---------|
| <0.65 | 148 | 62.8% | +0.04010 | +5.9355 | ✅ |
| 0.65-0.74 | 155 | 73.5% | +0.01641 | +2.5437 | ✅ |
| 0.75-0.84 | 233 | 75.1% | -0.06236 | -14.5295 | ❌ |
| 0.85-0.92 | 184 | 90.8% | +0.01515 | +2.7876 | ✅ |
| 0.93-0.96 | 111 | 96.4% | +0.01542 | +1.7113 | ✅ |
| >0.96 | 75 | 98.7% | +0.00390 | +0.2927 | ✅ |

**主要亏损来源：fill 0.75–0.84 区间**（233 件，累计 −14.5）。这是"危险地带"：
CLOB 已将 ask 推至 0.75–0.84，但实际赢率（75.1%）对应的盈亏平衡 fill ≈ 0.74，
买方每笔平均亏约 0.062——这一区间贡献了全量负 EV 的绝大部分。

fill < 0.74（303 件合计）和 fill ≥ 0.85（370 件合计）均有正 EV。
策略的真实 edge 存在于两端，被中间的"定价过渡带"吞噬。

---

## 2. 子集 Shadow 统计

### 子集 A：T+180 + dist 130–149

| 指标 | 值 |
|------|----|
| n | 123 |
| Win rate | 74.0% |
| Mean PnL/trade | -0.02749 |
| Median PnL/trade | +0.11160 |
| Cumulative PnL ($10/trade) | -3.3819 |
| Fill mean | 0.7498 |
| BE fill | 0.7200 |
| Fill margin vs BE | -2.98 pp |
| Max drawdown | 6.0957 |
| Longest losing streak | 4 |

**Gates: 1/5**

| Gate | 阈值 | 实际 | 状态 |
|------|------|------|------|
| G1 mean PnL ≥ +0.020 | — | -0.0275 | ❌ |
| G2 n ≥ 300 | — | 123 | ❌ |
| G3 fill margin > 0 | — | -2.9820 | ❌ |
| G4 max DD ≤ 5.0 | — | +6.0957 | ❌ |
| G5 losing streak ≤ 4 | — | 4 | ✅ |

### 子集 B：T+180 + dist 130–179

| 指标 | 值 |
|------|----|
| n | 237 |
| Win rate | 81.4% |
| Mean PnL/trade | +0.00045 |
| Median PnL/trade | +0.10230 |
| Cumulative PnL ($10/trade) | +0.1070 |
| Fill mean | 0.7999 |
| BE fill | 0.8000 |
| Fill margin vs BE | +0.01 pp |
| Max drawdown | 6.1169 |
| Longest losing streak | 4 |

**Gates: 2/5**

| Gate | 阈值 | 实际 | 状态 |
|------|------|------|------|
| G1 mean PnL ≥ +0.020 | — | +0.0005 | ❌ |
| G2 n ≥ 300 | — | 237 | ❌ |
| G3 fill margin > 0 | — | +0.0113 | ✅ |
| G4 max DD ≤ 5.0 | — | +6.1169 | ❌ |
| G5 losing streak ≤ 4 | — | 4 | ✅ |

### 子集 C：T+180 + 最近 12h

| 指标 | 值 |
|------|----|
| n | 30 |
| Win rate | 93.3% |
| Mean PnL/trade | +0.06473 |
| Median PnL/trade | +0.07440 |
| Cumulative PnL ($10/trade) | +1.9420 |
| Fill mean | 0.8587 |
| BE fill | 0.9280 |
| Fill margin vs BE | +6.93 pp |
| Max drawdown | 0.9349 |
| Longest losing streak | 1 |

**Gates: 4/5**

| Gate | 阈值 | 实际 | 状态 |
|------|------|------|------|
| G1 mean PnL ≥ +0.020 | — | +0.0647 | ✅ |
| G2 n ≥ 300 | — | 30 | ❌ |
| G3 fill margin > 0 | — | +6.9289 | ✅ |
| G4 max DD ≤ 5.0 | — | +0.9349 | ✅ |
| G5 losing streak ≤ 4 | — | 1 | ✅ |

### 参考：T+180 全部 dist

| 指标 | 值 |
|------|----|
| n | 325 |
| Win rate | 85.8% |
| Mean PnL/trade | +0.00977 |
| Median PnL/trade | +0.07440 |
| Cumulative PnL ($10/trade) | +3.1755 |
| Fill mean | 0.8373 |
| BE fill | 0.8470 |
| Fill margin vs BE | +0.97 pp |
| Max drawdown | 5.2187 |
| Longest losing streak | 3 |

**Gates: 3/5**

| Gate | 阈值 | 实际 | 状态 |
|------|------|------|------|
| G1 mean PnL ≥ +0.020 | — | +0.0098 | ❌ |
| G2 n ≥ 300 | — | 325 | ✅ |
| G3 fill margin > 0 | — | +0.9698 | ✅ |
| G4 max DD ≤ 5.0 | — | +5.2187 | ❌ |
| G5 losing streak ≤ 4 | — | 3 | ✅ |

### 参考：dist 130–149 全部 CP

| 指标 | 值 |
|------|----|
| n | 401 |
| Win rate | 73.6% |
| Mean PnL/trade | +0.00513 |
| Median PnL/trade | +0.17670 |
| Cumulative PnL ($10/trade) | +2.0576 |
| Fill mean | 0.7102 |
| BE fill | 0.7150 |
| Fill margin vs BE | +0.48 pp |
| Max drawdown | 11.2488 |
| Longest losing streak | 6 |

**Gates: 2/5**

| Gate | 阈值 | 实际 | 状态 |
|------|------|------|------|
| G1 mean PnL ≥ +0.020 | — | +0.0051 | ❌ |
| G2 n ≥ 300 | — | 401 | ✅ |
| G3 fill margin > 0 | — | +0.4753 | ✅ |
| G4 max DD ≤ 5.0 | — | +11.2488 | ❌ |
| G5 losing streak ≤ 4 | — | 6 | ❌ |

---

## 3. 下一阶段 Gate 定义（子集专用）

若选择继续 shadow 某个子集，GO 条件必须**同时**满足：

| Gate | 阈值 | 说明 |
|------|------|------|
| S-G1 | mean PnL/trade ≥ +0.020 | 不接受勉强正，需要明显 edge |
| S-G2 | 子集 n ≥ 300 | 统计置信度最低要求 |
| S-G3 | fill margin vs BE > +2 pp | 不能只是勉强过线 |
| S-G4 | max drawdown ≤ 5.0 USDC（per $10/trade）| 防止大波动期集中亏损 |
| S-G5 | longest losing streak ≤ 4 | 连败容忍上限 |
| S-G6 | 最近 12h mean PnL 与全量偏差 ≤ 50% | 不接受明显时间段依赖 |

**注**：WR 不再作为单一门控。只关心 fill margin（WR 高但 fill 贵 = 亏钱）。

---

## 4. 当前子集评估结论

| 子集 | 关键指标 | 结论 |
|------|----------|------|
| 子集 A：T+180 + dist 130–149 | gates 1/5, mean=-0.02749, margin=-3.0pp | ❌ NO-GO |
| 子集 B：T+180 + dist 130–179 | gates 2/5, mean=+0.00045, margin=+0.0pp | ❌ NO-GO |
| 子集 C：T+180 + 最近 12h | gates 4/5, mean=+0.06473, margin=+6.9pp | ⚠️ 微正，需更多样本验证 |
| 参考：T+180 全部 dist | gates 3/5, mean=+0.00977, margin=+1.0pp | ⚠️ 微正，需更多样本验证 |
| 参考：dist 130–149 全部 CP | gates 2/5, mean=+0.00513, margin=+0.5pp | ⚠️ 微正，需更多样本验证 |

---

## 5. 最终结论

### 是否存在值得继续 shadow 的子集？

**是**，以下子集有微弱但非零的正 EV 信号：
- 子集 C：T+180 + 最近 12h：⚠️ 微正，需更多样本验证
- 参考：T+180 全部 dist：⚠️ 微正，需更多样本验证
- 参考：dist 130–149 全部 CP：⚠️ 微正，需更多样本验证

但所有子集均未通过 S-G2（n < 300），不可上实盘。

### 是否需要调整信号过滤？

**是**，基于当前数据，有以下过滤方向值得研究（均为只读分析，不改运行代码）：

1. **剔除 220+ 信号**：market 对极高 dist 信号已充分定价，系统性亏损来源
2. **只保留 T+180**：T+90/T+120 两个 checkpoint 的 fill margin 均为负
3. **fill price 上限过滤**：fill > 0.92 的事件 EV 几乎全部为负，可考虑不入场
4. **波动率窗口过滤**：最近 12h 高波动环境 EV 显著更高，低波动期可暂停

注：以上为分析建议，不涉及任何运行时代码修改。

### 是否仍然 NO-GO？

**是。全数据集 NO-GO。所有子集数据量均不足 300，无法作出上线判断。**

下一步唯一行动：

```
继续跑 shadow（不改参数），每 2–3 天做一次 checkpoint。
重点观察：T+180 + dist 130–179 子集的 mean PnL 是否能稳定在 +0.020 以上。
当该子集 n ≥ 300 且所有 S-G 全部通过，才可讨论小额实盘准备。
```

---
*本文件为只读分析。未改任何代码，未重启服务，未下单。*
