# Execution Realism Phase Plan

Scope: research and observability only. This plan does not add live trading, an executor, private keys, strategy changes, threshold changes, service changes, databases, dashboards, or runtime rewrites.

Current goal: validate whether the observed Polymarket CLOB near-expiry high-dist edge survives realistic order-book latency, quote instability, and short remaining-time execution constraints.

## Decision Order

Do first:

1. Add offline-only latency tail statistics to the shadow report/checkpoint.
2. Add near-expiry bucketed execution statistics using existing shadow JSONL fields plus a small number of additive fields.
3. Add a minimal quote realism observer that samples the same read-only CLOB book for a short period after a shadow signal.

Future work:

1. Compare multiple VPS regions only if current latency p99 is unstable.
2. Add deeper order-book persistence analysis only if quote lifetime is borderline.
3. Add offline slippage sensitivity by remaining-time bucket.

Do not do:

1. Do not build a real executor.
2. Do not connect a trading account.
3. Do not add new alpha, ML, agents, dashboards, databases, or high-frequency replay infrastructure.
4. Do not change `paper_anchor_sim.py`, `shadow_execution_recorder.py` runtime behavior, `polymarket-okx-anchor.service`, or signal thresholds in this phase.

## 1. p99 Latency

Purpose: p50 and p95 are insufficient for near-expiry execution. A small number of latency spikes can destroy execution quality when remaining time is 30-60 seconds.

### Minimal Additive Fields

Add these fields only to future shadow/observer JSONL records if implementation is approved later:

```json
{
  "SHADOW_ONLY": true,
  "event_type": "clob_book_snapshot",
  "ts_utc": "2026-05-28T00:00:00Z",
  "signal_ts_utc": "2026-05-28T00:00:00Z",
  "slug": "btc-updown-5m-...",
  "checkpoint": "T+120",
  "distance": 150.0,
  "remaining_time_sec": 60.0,
  "remaining_bucket": "T-120~60",
  "clob_fetch_latency_ms": 320.5,
  "fetch_started_monotonic_ms": 123456.0,
  "fetch_finished_monotonic_ms": 123776.5,
  "fetch_error": null,
  "book_available": true,
  "fallback_used": false
}
```

No private API fields are required.

### Statistics

For all real-CLOB snapshots and for each near-expiry bucket:

| Metric | Definition |
| --- | --- |
| p50 latency | Median `clob_fetch_latency_ms` |
| p95 latency | 95th percentile |
| p99 latency | 99th percentile |
| max latency | Maximum observed latency |
| spike count | Count where latency exceeds danger threshold |
| spike duration | Consecutive wall-clock time spent in spike state |

Spike duration should be calculated from consecutive shadow snapshots where latency exceeds the configured danger threshold. It is an offline calculation, not runtime control logic.

### Checkpoint Display

```text
Latency Tail — real CLOB snapshots
bucket       N    p50    p95    p99    max    spikes>750ms    max_spike_duration
T-300~120    80   305    360    520    710    0               0s
T-120~60     74   310    380    690    820    1               2s
T-60~30      70   320    410    850    1100   3               8s
T-30~0       76   340    520    1300   2100   9               18s
```

### Dangerous Values

Initial thresholds for diagnosis only:

| Condition | Status |
| --- | --- |
| p99 <= 750ms and max <= 1500ms | Acceptable for shadow observation |
| p99 750-1500ms or repeated spikes > 2s | Degraded |
| p99 > 1500ms, max > 3000ms, or spike duration > 10s near T-60 | Too fragile |

Near-expiry danger is stricter:

| Remaining window | Dangerous p99 |
| --- | --- |
| T-300~120 | > 1500ms |
| T-120~60 | > 1000ms |
| T-60~30 | > 750ms |
| T-30~0 | > 500ms |

## 2. Near-Expiry Layered Statistics

Purpose: current executable/fillability is global. It must be split by remaining-time window to determine whether the edge is concentrated in a window that is operationally too short.

### Buckets

| Bucket | Remaining time |
| --- | --- |
| T-300~120 | 300s >= remaining > 120s |
| T-120~60 | 120s >= remaining > 60s |
| T-60~30 | 60s >= remaining > 30s |
| T-30~0 | 30s >= remaining >= 0s |

### Minimal Incremental Fields

```json
{
  "remaining_time_sec": 60.0,
  "remaining_bucket": "T-120~60",
  "best_bid": 0.73,
  "best_ask": 0.74,
  "mid": 0.735,
  "spread": 0.01,
  "side_specific_depth_top1": 120.0,
  "side_specific_depth_top3": 500.0,
  "side_specific_depth_top5": 900.0,
  "estimated_fill_price_10": 0.74,
  "estimated_fill_price_25": 0.74,
  "estimated_fill_price_50": 0.75,
  "executable_10": true,
  "executable_25": true,
  "executable_50": true,
  "clob_orderbook_available": true,
  "fallback_used": false,
  "clob_fetch_latency_ms": 310.0
}
```

Existing fields already cover most of this. The key additive fields are `remaining_time_sec` and normalized `remaining_bucket`.

### Per-Bucket Metrics

| Metric | Source |
| --- | --- |
| executable rate 10/25/50 | `executable_10/25/50` |
| spread mean/p50/p95 | `spread` |
| liquidity/depth p50/p95 | `side_specific_depth_top1/top3/top5` |
| latency p50/p95/p99/max | `clob_fetch_latency_ms` |
| fallback rate | `fallback_used` |
| orderbook availability | `clob_orderbook_available` |
| sample count | records per bucket |

### Checkpoint Display

```text
Near-Expiry Execution Buckets
bucket       N    CLOB%   fallback%   exec10   exec25   exec50   spread_p50   depth5_p50   lat_p50   lat_p95   lat_p99
T-300~120    75   99.0    1.0         100.0    100.0    100.0    0.01         1800         300       355       520
T-120~60     73   98.5    1.5         100.0    100.0    99.0     0.01         1200         310       380       690
T-60~30      78   96.0    4.0         100.0    98.0     94.0     0.02         650          325       440       850
T-30~0       74   91.0    9.0         98.0     92.0     83.0     0.03         240          355       620       1300
```

### Interpretation

REALISTIC:

- CLOB availability >= 95% in all buckets used for candidate live-shadow.
- executable_10 >= 95%, executable_25 >= 90%, executable_50 >= 80%.
- p99 latency remains below the bucket danger threshold.
- Spread does not expand materially in T-60~0.

TOO FRAGILE:

- Fillability collapses only in T-60~0.
- Fallback rises above 10% near expiry.
- p99 latency breaches bucket danger threshold.
- Depth p50 falls below the intended notional.

## 3. Queue / Quote Realism Observer

Purpose: determine whether the quote observed at signal time still exists long enough for a realistic taker decision path. This is observation only. It must not simulate or place orders.

### Minimal Observer Design

For each fresh shadow candidate that passes the existing threshold:

1. Capture the immediate CLOB book snapshot at signal time.
2. For the next 5 seconds, poll the same read-only book endpoint every 500ms.
3. Write one compact observer event per signal with aggregate quote persistence metrics.
4. Do not alter the main runner, strategy, thresholds, services, or shadow follow decision logic.

This observer can be implemented later as an offline/research append-only mode. It should not become a new service until the current shadow validation is complete.

### Minimal Fields

```json
{
  "SHADOW_ONLY": true,
  "event_type": "quote_realism_observation",
  "ts_utc": "2026-05-28T00:00:00Z",
  "slug": "btc-updown-5m-...",
  "checkpoint": "T+120",
  "direction": "UP",
  "distance": 150.0,
  "remaining_time_sec": 60.0,
  "remaining_bucket": "T-120~60",
  "initial_best_bid": 0.73,
  "initial_best_ask": 0.74,
  "initial_spread": 0.01,
  "initial_depth_top1": 120.0,
  "initial_depth_top3": 500.0,
  "initial_depth_top5": 900.0,
  "sample_interval_ms": 500,
  "observation_duration_ms": 5000,
  "samples_attempted": 10,
  "samples_ok": 10,
  "same_best_quote_samples": 7,
  "quote_lifetime_ms": 3500,
  "best_quote_disappear_count": 1,
  "book_flicker_count": 2,
  "max_adverse_price_jump": 0.02,
  "price_jump_1s": 0.01,
  "price_jump_3s": 0.02,
  "price_jump_5s": 0.02,
  "quote_disappear_rate": 10.0,
  "observer_error": null
}
```

### Definitions

| Field | Definition |
| --- | --- |
| quote_lifetime_ms | Longest continuous duration where the initial best executable quote remains available |
| same_best_quote_samples | Count of samples where best bid/ask equals initial best quote |
| book_flicker_count | Count of best quote changes during observation |
| best_quote_disappear_count | Count where same-side top quote is missing or not executable |
| price_jump_1s/3s/5s | Difference between initial executable price and later executable price |
| max_adverse_price_jump | Worst observed move against the intended side |
| quote_disappear_rate | Missing/unusable quote samples divided by valid samples |

### Classification

REALISTIC:

- quote_lifetime_ms >= 1500ms in T-120~60 and >= 1000ms in T-60~30.
- quote_disappear_rate <= 10%.
- max_adverse_price_jump <= 0.02.
- book_flicker_count <= 3 over 5 seconds.
- executable_10 remains true in at least 90% of observer samples.

TOO FRAGILE:

- quote_lifetime_ms < 500ms in T-60~0.
- quote_disappear_rate > 30%.
- max_adverse_price_jump >= 0.05.
- best quote changes almost every sample.
- executable_10 is lost in more than 20% of samples.

## Minimal File Outputs

If implementation is approved later, keep outputs append-only:

```text
research/execution_realism_observations.jsonl
research/execution_realism_report.md
```

Do not modify existing runtime files as part of this phase. Existing `shadow_execution_events.jsonl` can be used as the source for offline analysis.

## Proposed Checkpoint Summary

```text
Execution Realism Checkpoint
real_clob_samples: 300
fallback_rate: 1.7%
clob_available_rate: 100.0%
exec10/25/50: 100.0% / 100.0% / 100.0%
latency p50/p95/p99/max: 302ms / 358ms / 720ms / 1100ms

Near-expiry risk:
T-300~120: REALISTIC
T-120~60: REALISTIC
T-60~30: DEGRADED
T-30~0: TOO FRAGILE

Quote realism:
quote_lifetime_p50: 1800ms
quote_disappear_rate: 12.0%
max_adverse_jump_p95: 0.03

live_permission: NO
reason: execution realism still under observation
```

## Exit Criteria For This Phase

Still no live trading unless all are true:

1. At least 300 fresh real-CLOB shadow samples.
2. CLOB availability >= 95% overall and >= 90% in T-60~0.
3. executable_10 >= 95% in T-60~0.
4. p99 latency below the bucket danger threshold.
5. quote_lifetime_p50 >= 1000ms for the intended execution bucket.
6. quote_disappear_rate <= 10%.
7. extra 5c slippage stress remains profitable in the relevant bucket.

Even if these pass, the next step is a separate approval decision, not automatic live trading.

