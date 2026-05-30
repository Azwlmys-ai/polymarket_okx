# Wallet 0xe022 Research Summary

- Generated: `2026-05-25 18:43:13 CST`
- Wallet: `0xe0229e10a858860218b6132f4234602c47bd6603`
- Inputs:
  - `research/wallet_0xe022_behavior_report.md`
  - `research/wallet_0xe022_enriched_markets.jsonl`
  - `research/wallet_0xe022_trades_raw.jsonl`
  - `research/wallet_0xe022_markets_raw.jsonl`

## Decision

**NO-GO for VPS shadow test.**

This is not a conclusion that the wallet has no research value. It means the current observed features are not strong or clean enough to convert into a strategy or shadow-trading hypothesis. The wallet is worth keeping as a research object, but not worth wiring into shadow execution.

## Evidence Base

| Metric | Value |
|---|---:|
| Raw trades | 2491 |
| Raw markets | 116 |
| Enriched markets | 116 |
| BTC 5m candidates | 116 |
| confirmed_metadata_count | 116 |
| metadata_missing_count | 0 |
| STR_confirmed_count | 116 |
| confirmed_flip_rate | 17.2% (20/116) |
| flip_uncertain_count | 93 |
| flip_uncertain_rate | 80.2% |
| last 60-120s concentrated signal | False |

## Required Questions

### 1. Does this wallet mainly trade BTC 5m Up/Down?

Yes. In the current sample, all 116 enriched markets are BTC 5m Up/Down candidates. The metadata quality issue from Phase 2 was fixed in Phase 2.5: 115 markets were recovered through `gamma-api /events?slug=...`, and 1 through `markets?condition_ids=...`.

### 2. Is entry time concentrated in the final 60-120 seconds?

No. The final 60-120 second buckets contain 481 of 2491 entries. That is visible activity, but it is not concentrated enough to describe as a clear timing edge.

Entry distribution is broader, with substantial activity in 120-300 seconds before resolution:

| Bucket | Count |
|---|---:|
| 0-30 | 161 |
| 30-60 | 194 |
| 60-90 | 241 |
| 90-120 | 240 |
| 120-180 | 508 |
| 180-240 | 558 |
| 240-300 | 579 |
| out_of_range | 10 |

### 3. Is there evidence of late-stage adding?

There is evidence of repeated adding: all 116 markets have `buy_count >= 2`, with a max buy count of 49 in one market. However, it is not specifically a final 60-120 second add pattern. The adds are spread across the full 5-minute window, especially 120-300 seconds before resolution.

This supports a behavior description: **batch entry / repeated buying**. It does not support a tradable conclusion: **late-window add edge**.

### 4. Is confirmed flip rate enough to identify a reactive/orderflow trader?

No. The confirmed flip rate is 17.2% (20/116), which is not enough by itself to label this wallet as a reactive/orderflow trader.

There are observed net switches in some markets, but most potential flip-like behavior is not confirmed. The available data is mostly BUY-only, with total sell size shown as 0.0, so many apparent direction changes may be dual-side buying, hedging, partial risk transfer, or incomplete exit visibility rather than true reactive flipping.

### 5. What does `flip_uncertain=93` do to the conclusion?

It materially weakens any flip-based conclusion. `flip_uncertain=93/116` means 80.2% of markets cannot support a clean flip interpretation.

The important rule is: **do not treat `flip_uncertain` as confirmed flip**. It is a warning label. It means the sequence contains Up/Down exposure or ambiguous net changes, but the data cannot prove a deliberate directional reversal with reliable position reduction or exit mechanics.

### 6. Is this worth entering VPS shadow test now?

No.

Reasons:
- No final 60-120 second concentrated entry/add signal.
- Confirmed flip rate is only 17.2%.
- `flip_uncertain` is too high at 93/116.
- No sell-side visibility in this sample, so active exits and true position flips cannot be trusted.
- The current findings describe wallet behavior, not a reproducible alpha signal.

This is a **NO-GO because the current features are not worth turning into a strategy**, not because the wallet is worthless to study.

### 7. If not, what is the next minimal research action?

The next minimal action is **offline outcome attribution**, still read-only:

1. For each confirmed BTC 5m market, join the wallet's final net outcome (`main_net_outcome`) to the resolved market outcome.
2. Compute hit rate by STR bucket and by whether the market has `flip_uncertain`.
3. Separate BUY-only dual-outcome markets from clean one-sided markets.
4. Do not add execution, shadow trading, or strategy wiring until a specific bucket or behavior class shows a stable outcome advantage.

The smallest useful question is: **when this wallet ends with a main net outcome, did that outcome resolve correctly more often than baseline, and in which STR bucket?**

### 8. What does this imply for current `polymarket_okx` strategy?

Useful lessons:
- Keep focusing on BTC 5m market structure; this wallet confirms high activity there.
- Batch entry behavior is common, so single-trade snapshots are probably not enough to infer intent.
- STR buckets should be evaluated against actual outcomes, not treated as a signal by themselves.
- Any future wallet-following logic must model net exposure by outcome, not raw trade direction.
- Ambiguity flags such as `flip_uncertain` should block automation rather than be converted into a score.

Not useful yet:
- No direct signal to add to `mvp_runner.py`.
- No trigger for VPS shadow test.
- No evidence to add a late 60-120 second entry rule.
- No evidence to follow apparent Up/Down flips as reactive signals.

### 9. Which conclusions cannot be used?

Do not use these as strategy inputs:

- “This wallet has a strong late-entry edge.” Not supported.
- “This wallet flips directions aggressively.” Not supported; most flip-like cases are uncertain.
- “BUY on both outcomes means a confirmed reversal.” False.
- “Batch adding is alpha.” Not proven; it is only observed behavior.
- “Holding time implies exit quality.” Not reliable because sell-side visibility is absent.
- “Correlation with BTC 5m activity is enough for shadow test.” Not enough.
- “Metadata completeness means trading signal quality.” Metadata is now complete, but behavioral interpretation remains uncertain.

## Final Conclusion

**NO-GO.**

The wallet is clearly relevant to BTC 5m Up/Down research, but the current features do not justify VPS shadow testing or strategy conversion. The next step should remain offline research: outcome attribution by final net exposure and STR bucket. Until that shows a stable edge, this wallet should stay out of execution paths.

## Scope Confirmation

- No trading logic was added.
- No orders were placed.
- No live trading connection was made.
- No VPS shadow test was started.
- `mvp_runner.py` was not modified.
- Existing systemd services were not modified.
- `paper_anchor_sim.py` was not touched.
