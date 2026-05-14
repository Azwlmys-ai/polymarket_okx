"""
paper_trader.py — local paper trading simulation (Phase 1 only).

Reads lag_records and Polymarket market_snapshots from SQLite and simulates
hypothetical YES-side trades using a fixed baseline rule.

THIS IS A SIMULATION, NOT A TRADING RECOMMENDATION.
No real money is involved.  No API keys, no wallet signing, no order placement.

Baseline rule (MVP):
  - Trigger: a lag_record with a valid prediction_price_after (0 < p < 1)
    AND an upward OKX price move (exchange_price_after > exchange_price_before).
  - Direction: YES-side only.  Down/flat moves are skipped.
  - Entry: prediction_price_after + slippage.
  - Exit: first Polymarket snapshot for the same market_id found after
    (entry_ts + hold_window_ms), or mark as open_no_exit.
  - Risk: max_risk_pct of remaining cash per trade; hard cap at 2%; no all-in.
  - Skip: if OKX move is not upward, entry price out of (0,1), or cash < min.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_SIMULATION_DISCLAIMER = (
    "SIMULATION DISCLAIMER: All paper trades are hypothetical local simulations. "
    "They are NOT trading recommendations, NOT proof of profitability, and do NOT "
    "reflect real-market execution costs, liquidity, or outcomes.  No real money "
    "is involved.  Phase-1 rules forbid real trading."
)

# Status codes written to paper_trades.status
STATUS_CLOSED = "closed"                          # hold window expired; exit snapshot found
STATUS_CLOSED_STOP_LOSS = "closed_stop_loss"      # stop loss triggered within hold window
STATUS_OPEN_NO_EXIT = "open_no_exit"              # hold window passed but no snapshot found
STATUS_SKIPPED_INVALID_PRICE = "skipped_invalid_price"
STATUS_SKIPPED_DOWN_MOVE = "skipped_down_move"          # OKX move not upward; YES baseline skips
STATUS_SKIPPED_UNKNOWN_DIRECTION = "skipped_unknown_direction"  # OKX prices missing; direction unknown
STATUS_SKIPPED_RISK = "skipped_risk"              # config validation failure
STATUS_SKIPPED_NO_CASH = "skipped_no_cash"        # notional below min_notional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SimConfig:
    """
    Tunable parameters for the paper-trading simulation.

    Hard safety limits (enforced in __post_init__):
      - initial_cash > 0
      - 0 < max_risk_pct <= 0.02  (hard cap at 2% — no all-in)
      - slippage_pct >= 0
      - fee_pct >= 0
      - hold_window_ms > 0
    """
    initial_cash: float = 100.0       # USDC
    max_risk_pct: float = 0.02        # 2% of remaining cash per trade (hard cap)
    slippage_pct: float = 0.002       # 0.2% added to entry price
    fee_pct: float = 0.001            # 0.1% of notional as fees
    hold_window_ms: int = 300_000     # 5 minutes
    min_notional: float = 0.01        # minimum trade size in USDC
    stop_loss_yes_price: float = 0.40 # close immediately if YES price drops to/below this

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError(
                f"initial_cash must be > 0 (got {self.initial_cash})"
            )
        if not (0 < self.max_risk_pct <= 0.02):
            raise ValueError(
                f"max_risk_pct must be in (0, 0.02] to prevent all-in "
                f"(got {self.max_risk_pct}; hard cap is 2%)"
            )
        if self.slippage_pct < 0:
            raise ValueError(
                f"slippage_pct must be >= 0 (got {self.slippage_pct})"
            )
        if self.fee_pct < 0:
            raise ValueError(
                f"fee_pct must be >= 0 (got {self.fee_pct})"
            )
        if self.hold_window_ms <= 0:
            raise ValueError(
                f"hold_window_ms must be > 0 (got {self.hold_window_ms})"
            )
        if not (0.0 < self.stop_loss_yes_price < 1.0):
            raise ValueError(
                f"stop_loss_yes_price must be in (0, 1) (got {self.stop_loss_yes_price})"
            )


# ---------------------------------------------------------------------------
# SimTrade — internal result object (maps directly to paper_trades schema)
# ---------------------------------------------------------------------------

@dataclass
class SimTrade:
    """One simulated paper trade attempt."""
    opened_ts_ms: int
    closed_ts_ms: int | None
    market_id: str
    asset: str
    side: str                    # always "YES" in MVP baseline
    entry_price: float
    exit_price: float | None
    notional: float
    quantity: float
    fees: float
    slippage_cost: float
    pnl: float | None            # None if trade is not closed
    status: str
    reason: str


# ---------------------------------------------------------------------------
# Pure calculation helpers (no I/O — fully unit-testable)
# ---------------------------------------------------------------------------

def compute_entry_price(
    prediction_price_after: float,
    slippage_pct: float,
) -> float | None:
    """
    Compute the simulated entry price after adding slippage.

    Returns None if the resulting price is not strictly within (0, 1) —
    those prices are not valid for a binary prediction market.
    """
    price = prediction_price_after + (prediction_price_after * slippage_pct)
    if price <= 0.0 or price >= 1.0:
        return None
    return price


def compute_notional(remaining_cash: float, max_risk_pct: float) -> float:
    """
    Compute the notional (USDC) to risk on a single trade.

    Raises ValueError if max_risk_pct is outside (0, 0.02] — this prevents
    all-in sizing even when called directly without a SimConfig.
    Returns 0.0 if remaining_cash is non-positive (no overdraft).
    """
    if max_risk_pct <= 0 or max_risk_pct > 0.02:
        raise ValueError(
            f"max_risk_pct must be in (0, 0.02] to prevent all-in sizing "
            f"(got {max_risk_pct}; hard cap is 2%)"
        )
    if remaining_cash <= 0.0:
        return 0.0
    return remaining_cash * max_risk_pct


def compute_quantity(notional: float, entry_price: float) -> float:
    """
    Compute the number of YES-token units purchasable at entry_price.

    Returns 0 if entry_price is zero or negative.
    """
    if entry_price <= 0.0:
        return 0.0
    return notional / entry_price


def compute_fees(notional: float, fee_pct: float) -> float:
    """Compute the flat percentage fee on the notional amount."""
    return notional * fee_pct


def compute_pnl(
    quantity: float,
    entry_price: float,
    exit_price: float | None,
    fees: float,
) -> float | None:
    """
    Compute realised PnL: (exit - entry) * quantity - fees.

    Returns None if exit_price is None (trade not closed).
    """
    if exit_price is None:
        return None
    return (exit_price - entry_price) * quantity - fees


def find_exit_snapshot(
    market_id: str,
    opened_ts_ms: int,
    hold_window_ms: int,
    poly_snaps: list[dict],
) -> dict | None:
    """
    Find the first Polymarket snapshot for *market_id* after the hold window.

    *poly_snaps* must be sorted by ts_ms ascending.
    Returns the snapshot dict or None if none found.
    """
    cutoff = opened_ts_ms + hold_window_ms
    for snap in poly_snaps:
        if snap.get("market_id") != market_id:
            continue
        if snap["ts_ms"] > cutoff:
            return snap
    return None


def find_stop_loss_snapshot(
    market_id: str,
    opened_ts_ms: int,
    hold_window_ms: int,
    stop_loss_yes_price: float,
    poly_snaps: list[dict],
) -> dict | None:
    """
    Find the first Polymarket snapshot for *market_id* within the hold window
    where the YES price (last) has dropped to or below *stop_loss_yes_price*.

    *poly_snaps* must be sorted by ts_ms ascending.
    Returns the first matching snapshot or None if the price never hit the level.
    """
    cutoff = opened_ts_ms + hold_window_ms
    for snap in poly_snaps:
        ts = snap["ts_ms"]
        if ts > cutoff:
            break  # sorted; nothing more within window
        if ts <= opened_ts_ms:
            continue  # at or before entry — skip
        if snap.get("market_id") != market_id:
            continue
        last = snap.get("last")
        if last is not None and float(last) <= stop_loss_yes_price:
            return snap
    return None


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------

def simulate_trade(
    lag_row: dict,
    poly_snaps_sorted: list[dict],
    remaining_cash: float,
    cfg: SimConfig,
) -> SimTrade:
    """
    Attempt to simulate one paper trade from a single lag_record row.

    Returns a SimTrade with the appropriate status.  Never raises.
    """
    market_id: str = str(lag_row.get("market_id", ""))
    asset: str = str(lag_row.get("asset", "unknown"))
    opened_ts_ms: int = int(lag_row.get("prediction_response_ts_ms", 0))

    # --- YES-only baseline: require a known upward OKX move ---
    price_before = lag_row.get("exchange_price_before")
    price_after_okx = lag_row.get("exchange_price_after")

    if price_before is None or price_after_okx is None:
        # Direction unknown — skip rather than assume upward.
        return SimTrade(
            opened_ts_ms=opened_ts_ms,
            closed_ts_ms=None,
            market_id=market_id,
            asset=asset,
            side="YES",
            entry_price=0.0,
            exit_price=None,
            notional=0.0,
            quantity=0.0,
            fees=0.0,
            slippage_cost=0.0,
            pnl=None,
            status=STATUS_SKIPPED_UNKNOWN_DIRECTION,
            reason=(
                "OKX move direction unknown "
                f"(exchange_price_before={price_before}, "
                f"exchange_price_after={price_after_okx}); "
                "YES-only baseline requires confirmed upward move"
            ),
        )

    if float(price_after_okx) <= float(price_before):
        return SimTrade(
            opened_ts_ms=opened_ts_ms,
            closed_ts_ms=None,
            market_id=market_id,
            asset=asset,
            side="YES",
            entry_price=0.0,
            exit_price=None,
            notional=0.0,
            quantity=0.0,
            fees=0.0,
            slippage_cost=0.0,
            pnl=None,
            status=STATUS_SKIPPED_DOWN_MOVE,
            reason=(
                f"OKX move not upward "
                f"(before={price_before}, after={price_after_okx}); "
                "YES-only baseline skips down/flat moves"
            ),
        )

    raw_price = lag_row.get("prediction_price_after")

    # --- Validate prediction price ---
    if raw_price is None or not (0.0 < float(raw_price) < 1.0):
        return SimTrade(
            opened_ts_ms=opened_ts_ms,
            closed_ts_ms=None,
            market_id=market_id,
            asset=asset,
            side="YES",
            entry_price=float(raw_price) if raw_price is not None else 0.0,
            exit_price=None,
            notional=0.0,
            quantity=0.0,
            fees=0.0,
            slippage_cost=0.0,
            pnl=None,
            status=STATUS_SKIPPED_INVALID_PRICE,
            reason="prediction_price_after missing or out of (0,1)",
        )

    entry_price = compute_entry_price(float(raw_price), cfg.slippage_pct)
    if entry_price is None:
        return SimTrade(
            opened_ts_ms=opened_ts_ms,
            closed_ts_ms=None,
            market_id=market_id,
            asset=asset,
            side="YES",
            entry_price=0.0,
            exit_price=None,
            notional=0.0,
            quantity=0.0,
            fees=0.0,
            slippage_cost=0.0,
            pnl=None,
            status=STATUS_SKIPPED_INVALID_PRICE,
            reason="entry_price after slippage out of (0,1)",
        )

    # --- Risk sizing ---
    notional = compute_notional(remaining_cash, cfg.max_risk_pct)
    if notional < cfg.min_notional:
        return SimTrade(
            opened_ts_ms=opened_ts_ms,
            closed_ts_ms=None,
            market_id=market_id,
            asset=asset,
            side="YES",
            entry_price=entry_price,
            exit_price=None,
            notional=0.0,
            quantity=0.0,
            fees=0.0,
            slippage_cost=0.0,
            pnl=None,
            status=STATUS_SKIPPED_NO_CASH,
            reason=f"notional {notional:.4f} below min_notional {cfg.min_notional}",
        )

    quantity = compute_quantity(notional, entry_price)
    fees = compute_fees(notional, cfg.fee_pct)
    slippage_cost = notional * cfg.slippage_pct

    # --- Stop loss check (first YES-price drop within hold window) ---
    sl_snap = find_stop_loss_snapshot(
        market_id, opened_ts_ms, cfg.hold_window_ms,
        cfg.stop_loss_yes_price, poly_snaps_sorted,
    )
    if sl_snap is not None:
        sl_price: float | None = sl_snap.get("last")
        sl_ts: int = int(sl_snap["ts_ms"])
        return SimTrade(
            opened_ts_ms=opened_ts_ms,
            closed_ts_ms=sl_ts,
            market_id=market_id,
            asset=asset,
            side="YES",
            entry_price=entry_price,
            exit_price=sl_price,
            notional=notional,
            quantity=quantity,
            fees=fees,
            slippage_cost=slippage_cost,
            pnl=compute_pnl(quantity, entry_price, sl_price, fees),
            status=STATUS_CLOSED_STOP_LOSS,
            reason="stop_loss_yes_price",
        )

    # --- Find exit snapshot (hold window expiry) ---
    exit_snap = find_exit_snapshot(market_id, opened_ts_ms, cfg.hold_window_ms, poly_snaps_sorted)

    if exit_snap is None:
        return SimTrade(
            opened_ts_ms=opened_ts_ms,
            closed_ts_ms=None,
            market_id=market_id,
            asset=asset,
            side="YES",
            entry_price=entry_price,
            exit_price=None,
            notional=notional,
            quantity=quantity,
            fees=fees,
            slippage_cost=slippage_cost,
            pnl=None,
            status=STATUS_OPEN_NO_EXIT,
            reason="no Polymarket snapshot found after hold window",
        )

    exit_price: float | None = exit_snap.get("last")
    closed_ts_ms: int = int(exit_snap["ts_ms"])
    pnl = compute_pnl(quantity, entry_price, exit_price, fees)

    return SimTrade(
        opened_ts_ms=opened_ts_ms,
        closed_ts_ms=closed_ts_ms,
        market_id=market_id,
        asset=asset,
        side="YES",
        entry_price=entry_price,
        exit_price=exit_price,
        notional=notional,
        quantity=quantity,
        fees=fees,
        slippage_cost=slippage_cost,
        pnl=pnl,
        status=STATUS_CLOSED,
        reason="hold_window_expired",
    )


def run_paper_simulation(
    lag_rows: list[dict],
    poly_snaps: list[dict],
    cfg: SimConfig | None = None,
) -> tuple[list[SimTrade], float]:
    """
    Simulate all paper trades from *lag_rows*.

    *poly_snaps* should be all Polymarket market_snapshots rows, sorted by
    ts_ms ascending.  Cash accounting is applied in insertion order.

    Returns (trades, final_cash).
    """
    if cfg is None:
        cfg = SimConfig()

    cash = cfg.initial_cash
    trades: list[SimTrade] = []

    # Sort lag rows by exchange_move_ts_ms to process in chronological order
    sorted_rows = sorted(lag_rows, key=lambda r: r.get("exchange_move_ts_ms", 0))

    for row in sorted_rows:
        trade = simulate_trade(row, poly_snaps, cash, cfg)
        trades.append(trade)

        # Cash flow for executed (non-skipped) trades:
        #   CLOSED / CLOSED_STOP_LOSS → net effect = trade.pnl (fees already deducted)
        #   OPEN_NO_EXIT              → net effect = -(notional + fees); no recovery
        if trade.status in (STATUS_CLOSED, STATUS_CLOSED_STOP_LOSS) and trade.pnl is not None:
            cash += trade.pnl
        elif trade.status == STATUS_OPEN_NO_EXIT:
            cash -= (trade.notional + trade.fees)

        logger.debug(
            "SimTrade %s  status=%s  notional=%.4f  pnl=%s  cash_remaining=%.4f",
            trade.market_id[:40],
            trade.status,
            trade.notional,
            f"{trade.pnl:.4f}" if trade.pnl is not None else "—",
            cash,
        )

    logger.info(
        "Paper simulation complete: %d trade(s), final_cash=%.4f",
        len(trades),
        cash,
    )
    return trades, cash


# ---------------------------------------------------------------------------
# Summary formatting
# ---------------------------------------------------------------------------

def _max_drawdown(pnls: list[float]) -> float:
    """Max peak-to-trough drop in cumulative PnL for a chronological sequence."""
    cumulative = peak = max_dd = 0.0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    return max_dd


def format_summary(trades: list[SimTrade], final_cash: float, initial_cash: float) -> str:
    """Render a concise paper-trading summary for console output."""
    lines: list[str] = []
    sep = "─" * 60

    lines.append(sep)
    lines.append("  PAPER TRADING SIMULATION SUMMARY")
    lines.append("  (This is a local simulation, NOT a trading recommendation)")
    lines.append(sep)

    total = len(trades)
    closed = [t for t in trades if t.status == STATUS_CLOSED]
    stop_loss = [t for t in trades if t.status == STATUS_CLOSED_STOP_LOSS]
    no_exit = [t for t in trades if t.status == STATUS_OPEN_NO_EXIT]
    skipped = [t for t in trades if t.status not in (
        STATUS_CLOSED, STATUS_CLOSED_STOP_LOSS, STATUS_OPEN_NO_EXIT
    )]
    all_closed = closed + stop_loss

    lines.append(f"  Total trade attempts : {total}")
    lines.append(f"  Closed (hold_window) : {len(closed)}")
    lines.append(f"  Closed (stop_loss)   : {len(stop_loss)}")
    lines.append(f"  Open / no exit       : {len(no_exit)}")
    lines.append(f"  Skipped              : {len(skipped)}")
    lines.append("")

    if all_closed:
        pnls = [t.pnl for t in all_closed if t.pnl is not None]
        if pnls:
            wins = sum(1 for p in pnls if p > 0)
            total_pnl = sum(pnls)
            lines.append(f"  Closed-trade PnL     : {total_pnl:+.4f} USDC (simulated)")
            lines.append(f"  Win rate             : {wins}/{len(pnls)}")
            lines.append(f"  Avg PnL per trade    : {total_pnl/len(pnls):+.4f} USDC")
        lines.append("")

    lines.append(f"  Initial cash         : {initial_cash:.4f} USDC (simulated)")
    lines.append(f"  Final cash           : {final_cash:.4f} USDC (simulated)")
    net = final_cash - initial_cash
    lines.append(f"  Net change           : {net:+.4f} USDC (simulated)")
    lines.append("")

    # open_no_exit cash note — explain why net cash can appear paradoxically low
    if no_exit:
        open_notional = sum(t.notional for t in no_exit)
        open_fees = sum(t.fees for t in no_exit)
        lines.append("  NOTE — open / no-exit positions:")
        lines.append(
            f"    {len(no_exit)} simulated position(s) were opened but no Polymarket"
        )
        lines.append(
            "    exit snapshot was found within the hold window.  Their notional"
        )
        lines.append(
            f"    ({open_notional:.4f} USDC) and fees ({open_fees:.4f} USDC) have"
        )
        lines.append(
            "    been deducted from simulated cash and are unrecovered.  These"
        )
        lines.append(
            "    positions are EXCLUDED from closed-trade PnL metrics above."
        )
        lines.append(
            "    If this makes the net change appear negative, it reflects"
        )
        lines.append(
            "    unrealised simulated exposure — not a real loss."
        )
        lines.append("")

    # Stop-loss & close breakdown stats
    sl_pnls = [t.pnl for t in stop_loss if t.pnl is not None]
    sl_total_pnl = sum(sl_pnls) if sl_pnls else 0.0
    all_pnls = [t.pnl for t in all_closed if t.pnl is not None]
    large_losses = sum(1 for p in all_pnls if p < -5.0)
    sorted_closed = sorted(all_closed, key=lambda t: t.opened_ts_ms)
    max_dd = _max_drawdown([t.pnl for t in sorted_closed if t.pnl is not None])
    lines.append("  STOP-LOSS & CLOSE BREAKDOWN")
    lines.append(f"  Stop-loss triggers   : {len(stop_loss)}")
    lines.append(f"  Stop-loss total PnL  : {sl_total_pnl:+.4f} USDC (simulated)")
    lines.append(f"  Hold-window closes   : {len(closed)}")
    lines.append(f"  Large losses >5 USDC : {large_losses}")
    lines.append(f"  Max drawdown (sim)   : {max_dd:.4f} USDC (simulated)")
    lines.append("")

    # Skip reason breakdown
    if skipped:
        reasons: dict[str, int] = {}
        for t in skipped:
            reasons[t.status] = reasons.get(t.status, 0) + 1
        lines.append("  Skip reasons:")
        for status, count in sorted(reasons.items()):
            lines.append(f"    {status}: {count}")
        lines.append("")

    lines.append(sep)
    lines.append(f"  {_SIMULATION_DISCLAIMER}")
    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SQLite I/O helpers
# ---------------------------------------------------------------------------

def load_lag_rows(db_path: str | Path) -> list[dict]:
    """Load all lag_records rows as plain dicts, sorted by exchange_move_ts_ms."""
    path = str(db_path)
    rows: list[dict] = []
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT ts_ms, exchange_source, prediction_source, asset, market_id,
                       exchange_move_ts_ms, prediction_response_ts_ms, lag_ms,
                       exchange_price_before, exchange_price_after,
                       prediction_price_before, prediction_price_after, notes
                FROM lag_records
                ORDER BY exchange_move_ts_ms ASC
                """
            )
            for row in cur:
                rows.append(dict(row))
    except sqlite3.OperationalError as exc:
        logger.warning("Could not load lag_records: %s", exc)
    logger.info("Loaded %d lag row(s) for paper simulation", len(rows))
    return rows


def load_poly_snapshots(db_path: str | Path) -> list[dict]:
    """Load all Polymarket market_snapshots rows as plain dicts, sorted by ts_ms."""
    path = str(db_path)
    rows: list[dict] = []
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT ts_ms, source, market_id, symbol, last
                FROM market_snapshots
                WHERE source = 'polymarket'
                ORDER BY ts_ms ASC
                """
            )
            for row in cur:
                rows.append(dict(row))
    except sqlite3.OperationalError as exc:
        logger.warning("Could not load Polymarket snapshots: %s", exc)
    logger.info("Loaded %d Polymarket snapshot(s) for exit search", len(rows))
    return rows


def insert_paper_trades(db_path: str | Path, trades: list[SimTrade]) -> int:
    """
    Persist SimTrade objects to the paper_trades SQLite table.
    Only inserts trades that were actually executed (closed or open_no_exit).
    Returns the number of rows inserted.
    """
    executable = [t for t in trades if t.status in (STATUS_CLOSED, STATUS_CLOSED_STOP_LOSS, STATUS_OPEN_NO_EXIT)]
    if not executable:
        return 0
    path = str(db_path)
    with sqlite3.connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO paper_trades
                (opened_ts_ms, closed_ts_ms, market_id, asset, side,
                 entry_price, exit_price, notional, quantity,
                 fees, slippage, pnl, status, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    t.opened_ts_ms,
                    t.closed_ts_ms,
                    t.market_id,
                    t.asset,
                    t.side,
                    t.entry_price,
                    t.exit_price,
                    t.notional,
                    t.quantity,
                    t.fees,
                    t.slippage_cost,
                    t.pnl,
                    t.status,
                    t.reason,
                )
                for t in executable
            ],
        )
        conn.commit()
    logger.info("Inserted %d paper trade(s) into %s", len(executable), path)
    return len(executable)


def run_paper_trading(
    db_path: str | Path,
    cfg: SimConfig | None = None,
) -> tuple[list[SimTrade], float]:
    """
    Full paper trading pipeline:
      1. Load lag_records and Polymarket snapshots from SQLite.
      2. Simulate trades.
      3. Persist executed trades to paper_trades table.

    Returns (all_trades, final_cash).
    """
    if cfg is None:
        cfg = SimConfig()

    lag_rows = load_lag_rows(db_path)
    poly_snaps = load_poly_snapshots(db_path)

    if not lag_rows:
        logger.warning("No lag records found. Run 'lag' first.")

    trades, final_cash = run_paper_simulation(lag_rows, poly_snaps, cfg)
    insert_paper_trades(db_path, trades)
    return trades, final_cash
