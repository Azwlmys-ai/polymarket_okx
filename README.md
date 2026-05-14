# Polymarket × OKX Quant Research System

Phase 1 is local-only research infrastructure for data collection, lag recording, and paper trading.

This project must not perform real-money trading, browser automation, wallet operations, private key handling, or withdrawals.

---

## Setup

Python **3.11** is required.  Create an isolated virtual environment before running anything.

```bash
# 1. Create the venv (run once)
python3.11 -m venv .venv

# 2. Activate it (every new shell session)
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate           # Windows PowerShell

# 3. Install dependencies
pip install -r requirements.txt
```

All commands below assume the venv is **active** (i.e. `.venv/bin/python` is on `PATH`).
You can also prefix every command explicitly with `.venv/bin/python -m ...` if you prefer not to activate.

---

## Commands

### Initialize the database

```bash
python -m src.main init-db
```

Creates `data/research.db` with the full SQLite schema.  Safe to run multiple times.

### Collect market data snapshots

```bash
# OKX only — 30 seconds (default source and duration)
python -m src.main scan

# OKX with custom duration / count
python -m src.main scan --source okx --duration 60
python -m src.main scan --source okx --count 30

# Polymarket only — poll crypto-related markets for 60 s
python -m src.main scan --source polymarket --duration 60

# Both sources — whichever count/duration triggers first
python -m src.main scan --source all --duration 120 --count 50
```

`--source okx` streams BTC-USDT, ETH-USDT, SOL-USDT tickers via the OKX public WebSocket.
`--source polymarket` polls the Polymarket Gamma + CLOB public APIs, filtered by crypto keywords.
All snapshots (both sources) are stored in the `market_snapshots` SQLite table with `source` column.
No API key required for either source.

### Run offline lag recording

```bash
# Analyse existing snapshots and write lag records (default: 0.5% threshold, 60 s window)
python -m src.main lag

# Custom threshold and look-ahead window
python -m src.main lag --threshold 0.003 --max-lag-ms 30000
```

`lag` reads from the `market_snapshots` table (populated by `scan`), detects OKX price
moves above `--threshold`, then finds the first matching Polymarket snapshot within
`--max-lag-ms`.  Results are written to the `lag_records` SQLite table.
Requires both OKX and Polymarket snapshots to already exist in the database.

### Generate a lag distribution report

```bash
# Print report to console (reads lag_records from SQLite)
python -m src.main report

# Also write JSON and Markdown files to reports/
python -m src.main report --output json markdown

# Either format alone
python -m src.main report --output json
python -m src.main report --output markdown
```

Reads from the `lag_records` table (populated by `lag`).  Prints descriptive
statistics: total records, per-asset breakdown, min/median/mean/p90/p95/max
lag_ms, OKX move size summary, and data quality notes.  Output files are
written to `reports/` inside the project folder.

**Important:** The report explicitly states that lag records are not proof of
profitability and are not trading signals.

### Run paper-trading simulation

```bash
# Simulate with defaults (100 USDC cash, 2% risk/trade, 0.2% slippage, 5 min hold)
python -m src.main paper

# Custom parameters
python -m src.main paper --cash 500 --risk 0.01 --slippage 0.003 --hold-ms 600000
```

Reads from `lag_records` and `market_snapshots` (populated by `scan` and `lag`).
Simulates YES-side paper trades using a fixed baseline rule:

- **Upward-only:** Only OKX moves where `exchange_price_after > exchange_price_before`
  trigger a YES trade. Down/flat moves are skipped (`skipped_down_move`).
- **Entry:** `prediction_price_after + slippage` (rejected if result ≥ 1.0).
- **Exit:** First Polymarket snapshot for the same market after the hold window.
- **Hard risk cap:** `--risk` is capped at a maximum of **2% per trade** (`0.02`).
  Values above 2% or ≤ 0 are rejected with an error. No leverage, no all-in.

Results are written to the `paper_trades` SQLite table and a concise summary is
printed.

**Important:** This is a local simulation only.  It is NOT a trading
recommendation and is NOT proof of profitability.

### Show database status

```bash
python -m src.main status
```

Prints a read-only summary of the local SQLite database:

- `market_snapshots` — total count and per-source breakdown with latest timestamp.
- `lag_records` — total count and latest timestamp.
- `paper_trades` — total count and breakdown by status (`closed`, `open_no_exit`, `skipped_*`).

Use this command to quickly check how much data has been collected before running `lag`, `paper`, or `evaluate`.  No writes are made to the database.

### Evaluate paper-trading results

```bash
# Print evaluation report to console (reads paper_trades from SQLite)
python -m src.main evaluate

# Also write JSON and Markdown files to reports/
python -m src.main evaluate --output json markdown

# Either format alone
python -m src.main evaluate --output json
python -m src.main evaluate --output markdown
```

Reads from the `paper_trades` table (populated by `paper`).  Computes descriptive
metrics over **simulated** outcomes:

- Trade count breakdown by status (`closed`, `open_no_exit`, `skipped_*`).
- Simulated gross and net PnL from closed trades (net = gross minus simulated fees).
- Average and median net PnL per closed trade.
- Win/loss count and simulated win rate.
- Max simulated drawdown over the closed-trade sequence (chronological).
- Open/unresolved exposure: total notional and fees for `open_no_exit` trades.
- Data quality caveats: sample size warnings, missing exits, fixed-rate fee/slippage limits.

Output files are written to `reports/` as `paper_eval_<timestamp>.json` and/or
`paper_eval_<timestamp>.md`.

**Important:** All evaluation metrics are hypothetical simulation results.  They are
NOT proof of real profitability, NOT trading recommendations, and do NOT reflect
real-market execution costs, liquidity, or outcomes.  No real money is involved.

---

## Running tests

```bash
python -m pytest tests/ -v
```

---

## Configuration

Copy `.env.example` to `.env` and edit as needed:

```bash
cp .env.example .env
```

Key settings:

| Variable | Default | Description |
|---|---|---|
| `OKX_SYMBOLS` | `BTC-USDT,ETH-USDT,SOL-USDT` | Comma-separated instruments |
| `POLYMARKET_CRYPTO_KEYWORDS` | `BTC,ETH,SOL,bitcoin,ethereum,solana,crypto` | Comma-separated keywords used to filter Polymarket markets by question text (case-insensitive) |
| `DATABASE_URL` | `sqlite:///./data/research.db` | SQLite path |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

Phase-1 safety flags (`ALLOW_REAL_TRADING`, `ALLOW_PRIVATE_KEYS`, etc.) must remain `false`.

---

## Project structure

```
src/
  config.py           Settings (env-driven, frozen dataclass)
  db.py               Schema initializer
  models.py           Pydantic data models
  okx_ws.py               OKX public WebSocket client + parser      ← Step 2
  polymarket_client.py    Polymarket Gamma/CLOB HTTP client + parser ← Step 3
  market_mapper.py        OKX → Polymarket asset keyword mapping     ← Step 4
  lag_recorder.py         Offline lag detection and persistence       ← Step 4
  report.py               Lag distribution report (statistics only)   ← Step 5
  paper_trader.py         Paper trading simulation (local only)       ← Step 6
  evaluator.py            Paper-trading profitability evaluation       ← Step 7
  safety.py               Phase-1 enforcement
  snapshot_store.py       Async SQLite storage for snapshots         ← Step 2
  main.py                 CLI entry point
tests/
  test_config.py
  test_okx_ws.py              ← Step 2
  test_polymarket_client.py   ← Step 3
  test_lag_recorder.py        ← Step 4
  test_report.py              ← Step 5
  test_paper_trader.py        ← Step 6
  test_evaluator.py           ← Step 7
  test_safety.py
  test_snapshot_store.py      ← Step 2
data/research.db      SQLite database (git-ignored)
schema.sql            Table definitions
docker-compose.yml
Dockerfile
```

---

## Current Status

| Step | Description | Status |
|---|---|---|
| 1 | Project initialization | ✅ Approved |
| 2 | OKX public data collection | ✅ Approved |
| 3 | Polymarket public data collection | ✅ Approved |
| 4 | Lag recording | ✅ Approved |
| 5 | Lag distribution report | ✅ Approved |
| 6 | Paper trading | ✅ Approved |
| 7 | Profitability evaluation | ✅ Approved |
| 8 | DB status command | ✅ Implemented |
