CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    source TEXT NOT NULL,
    market_id TEXT NOT NULL,
    symbol TEXT,
    bid REAL,
    ask REAL,
    mid REAL,
    last REAL,
    liquidity REAL,
    volume_24h REAL,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_snapshots_ts
ON market_snapshots (ts_ms);

CREATE INDEX IF NOT EXISTS idx_market_snapshots_source_market
ON market_snapshots (source, market_id, ts_ms);

CREATE TABLE IF NOT EXISTS lag_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    exchange_source TEXT NOT NULL,
    prediction_source TEXT NOT NULL,
    asset TEXT NOT NULL,
    market_id TEXT NOT NULL,
    exchange_move_ts_ms INTEGER NOT NULL,
    prediction_response_ts_ms INTEGER NOT NULL,
    lag_ms INTEGER NOT NULL,
    exchange_price_before REAL,
    exchange_price_after REAL,
    prediction_price_before REAL,
    prediction_price_after REAL,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_lag_records_asset_ts
ON lag_records (asset, ts_ms);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    market_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    exchange_price REAL NOT NULL,
    prediction_price REAL NOT NULL,
    spread REAL,
    liquidity REAL,
    reason TEXT NOT NULL,
    confidence REAL NOT NULL,
    net_edge REAL,
    outcome TEXT
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_ts_ms INTEGER NOT NULL,
    closed_ts_ms INTEGER,
    market_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    notional REAL NOT NULL,
    quantity REAL NOT NULL,
    fees REAL NOT NULL DEFAULT 0,
    slippage REAL NOT NULL DEFAULT 0,
    pnl REAL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL UNIQUE,
    created_ts_ms INTEGER NOT NULL,
    lag_summary_json TEXT NOT NULL,
    pnl_summary_json TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS execution_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    level TEXT NOT NULL,
    component TEXT NOT NULL,
    message TEXT NOT NULL,
    context_json TEXT
);

