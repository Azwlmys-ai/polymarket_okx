# Wallet 0xe022 Polymarket Data-API Probe

- Execution time: `2026-05-25T09:20:57+00:00`
- Wallet: `0xe0229e10a858860218b6132f4234602c47bd6603`
- Trades endpoint: `https://data-api.polymarket.com/trades?user=0xe0229e10a858860218b6132f4234602c47bd6603&limit=10`
- Positions endpoint: `not requested`

## Results

- Trades verification: ok, HTTP 200, count=10, latency_ms=308.6
- Trades non-empty: `True`
- Positions verification: skipped (trades endpoint was non-empty)
- Positions non-empty: `None`
- Can this address be used directly as Polymarket data-api `user`: `True`
- Trades saved: `10` rows to `research/wallet_0xe022_trades_sample.jsonl`
- Sample contains Bitcoin Up or Down / BTC / 5m text: `True`
- Recommend next collector/enrich/report step: Yes: trades are non-empty and the sample includes BTC/5m-style market text.

## Scope Confirmation

- No orders were placed.
- No live trading connection was made.
- `mvp_runner.py` was not modified.
- Existing VPS systemd services were not modified.
- The running `paper_anchor_sim.py` process/files were not touched by this probe.
- No strategy logic or trading decision logic was added.
