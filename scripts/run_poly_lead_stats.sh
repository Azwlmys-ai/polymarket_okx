#!/usr/bin/env bash
# scripts/run_poly_lead_stats.sh
#
# Run the Polymarket → OKX lead signal statistics engine.
# STATS_ONLY / DRY_RUN — No real orders. No capital at risk.
#
# Usage:
#   ./scripts/run_poly_lead_stats.sh               # 1-hour default
#   ./scripts/run_poly_lead_stats.sh 7200          # 2 hours
#   POLY_JUMP_10S=0.05 ./scripts/run_poly_lead_stats.sh  # tighter threshold
#
set -euo pipefail

DURATION="${1:-3600}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  POLY_LEAD_STATS — STATS_ONLY / DRY_RUN"
echo "  Duration: ${DURATION}s"
echo "  Thresholds: JUMP_10S=${POLY_JUMP_10S:-0.03}  JUMP_30S=${POLY_JUMP_30S:-0.05}  JUMP_60S=${POLY_JUMP_60S:-0.08}"
echo "  Filters:   MIN_LIQ=${MIN_LIQUIDITY:-1000}  YES=[${MIN_PRICE:-0.35},${MAX_PRICE:-0.75}]"
echo "  Output:    POLY_LEAD_STATS_REPORT.md"
echo "  No real orders. No wallet access."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

DISABLE_SSL_VERIFY=1 \
STATS_ONLY=1 \
REAL_ORDER=0 \
POLY_JUMP_10S="${POLY_JUMP_10S:-0.03}" \
POLY_JUMP_30S="${POLY_JUMP_30S:-0.05}" \
POLY_JUMP_60S="${POLY_JUMP_60S:-0.08}" \
MIN_LIQUIDITY="${MIN_LIQUIDITY:-1000}" \
MIN_PRICE="${MIN_PRICE:-0.35}" \
MAX_PRICE="${MAX_PRICE:-0.75}" \
  .venv/bin/python3 -m src.poly_lead_stats \
    --duration "$DURATION" \
    --log poly_lead_stats.log \
    --report POLY_LEAD_STATS_REPORT.md
