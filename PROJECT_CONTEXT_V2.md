# PROJECT_CONTEXT_V2.md

# Project Name

Polymarket × OKX Quant Research System

---

# Core Philosophy

This is NOT a gambling bot.

This is a quantitative research and market infrastructure project.

Primary goals:
- detect edge
- validate edge statistically
- manage risk
- automate research
- build reusable trading infrastructure

Profitability must be proven with data.

Never assume profitability.

---

# Current Development Stage

Phase 1:
- local-only
- research-only
- paper trading only
- no real-money execution

No automated real trading is allowed during phase 1.

---

# Main Objective

Research the relationship between:

- Polymarket prediction markets
- OKX/Binance crypto markets

Focus areas:
- lag analysis
- probability mispricing
- sentiment divergence
- volatility-driven edge
- event-driven inefficiency

---

# Important Discovery

Pure latency arbitrage is highly competitive.

Professional HFT bots dominate:
- millisecond execution
- colocated infra
- optimized networking

This project should NOT initially compete directly in ultra-low-latency HFT.

Instead focus on:
- medium-frequency opportunities
- probability mispricing
- sentiment inefficiency
- structural edge
- volatility mismatch

---

# Why This Project Exists

Retail traders usually fail because they:
- trade emotionally
- lack data
- lack risk management
- lack infrastructure
- lack journaling
- lack statistical validation

This project aims to build:
AI-native quantitative infrastructure.

---

# Long-Term Architecture

Future architecture:

Research System
↓
Hermes Strategy Plugin
↓
Hermes Risk Layer
↓
Execution Layer
↓
Exchange APIs

---

# Hermes Relationship

Hermes is NOT replaced.

Hermes remains:
- orchestration layer
- risk controller
- execution manager
- journaling system
- monitoring framework

This project may later become:
a Hermes strategy plugin.

---

# AI Collaboration Structure

ChatGPT:
- architecture
- strategy research
- risk management
- system design
- logic review

Claude Code / Codex:
- engineering
- implementation
- Docker
- APIs
- async systems
- refactoring

The project should be designed for:
multi-AI collaboration.

---

# Security Boundary

ALL operations MUST stay inside:

/Users/libo/polymarket_okx

Never:
- access unrelated directories
- read sensitive system files
- handle private keys
- execute withdrawals
- access browser credentials

---

# Forbidden Actions

Forbidden during phase 1:
- real-money automated trading
- leverage trading
- martingale
- unlimited averaging down
- browser automation
- Selenium
- GUI clicking
- auto-wallet operations

---

# Technical Stack

Python 3.11

Core libraries:
- asyncio
- websockets
- ccxt
- aiohttp
- pandas
- numpy
- sqlalchemy

Infrastructure:
- Docker Compose
- SQLite initially
- optional Postgres later

Optional:
- FastAPI dashboard
- Redis cache

---

# Project Goals

1. Detect lag between:
   - OKX/Binance
   - Polymarket

2. Measure lag statistically.

3. Determine whether:
   edge survives fees/slippage.

4. Build reusable research infrastructure.

5. Validate paper trading profitability.

---

# Research Priorities

Priority 1:
Lag recording.

Priority 2:
Probability mispricing.

Priority 3:
Sentiment divergence.

Priority 4:
Volatility/event-driven edge.

---

# Important Strategic Insight

Do NOT rely on:
"AI predicts BTC direction."

Instead:
focus on measurable inefficiencies.

The system should behave like:
a quantitative research desk.

---

# Core Modules

## okx_ws.py

Responsibilities:
- WebSocket connection
- price streaming
- funding rate
- order book
- candle updates

Assets:
- BTC
- ETH
- SOL

---

## polymarket_client.py

Fetch:
- markets
- YES/NO prices
- liquidity
- volume
- expiry
- order books

Focus on:
short-term crypto-related markets.

---

## market_mapper.py

Map:
Polymarket market
↔
OKX/Binance asset

Examples:
- BTC direction
- ETH target price
- time-window markets

---

## lag_recorder.py

Record:
- exchange move timestamp
- prediction market response timestamp
- lag_ms

Store all events.

This is critical.

---

## edge_detector.py

Calculate:
- implied probability
- derived probability
- edge
- confidence score

---

## risk_filter.py

Reject:
- low liquidity
- extreme spread
- unclear rules
- near-expiry markets
- manipulation-prone markets

---

## paper_trader.py

Paper trading only.

Record:
- entry
- exit
- pnl
- fees
- slippage
- outcome

---

## notifier.py

Telegram notifications:
- high-confidence opportunities
- errors
- reports

Do NOT spam low-quality signals.

---

## report.py

Generate:
- lag distribution
- win rate
- simulated pnl
- edge statistics
- market rankings

---

# Database Design

SQLite initially.

Suggested tables:

- market_snapshots
- lag_records
- signals
- paper_trades
- daily_reports
- execution_logs

---

# Logging Requirements

Every signal MUST include:
- timestamp
- exchange price
- prediction price
- spread
- liquidity
- reason
- confidence
- outcome

Nothing should be hidden.

---

# Risk Management Philosophy

Primary goal:
survival.

Rules:
- max simulated risk 1%-2%
- avoid oversized exposure
- no revenge trading
- reject poor liquidity
- reject unclear markets

---

# Important Quant Principle

Most apparent edges disappear after:
- fees
- slippage
- failed execution
- latency
- spread

The system MUST validate:
net profitability.

---

# Paper Trading Philosophy

Before any real-money deployment:
- run continuously
- collect data
- validate statistically
- analyze failures

Minimum:
7 consecutive days of positive simulated performance.

---

# AI Agent Rules

Coding agents should:
- remain modular
- avoid hardcoded secrets
- use config files
- support Docker
- support async architecture
- log extensively

---

# Deliverables

Required deliverables:

- runnable source code
- requirements.txt
- docker-compose.yml
- README.md
- sample configs
- SQLite schema
- setup instructions

---

# Command Examples

python main.py scan

python main.py paper

python main.py report

---

# Immediate Development Plan

Step 1:
build project structure.

Step 2:
connect OKX WebSocket.

Step 3:
connect Polymarket API.

Step 4:
record lag data.

Step 5:
analyze lag distribution.

Step 6:
paper trading.

Step 7:
evaluate profitability.

---

# Current Priority

Do NOT optimize profit first.

Optimize:
- stability
- data quality
- logging
- reproducibility
- statistical validity

---

# Long-Term Vision

Build:
AI-assisted quantitative research infrastructure.

Not:
a simple crypto gambling bot.