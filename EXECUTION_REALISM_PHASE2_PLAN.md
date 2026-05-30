# Execution Realism Phase 2 Plan

Scope: minimal observability and offline analysis only.

Hard constraints:

- No live trading.
- No executor.
- No private keys or trading API calls.
- No new alpha, strategy, or threshold changes.
- No changes to `polymarket-okx-anchor.service`.
- No changes to `paper_anchor_sim.py`.
- No changes to `shadow_execution_recorder.py` in this planning step.
- No healthcheck/systemd changes.
- No database, dashboard, websocket rewrite, replay engine, or high-frequency system.

Current phase goal:

Validate whether the observed Polymarket near-expiry edge remains executable, fillable, and reproducible under realistic latency tail and remaining-time conditions.

`live_permission: NO`

## Implementation Order

Do first:

1. Add offline latency tail stats to checkpoint/report logic:
   - p99 latency
   - max latency
   - spikes above threshold
   - longest spike duration
2. Add near-expiry bucketization to offline checkpoint/report logic:
   - T-300~120
   - T-120~60
   - T-60~30
   - T-30~0

Future work:

- Quote lifetime observer.
- Orderbook flicker analysis.
- Queue simulation.
- Partial fill simulator.
- Replay engine.
- Dashboard.
- Dedicated high-frequency microstructure collector.

Not worth doing now:

- Increasing signal count.
- Adding new strategy filters.
- Optimizing alpha.
- Building a live executor.
- Adding a new database.
- Reworking websocket/runtime architecture.

## Part 1: p99 / Max Latency

### Objective

Current p50/p95 latency is not enough. Near-expiry execution can fail because of rare stalls, API pauses, or short bursts of latency that are invisible in average metrics.

Add tail diagnostics without changing signal logic or runtime architecture.

### Minimal JSONL Incremental Fields

Existing shadow events already include `clob_fetch_latency_ms` and timestamps. If adding fields later is approved, keep the additions append-only:

```json
{
  "SHADOW_ONLY": true,
  "ts_utc": "2026-05-28T00:00:00Z",
  "slug": "btc-updown-5m-...",
  "checkpoint_time": "T+120",
  "remaining_time_sec": 60.0,
  "remaining_bucket": "T-120~60",
  "clob_fetch_latency_ms": 320.5,
  "latency_spike": false,
  "latency_spike_threshold_ms": 1000,
  "latency_risk": "SAFE"
}
```

Minimum required new fields:

| Field | Purpose |
| --- | --- |
| `remaining_time_sec` | Enables near-expiry tail latency analysis |
| `remaining_bucket` | Normalized bucket for checkpoint grouping |
| `latency_spike` | Boolean flag for threshold breach |
| `latency_spike_threshold_ms` | Makes historical interpretation explicit |
| `latency_risk` | `SAFE`, `WARNING`, or `DANGEROUS` |

If code changes are not approved, these can be derived offline from existing fields:

- `clob_fetch_latency_ms`
- `remaining_time_hours`
- `ts_utc`

### Spike Definition

Use two layers: absolute latency and near-expiry adjusted latency.

Global spike:

- `latency_spike = clob_fetch_latency_ms > 1000`

Near-expiry adjusted spike:

| Bucket | Spike threshold |
| --- | --- |
| T-300~120 | > 1500ms |
| T-120~60 | > 1000ms |
| T-60~30 | > 750ms |
| T-30~0 | > 500ms |

Rationale: a 900ms delay may be acceptable with 4 minutes remaining, but dangerous in the final 30 seconds.

### Consecutive Spike Definition

A spike sequence is a run of shadow events where:

1. Each event is in the same or adjacent near-expiry bucket.
2. Each event breaches the bucket-specific spike threshold.
3. The time gap between adjacent events is <= 5 seconds.

This avoids treating isolated events several minutes apart as one continuous stall.

### Spike Duration Calculation

Offline calculation:

1. Sort events by `ts_utc`.
2. Mark each event as spike/non-spike using the bucket-specific threshold.
3. Group consecutive spike events where the next event occurs within 5 seconds.
4. For each group:
   - `duration = last_event_ts - first_event_ts`
   - If the group contains one event, duration is `0s`.
5. Report:
   - spike count
   - longest spike duration
   - total spike duration

No runtime state is required.

### Latency Risk Levels

Per event:

| Status | Definition |
| --- | --- |
| SAFE | latency <= 50% of bucket threshold |
| WARNING | latency > 50% and <= bucket threshold |
| DANGEROUS | latency > bucket threshold |

Per bucket:

| Status | Definition |
| --- | --- |
| SAFE | p99 <= bucket threshold and max <= 2x threshold and longest spike duration <= 2s |
| WARNING | p99 <= 1.5x threshold or max <= 3x threshold or longest spike duration <= 10s |
| DANGEROUS | p99 > 1.5x threshold or max > 3x threshold or longest spike duration > 10s |

### Minimal Checkpoint Display

```text
Latency Tail
bucket       N    p50_ms  p95_ms  p99_ms  max_ms  spikes>threshold  longest_spike_s  status
ALL          300  302     358     690     1100    1                 0                SAFE
T-300~120    75   300     350     520     700     0                 0                SAFE
T-120~60     75   305     360     720     950     0                 0                SAFE
T-60~30      75   315     420     780     1200    2                 4                WARNING
T-30~0       75   340     520     1300    2200    8                 14               DANGEROUS
```

Checkpoint should not make live decisions. It should only report:

```text
latency_tail_status: SAFE/WARNING/DANGEROUS
live_permission: NO
```

## Part 2: Near-Expiry Bucketization

### Objective

Current global fillability can hide whether the edge only works in a narrow and fragile near-expiry window. Bucketization answers whether execution quality is stable across remaining-time windows.

### Bucket Schema

Derive from remaining seconds at the time of the shadow event:

```text
remaining_time_sec = market_end_ts - event_ts
```

Buckets:

| Bucket | Condition |
| --- | --- |
| T-300~120 | 300 >= remaining_time_sec > 120 |
| T-120~60 | 120 >= remaining_time_sec > 60 |
| T-60~30 | 60 >= remaining_time_sec > 30 |
| T-30~0 | 30 >= remaining_time_sec >= 0 |
| OUT_OF_SCOPE | remaining_time_sec < 0 or > 300 |

### Minimal JSONL Incremental Fields

```json
{
  "SHADOW_ONLY": true,
  "ts_utc": "2026-05-28T00:00:00Z",
  "slug": "btc-updown-5m-...",
  "market_end_ts": 1779925800,
  "remaining_time_sec": 60.0,
  "remaining_bucket": "T-120~60",
  "clob_orderbook_available": true,
  "fallback_used": false,
  "spread": 0.01,
  "side_specific_depth_top1": 57.9,
  "side_specific_depth_top3": 1876.9,
  "side_specific_depth_top5": 4825.8,
  "executable_10": true,
  "executable_25": true,
  "executable_50": true,
  "clob_fetch_latency_ms": 299.2
}
```

Minimum new fields:

| Field | Why |
| --- | --- |
| `remaining_time_sec` | Precise bucket derivation |
| `remaining_bucket` | Stable grouping label |

All other required metrics already exist in shadow events or can be derived from existing fields.

### Per-Bucket Metrics

For each bucket:

| Metric | Definition |
| --- | --- |
| N | number of shadow events |
| CLOB availability | `clob_orderbook_available=true` share |
| fallback rate | `fallback_used=true` share |
| executable 10/25/50 | `executable_10/25/50=true` share |
| spread p50/p95 | distribution of `spread` |
| depth top5 p50/p95 | distribution of `side_specific_depth_top5` |
| latency p50/p95/p99/max | distribution of `clob_fetch_latency_ms` |

Optional but useful:

| Metric | Definition |
| --- | --- |
| thin top1 count | `side_specific_depth_top1 < 10 USDC` |
| spread widening count | `spread >= 0.03` |
| degraded fill count | `executable_50=false` while `executable_10=true` |

### Minimal Checkpoint Display

```text
Near-Expiry Buckets
bucket       N    CLOB%   fallback%   exec10%  exec25%  exec50%  spread_p50  spread_p95  depth5_p50  lat_p50  lat_p95  lat_p99  status
T-300~120    70   100.0   0.0         100.0    100.0    100.0    0.01        0.02        1800        300      350      520      HEALTHY
T-120~60     78   100.0   0.0         100.0    100.0    100.0    0.01        0.02        1200        305      360      720      HEALTHY
T-60~30      82   98.0    2.0         100.0    98.0     94.0     0.02        0.04        650         315      420      780      WARNING
T-30~0       70   92.0    8.0         96.0     90.0     82.0     0.03        0.08        240         340      520      1300     NO-TRADE
```

### Bucket Health Classification

HEALTHY:

- N >= 50
- CLOB availability >= 95%
- fallback rate <= 5%
- executable_10 >= 95%
- executable_25 >= 90%
- executable_50 >= 80%
- spread p95 <= 0.03
- latency p99 <= bucket threshold

WARNING:

- N < 50, or
- CLOB availability 90-95%, or
- fallback rate 5-10%, or
- executable_10 90-95%, or
- executable_25 80-90%, or
- executable_50 70-80%, or
- spread p95 0.03-0.05, or
- latency p99 up to 1.5x bucket threshold

NO-TRADE:

- CLOB availability < 90%, or
- fallback rate > 10%, or
- executable_10 < 90%, or
- executable_25 < 80%, or
- executable_50 < 70%, or
- spread p95 > 0.05, or
- latency p99 > 1.5x bucket threshold, or
- longest latency spike duration > 10s.

NO-TRADE here means "not suitable for future live consideration." It does not trigger any runtime behavior.

## Minimal Analysis Flow

Use existing `shadow_execution_events.jsonl` as the source.

1. Load recent N shadow events, default `N=300`.
2. Keep only `SHADOW_ONLY=true`.
3. Derive `remaining_time_sec` if absent:
   - Prefer explicit field if present.
   - Else use `remaining_time_hours * 3600`.
   - Else mark `remaining_bucket=UNKNOWN`.
4. Assign `remaining_bucket`.
5. Compute latency tail metrics.
6. Compute bucket execution metrics.
7. Print checkpoint summary.
8. Keep `live_permission: NO`.

No database or new runtime process is required.

## Future Work

Explicitly deferred:

- Quote lifetime observer.
- Orderbook flicker engine.
- Queue simulation.
- Partial fill simulator.
- Replay engine.
- Dashboard.
- High-frequency orderbook sampling.
- New observer service.

These should only be reconsidered if:

1. p99/max latency is acceptable, and
2. near-expiry bucketization shows a specific bucket is operationally viable, and
3. live-shadow remains promising after more samples.

## Non-Goals

Do not:

- Increase alpha complexity.
- Increase signal count.
- Add market filters to improve reported metrics.
- Change thresholds.
- Change runner behavior.
- Connect accounts.
- Place orders.
- Build a live execution path.

## Phase 2 Exit Criteria

This phase can be considered complete when:

1. At least 300 real-CLOB shadow samples are bucketized.
2. p99/max latency is reported overall and by bucket.
3. Spike count and longest spike duration are reported.
4. Each near-expiry bucket has a HEALTHY/WARNING/NO-TRADE classification.
5. The report clearly states whether the edge is concentrated in a fragile bucket.
6. `live_permission: NO` remains explicit.

