# Wallet 0xe022 Behavior Research Report

- Generated: `2026-05-25T10:38:35+00:00`
- Input: `research/wallet_0xe022_enriched_markets.jsonl`

## Summary

- Sample scale: `116` enriched markets
- BTC 5m candidates: `116`
- confirmed_metadata_count: `116`
- metadata_missing_count: `0`
- STR_confirmed_count: `116`
- STR confirmed rate: `100.0%` (116/116)
- confirmed_flip_rate: `17.2%` (20/116)
- flip_uncertain_count: `93`
- Active exit markets: `0`
- Batch add markets (`buy_count >= 2`): `116`
- Last 60-120 second concentrated entry/add signal: `False` (last 60-120s entries=481/2491, last 0-60s entries=355/2491, markets with >=2 buys=116/116)
- Worth entering VPS shadow test: `NO` (metadata_ok_rate=100.0%, STR_valid=100.0%, confirmed_flip_rate=17.2%, flip_uncertain_rate=80.2%, late_signal=False)

## Entry Bucket Distribution

| Bucket | Count |
|---|---:|
| `0-30` | 161 |
| `30-60` | 194 |
| `60-90` | 241 |
| `90-120` | 240 |
| `120-180` | 508 |
| `180-240` | 558 |
| `240-300` | 579 |
| `out_of_range` | 10 |

## Holding Duration Distribution

| Bucket | Count |
|---|---:|
| `0-30` | 1 |
| `30-60` | 1 |
| `60-120` | 7 |
| `120-180` | 15 |
| `180-300` | 92 |
| `300+` | 0 |
| `unknown` | 0 |

## Batch Add Statistics

- Markets with at least 2 buys: `116`
- Max buy count in a market: `49`
- Total buy size across BTC 5m candidates: `49820.0`
- Total sell size across BTC 5m candidates: `0.0`

## Data Quality

| Bucket | Count |
|---|---:|
| `flip_uncertain` | 93 |
| `ok` | 22 |
| `str_out_of_5m_range` | 10 |

## Metadata Sources

| Bucket | Count |
|---|---:|
| `gamma_events_slug_query` | 115 |
| `gamma_markets_condition_ids` | 1 |

## Metadata Missing Reasons

| Bucket | Count |
|---|---:|

## Notes

- This report is based only on public read-only API data collected by the Phase 2 scripts.
- No orders were placed.
- No live trading connection was made.
- `mvp_runner.py` and the main strategy were not modified.
- Existing VPS systemd services were not modified.
- The running `paper_anchor_sim.py` workflow was not touched.
