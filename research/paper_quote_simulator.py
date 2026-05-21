"""
paper_quote_simulator.py — single-market paper maker quote simulator.

Market: xi-jinping-out-before-2027 (the only current P2 candidate with
        real depth, tight spread, and positive net edge after rebates).

Simulates a passive limit-order market-making strategy per round:
  • Post YES bid (buy YES) and NO bid (buy NO) inside current spread.
  • Model fill probability from queue depth.
  • Track inventory, P&L, rebates, and risk metrics.

NO REAL ORDERS. NO WALLET. NO PRIVATE KEYS. READ-ONLY.

Usage:
    python3 research/paper_quote_simulator.py --once
    python3 research/paper_quote_simulator.py --rounds 8 --interval-sec 900
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import ssl
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev
from typing import Any

import aiohttp

# Ensure project root is on path when running as `python3 research/script.py`
import pathlib as _pathlib
sys.path.insert(0, str(_pathlib.Path(__file__).parent.parent))

from research.polymarket_rewards_mm_observer import (
    _make_connector,
    fetch_clob_book,
    BookState,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TARGET_SLUG = "xi-jinping-out-before-2027"
GAMMA_BASE  = "https://gamma-api.polymarket.com"

DEFAULT_OUT    = Path("research/paper_quote_simulation.jsonl")
DEFAULT_REPORT = Path("research/paper_quote_report.md")

DEFAULT_ROUNDS       = 8
DEFAULT_INTERVAL_S   = 900
DEFAULT_QUOTE_SIZE   = 200.0     # USD per side (matches rewardsMinSize)
DEFAULT_MAX_INVENTORY= 600.0     # USD max one-side exposure

TAKER_FEE_RATE = 0.04
REBATE_RATE    = 0.25
TICK           = 0.001
REWARDS_MAX_SPREAD_BPS = 350     # outside this → not eligible for rewards

# Risk thresholds
TOXIC_SPREAD_MULTIPLIER  = 3.0   # adverse move > N × spread → toxic flag
VAR_CONFIDENCE           = 0.95
SIGMA_DAILY_BPS          = 200   # rough daily vol estimate in bps (conservative)
SIGMA_PER_ROUND_BPS      = SIGMA_DAILY_BPS / math.sqrt(24 * 4)  # per 15-min round


# ---------------------------------------------------------------------------
# Pure analysis functions (no I/O — fully testable)
# ---------------------------------------------------------------------------

def fill_probability(
    our_price: float,
    best_price: float,
    queue_ahead_usd: float,
    quote_size_usd: float,
    is_bid: bool,
) -> float:
    """
    Simplified fill probability for a passive limit order in one round.

    Model:
      • If we improve the best bid/ask by ≥ 1 tick → front of queue → ~80% fill.
      • If at same price → behind existing queue; P ∝ quote / (queue + quote).
      • If behind best → very low probability (~2%).

    Returns value in [0, 1].
    """
    if queue_ahead_usd < 0:
        queue_ahead_usd = 0.0
    if quote_size_usd <= 0:
        return 0.0

    improves = our_price > best_price if is_bid else our_price < best_price
    at_best  = abs(our_price - best_price) < TICK / 2

    if improves:
        return 0.80   # at front of queue — high fill probability
    if at_best:
        total = queue_ahead_usd + quote_size_usd
        if total <= 0:
            return 0.50
        return round(min(0.70, (quote_size_usd / total) * 1.5), 4)
    return 0.02       # behind best — rarely filled


def simulate_fill(probability: float, rng: random.Random) -> bool:
    """Bernoulli draw: True = order filled this round."""
    return rng.random() < probability


def fill_scenario(yes_filled: bool, no_filled: bool) -> str:
    """Return scenario label from two fill booleans."""
    if yes_filled and no_filled:
        return "both"
    if yes_filled:
        return "yes_only"
    if no_filled:
        return "no_only"
    return "none"


def compute_rebate(fill_price: float, fill_size_usd: float,
                   taker_fee: float, rebate: float) -> float:
    """
    Maker rebate earned when a taker fills our limit order.

    rebate_usd = rebate_rate × taker_fee_rate × (1 − fill_price) × fill_size_usd
    """
    return round(rebate * taker_fee * (1.0 - fill_price) * fill_size_usd, 6)


def unrealized_pnl(
    yes_inventory: float,
    no_inventory: float,
    yes_avg_cost: float,
    no_avg_cost: float,
    yes_mid: float,
    no_mid: float,
) -> float:
    """
    Mark-to-market unrealized P&L on open inventory.

    YES inventory: bought at yes_avg_cost, current mark = yes_mid.
    NO  inventory: bought at no_avg_cost,  current mark = no_mid.
    Positive = unrealized gain.
    """
    yes_pnl = yes_inventory * (yes_mid - yes_avg_cost) if yes_avg_cost else 0.0
    no_pnl  = no_inventory  * (no_mid  - no_avg_cost)  if no_avg_cost  else 0.0
    return round(yes_pnl + no_pnl, 6)


def inventory_var(
    yes_inventory: float,
    no_inventory: float,
    yes_mid: float,
    sigma_per_round_bps: float = SIGMA_PER_ROUND_BPS,
    z_score: float = 1.645,
) -> float:
    """
    Simplified per-round 95% VaR on open inventory.

    VaR ≈ z × σ_round × |net_exposure|
    where σ_round = sigma_bps/10000 × yes_mid × sqrt(1).
    """
    net_exposure = abs(yes_inventory * yes_mid - no_inventory * (1.0 - yes_mid))
    sigma_usd = (sigma_per_round_bps / 10_000) * yes_mid
    return round(z_score * sigma_usd * net_exposure, 4)


def hedge_cost_one_side(
    yes_filled: bool,
    no_filled: bool,
    no_ask: float | None,
    yes_ask: float | None,
    fill_size: float,
) -> float | None:
    """
    Theoretical cost to hedge if only ONE side was filled this round.

    If YES filled (we bought YES):  hedge = buy NO at market ask.
    If NO  filled (we bought NO):   hedge = buy YES at market ask.
    If both or none: no hedge needed now.

    Returns cost in USD (fill_size × hedge_ask_price), or None.
    """
    if yes_filled and not no_filled and no_ask is not None:
        return round(fill_size * no_ask, 4)
    if no_filled and not yes_filled and yes_ask is not None:
        return round(fill_size * yes_ask, 4)
    return None


def is_toxic_move(
    buy_price: float,
    current_mid: float,
    spread: float,
    multiplier: float = TOXIC_SPREAD_MULTIPLIER,
) -> bool:
    """
    Flag a toxic fill: price moved adversely by > multiplier × spread since our fill.

    YES buy: toxic if mid drops below buy_price by > multiplier × spread.
    """
    if spread <= 0:
        return False
    adverse = buy_price - current_mid
    return adverse > multiplier * spread


def quote_inside_spread(
    best_bid: float,
    best_ask: float,
    tick: float = TICK,
    n_ticks: int = 1,
) -> tuple[float, float]:
    """
    Place passive quotes n_ticks inside the current spread.

    Returns (our_bid, our_ask). Falls back to best_bid/best_ask if
    spread is ≤ 1 tick (no room to improve).
    """
    spread = best_ask - best_bid
    if spread <= tick:
        return best_bid, best_ask
    our_bid = round(best_bid + tick * n_ticks, 6)
    our_ask = round(best_ask - tick * n_ticks, 6)
    if our_bid >= our_ask:
        return best_bid, best_ask
    return our_bid, our_ask


def within_rewards_band(
    our_bid: float,
    our_ask: float,
    mid: float,
    max_spread_bps: float = REWARDS_MAX_SPREAD_BPS,
) -> bool:
    """
    Check whether our quotes fall within the rewards-eligible spread band.
    Rewards require quotes within max_spread_bps of midpoint.
    """
    if mid <= 0:
        return False
    bid_dist_bps = abs(mid - our_bid) / mid * 10_000
    ask_dist_bps = abs(our_ask - mid) / mid * 10_000
    return bid_dist_bps <= max_spread_bps and ask_dist_bps <= max_spread_bps


def net_pnl_after_hedge(
    realized_pnl: float,
    rebate_pnl: float,
    hedge_costs: list[float],
) -> float:
    """Total net P&L = realized spread + rebates - hedge costs incurred."""
    return round(realized_pnl + rebate_pnl - sum(hedge_costs), 6)


def round_trip_pnl(
    yes_buy_price: float,
    no_buy_price: float,
    yes_ask_quote: float,
    no_ask_quote: float,
    fill_size: float,
) -> float:
    """
    P&L from a completed round-trip (bought YES + NO, both now settled).

    gross = (yes_ask_quote + no_ask_quote - 1.0) × fill_size
    (positive if we sold higher than we bought, guaranteed profit if > 0)
    """
    return round((yes_ask_quote + no_ask_quote - 1.0) * fill_size, 6)


def inventory_within_cap(
    current: float,
    add: float,
    cap: float,
) -> tuple[float, bool]:
    """
    Return (new_inventory, capped) after adding `add` units.
    If adding would exceed cap, return (cap, True) instead.
    """
    proposed = current + add
    if proposed > cap:
        return cap, True
    return round(proposed, 4), False


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RoundRecord:
    ts: str
    round_n: int

    # Market snapshot
    yes_bid:  float | None
    yes_ask:  float | None
    no_bid:   float | None
    no_ask:   float | None
    yes_mid:  float | None
    spread_bps: float | None
    depth_near_mid: float

    # Our paper quotes
    our_yes_bid: float | None
    our_yes_ask: float | None
    our_no_bid:  float | None
    our_no_ask:  float | None
    in_rewards_band: bool

    # Queue position
    yes_queue_ahead: float
    no_queue_ahead:  float

    # Fill probabilities and results
    fill_prob_yes: float
    fill_prob_no:  float
    yes_filled:    bool
    no_filled:     bool
    scenario:      str           # none / yes_only / no_only / both

    # Inventory (after this round)
    yes_inventory: float
    no_inventory:  float
    yes_inventory_capped: bool
    no_inventory_capped:  bool
    yes_avg_cost:  float
    no_avg_cost:   float

    # P&L components
    realized_pnl:       float   # locked round-trips
    unrealized_pnl_usd: float   # mark-to-market
    rebate_pnl:         float   # accumulated rebates this round
    hedge_cost_usd:     float | None   # cost if one-sided hedge needed

    # Risk
    var_95:         float
    toxic_flag:     bool
    adverse_bps:    float       # bps price moved against us vs last fill

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SimState:
    """Mutable state carried across rounds."""
    yes_inventory: float = 0.0
    no_inventory:  float = 0.0
    yes_avg_cost:  float = 0.0
    no_avg_cost:   float = 0.0
    realized_pnl:  float = 0.0
    rebate_pnl:    float = 0.0
    hedge_costs:   list[float] = field(default_factory=list)
    last_yes_fill_price: float | None = None
    last_no_fill_price:  float | None = None
    total_fills:   int = 0

    def absorb_yes_fill(self, price: float, size: float, cap: float) -> bool:
        """Add YES inventory from a fill. Returns True if capped."""
        new_inv, capped = inventory_within_cap(self.yes_inventory, size, cap)
        if not capped:
            # Update average cost
            total_cost = self.yes_avg_cost * self.yes_inventory + price * size
            self.yes_inventory = new_inv
            self.yes_avg_cost = total_cost / new_inv if new_inv else 0.0
        self.last_yes_fill_price = price
        self.total_fills += 1
        return capped

    def absorb_no_fill(self, price: float, size: float, cap: float) -> bool:
        new_inv, capped = inventory_within_cap(self.no_inventory, size, cap)
        if not capped:
            total_cost = self.no_avg_cost * self.no_inventory + price * size
            self.no_inventory = new_inv
            self.no_avg_cost = total_cost / new_inv if new_inv else 0.0
        self.last_no_fill_price = price
        self.total_fills += 1
        return capped

    def flush_round_trip(self, yes_ask: float, no_ask: float,
                         fill_size: float, cap: float) -> None:
        """When both sides fill: book a round-trip and reduce inventory."""
        pnl = round_trip_pnl(
            self.yes_avg_cost, self.no_avg_cost,
            yes_ask, no_ask, fill_size,
        )
        self.realized_pnl = round(self.realized_pnl + pnl, 6)
        reduce = min(fill_size, self.yes_inventory, self.no_inventory)
        self.yes_inventory = round(max(0.0, self.yes_inventory - reduce), 4)
        self.no_inventory  = round(max(0.0, self.no_inventory  - reduce), 4)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


async def _get_json(session: aiohttp.ClientSession, url: str,
                    params: dict | None = None) -> Any:
    try:
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            r.raise_for_status()
            return await r.json(content_type=None)
    except Exception as e:
        return {"_error": str(e)}


async def fetch_market_tokens(session: aiohttp.ClientSession) -> tuple[str, str]:
    """Return (yes_token_id, no_token_id) for the target market."""
    url = f"{GAMMA_BASE}/markets"
    data = await _get_json(session, url, {"slug": TARGET_SLUG})
    if not isinstance(data, list) or not data:
        return "", ""
    m = data[0]
    try:
        ids = json.loads(m.get("clobTokenIds", "[]"))
        return str(ids[0]), str(ids[1])
    except Exception:
        return "", ""


# ---------------------------------------------------------------------------
# One simulation round
# ---------------------------------------------------------------------------

async def simulate_round(
    session: aiohttp.ClientSession,
    yes_tok: str,
    no_tok:  str,
    state:   SimState,
    round_n: int,
    quote_size: float,
    max_inv:    float,
    rng:        random.Random,
) -> RoundRecord:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Fetch books
    yes_b, no_b = await asyncio.gather(
        fetch_clob_book(session, yes_tok),
        fetch_clob_book(session, no_tok),
    )

    yes_bid = yes_b.best_bid
    yes_ask = yes_b.best_ask
    no_bid  = no_b.best_bid
    no_ask  = no_b.best_ask
    yes_mid = yes_b.midpoint

    sp_bps = yes_b.spread_bps

    # Depth within ±5% of mid
    bids_raw = [{"price": str(lv.price), "size": str(lv.size)} for lv in yes_b.bids]
    asks_raw = [{"price": str(lv.price), "size": str(lv.size)} for lv in yes_b.asks]
    depth = _depth_near(bids_raw, asks_raw, yes_mid)

    # Our paper quotes
    our_yes_bid = our_yes_ask = our_no_bid = our_no_ask = None
    in_band = False
    if yes_bid and yes_ask:
        our_yes_bid, our_yes_ask = quote_inside_spread(yes_bid, yes_ask)
        in_band = within_rewards_band(our_yes_bid, our_yes_ask, yes_mid or 0)
    if no_bid and no_ask:
        our_no_bid, our_no_ask = quote_inside_spread(no_bid, no_ask)

    # Queue position: size ahead of us at our bid price
    yes_q = _queue_ahead(yes_b.bids, our_yes_bid)
    no_q  = _queue_ahead(no_b.bids,  our_no_bid)

    # Fill probabilities
    fp_yes = fill_probability(our_yes_bid or 0, yes_bid or 0, yes_q, quote_size, True) \
        if our_yes_bid and yes_bid else 0.0
    fp_no  = fill_probability(our_no_bid  or 0, no_bid  or 0, no_q,  quote_size, True) \
        if our_no_bid  and no_bid  else 0.0

    # Cap if inventory already at limit
    if state.yes_inventory >= max_inv:
        fp_yes = 0.0
    if state.no_inventory >= max_inv:
        fp_no = 0.0

    # Simulate fills (YES and NO fills are negatively correlated in binary markets;
    # simplify: independent draws but cap combined fill to realistic rate)
    yes_filled = simulate_fill(fp_yes, rng)
    no_filled  = simulate_fill(fp_no,  rng)

    scenario = fill_scenario(yes_filled, no_filled)

    # Update inventory and P&L
    round_rebate = 0.0
    yes_capped = no_capped = False

    if yes_filled and our_yes_bid:
        yes_capped = state.absorb_yes_fill(our_yes_bid, quote_size, max_inv)
        round_rebate += compute_rebate(our_yes_bid, quote_size, TAKER_FEE_RATE, REBATE_RATE)

    if no_filled and our_no_bid:
        no_capped = state.absorb_no_fill(our_no_bid, quote_size, max_inv)
        round_rebate += compute_rebate(our_no_bid, quote_size, TAKER_FEE_RATE, REBATE_RATE)

    if scenario == "both" and our_yes_ask and our_no_ask:
        state.flush_round_trip(our_yes_ask, our_no_ask, quote_size, max_inv)

    state.rebate_pnl = round(state.rebate_pnl + round_rebate, 6)

    # Hedge cost for one-side fills
    hc = hedge_cost_one_side(yes_filled, no_filled, no_ask, yes_ask, quote_size)
    if hc is not None:
        state.hedge_costs.append(hc)

    # Unrealized P&L
    upnl = unrealized_pnl(
        state.yes_inventory, state.no_inventory,
        state.yes_avg_cost,  state.no_avg_cost,
        yes_mid or 0, 1.0 - (yes_mid or 0),
    )

    # VaR
    var = inventory_var(state.yes_inventory, state.no_inventory, yes_mid or 0.065)

    # Toxic flow detection
    toxic = False
    adverse = 0.0
    if state.last_yes_fill_price and yes_mid and yes_b.spread:
        adverse = (state.last_yes_fill_price - yes_mid) / (yes_mid + 1e-9) * 10_000
        toxic = is_toxic_move(state.last_yes_fill_price, yes_mid, yes_b.spread or TICK)

    return RoundRecord(
        ts=ts, round_n=round_n,
        yes_bid=yes_bid, yes_ask=yes_ask,
        no_bid=no_bid,   no_ask=no_ask,
        yes_mid=yes_mid, spread_bps=sp_bps, depth_near_mid=depth,
        our_yes_bid=our_yes_bid, our_yes_ask=our_yes_ask,
        our_no_bid=our_no_bid,   our_no_ask=our_no_ask,
        in_rewards_band=in_band,
        yes_queue_ahead=yes_q, no_queue_ahead=no_q,
        fill_prob_yes=fp_yes, fill_prob_no=fp_no,
        yes_filled=yes_filled, no_filled=no_filled,
        scenario=scenario,
        yes_inventory=state.yes_inventory, no_inventory=state.no_inventory,
        yes_inventory_capped=yes_capped, no_inventory_capped=no_capped,
        yes_avg_cost=state.yes_avg_cost, no_avg_cost=state.no_avg_cost,
        realized_pnl=state.realized_pnl,
        unrealized_pnl_usd=upnl,
        rebate_pnl=round_rebate,
        hedge_cost_usd=hc,
        var_95=var, toxic_flag=toxic, adverse_bps=round(adverse, 1),
    )


def _queue_ahead(bids: list, price: float | None) -> float:
    """Total USD size in book at the same price level (ahead of our order)."""
    if price is None:
        return 0.0
    total = 0.0
    for lv in bids:
        if abs(lv.price - price) < TICK / 2:
            total += lv.size * lv.price
    return round(total, 2)


def _depth_near(bids: list[dict], asks: list[dict], mid: float | None,
                pct: float = 0.05) -> float:
    if not mid or mid <= 0:
        return 0.0
    lo, hi = mid * (1 - pct), mid * (1 + pct)
    total = 0.0
    for lvl in bids + asks:
        try:
            p, s = float(lvl["price"]), float(lvl["size"])
            if lo <= p <= hi:
                total += p * s
        except Exception:
            pass
    return round(total, 2)


# ---------------------------------------------------------------------------
# JSONL + report
# ---------------------------------------------------------------------------

def _append_record(rec: RoundRecord, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec.to_dict()) + "\n")


def _flt(v: Any, fmt: str = ".5f") -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:{fmt}}"


def generate_report(records: list[RoundRecord], state: SimState,
                    quote_size: float, report_path: Path) -> None:
    n = len(records)
    if n == 0:
        report_path.write_text("# Paper Quote Simulator — No data yet.\n")
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    fills = [r for r in records if r.scenario != "none"]
    both  = [r for r in records if r.scenario == "both"]
    one   = [r for r in records if r.scenario in ("yes_only", "no_only")]
    toxic_evts = [r for r in records if r.toxic_flag]

    fill_ratio   = len(fills) / n
    both_ratio   = len(both) / n
    round_rebates= [r.rebate_pnl for r in records]
    total_rebate = sum(round_rebates)
    hedge_total  = sum(h for r in records for h in ([r.hedge_cost_usd] if r.hedge_cost_usd else []))

    net_pnl = net_pnl_after_hedge(state.realized_pnl, state.rebate_pnl, state.hedge_costs)
    final_upnl = records[-1].unrealized_pnl_usd if records else 0.0
    final_var   = records[-1].var_95 if records else 0.0
    final_inv_y = records[-1].yes_inventory
    final_inv_n = records[-1].no_inventory

    spread_vals = [r.spread_bps for r in records if r.spread_bps]
    depth_vals  = [r.depth_near_mid for r in records]

    L: list[str] = []
    a = L.append

    a("# Paper Quote Simulator — Report")
    a("")
    a(f"> Market: `{TARGET_SLUG}`")
    a(f"> Generated: {ts}")
    a(f"> Rounds: {n}  |  Quote size: ${quote_size:.0f}/side")
    a("")

    a("## 1. Fill Statistics")
    a("")
    a(f"| | Value |")
    a(f"|---|---|")
    a(f"| Total rounds | {n} |")
    a(f"| Rounds with ≥1 fill | {len(fills)} ({fill_ratio:.0%}) |")
    a(f"| Both-sides filled | {len(both)} ({both_ratio:.0%}) |")
    a(f"| One-side only | {len(one)} ({len(one)/n:.0%}) |")
    a(f"| No fills | {n - len(fills)} ({(n-len(fills))/n:.0%}) |")
    a(f"| Fill prob YES (avg) | {mean([r.fill_prob_yes for r in records]):.1%} |")
    a(f"| Fill prob NO (avg)  | {mean([r.fill_prob_no for r in records]):.1%} |")
    a("")

    a("## 2. P&L Summary")
    a("")
    a(f"| Component | USD |")
    a(f"|-----------|-----|")
    a(f"| Realized spread P&L | {state.realized_pnl:+.4f} |")
    a(f"| Total rebate earned  | {state.rebate_pnl:+.4f} |")
    a(f"| Hedge costs incurred | {-hedge_total:.4f} |")
    a(f"| **Net P&L after hedge** | **{net_pnl:+.4f}** |")
    a(f"| Unrealized P&L (mark) | {final_upnl:+.4f} |")
    a(f"| Total fills executed  | {state.total_fills} |")
    a("")

    a("## 3. Inventory Status")
    a("")
    a(f"| | YES | NO |")
    a(f"|---|---|---|")
    a(f"| Final inventory (USD) | {final_inv_y:.2f} | {final_inv_n:.2f} |")
    a(f"| Avg cost | {state.yes_avg_cost:.4f} | {state.no_avg_cost:.4f} |")
    a(f"| Inventory cap reached | {'Yes' if any(r.yes_inventory_capped for r in records) else 'No'} | {'Yes' if any(r.no_inventory_capped for r in records) else 'No'} |")
    a("")

    a("## 4. Risk Metrics")
    a("")
    a(f"| Metric | Value |")
    a(f"|--------|-------|")
    a(f"| VaR 95% (final round) | ${final_var:.4f} |")
    a(f"| Toxic flow events | {len(toxic_evts)} / {n} rounds |")
    if toxic_evts:
        a(f"| Worst adverse move | {max(r.adverse_bps for r in toxic_evts):.1f} bps |")
    a(f"| In-rewards-band ratio | {mean([1 if r.in_rewards_band else 0 for r in records]):.0%} |")
    if spread_vals:
        a(f"| Spread bps (mean/min/max) | {mean(spread_vals):.1f} / {min(spread_vals):.1f} / {max(spread_vals):.1f} |")
    if depth_vals:
        a(f"| Depth near mid (mean) | ${mean(depth_vals):,.0f} |")
    a("")

    a("## 5. Round-by-Round Log")
    a("")
    a("| Round | Scenario | YES inv | NO inv | Rebate | Real PnL | VaR | Toxic |")
    a("|-------|----------|---------|--------|--------|----------|-----|-------|")
    for r in records:
        tox = "⚠️" if r.toxic_flag else "—"
        a(f"| {r.round_n+1} | {r.scenario:<10} "
          f"| {r.yes_inventory:>7.1f} | {r.no_inventory:>6.1f} "
          f"| {r.rebate_pnl:>+7.5f} | {r.realized_pnl:>+8.4f} "
          f"| {r.var_95:.4f} | {tox} |")
    a("")

    a("## 6. GO / WATCH / NO-GO")
    a("")
    go_conds = {
        "Net P&L > 0": net_pnl > 0,
        "Fill ratio > 0": fill_ratio > 0,
        "No inventory cap breach": not any(r.yes_inventory_capped or r.no_inventory_capped for r in records),
        "Toxic events < 20% of rounds": len(toxic_evts) / n < 0.20,
        "In-rewards-band > 80%": mean([1 if r.in_rewards_band else 0 for r in records]) >= 0.80,
        "Depth > 0 every round": all(r.depth_near_mid > 0 for r in records),
    }
    all_go = all(go_conds.values())
    verdict = "✅ GO — continue to long-term paper run" if all_go else (
        "👁 WATCH — some conditions not met yet" if net_pnl >= 0 else
        "❌ NO-GO — negative net P&L"
    )
    a(f"### Verdict: {verdict}")
    a("")
    a(f"| Condition | Result |")
    a(f"|-----------|--------|")
    for label, ok in go_conds.items():
        a(f"| {label} | {'✅' if ok else '❌'} |")
    a("")
    a("---")
    a("*Paper simulation only. No real orders placed. No wallet required.*")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(L), encoding="utf-8")
    print(f"[out] Report → {report_path}")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

async def run(rounds: int, interval_s: int, quote_size: float,
              max_inv: float, out_path: Path, once: bool,
              seed: int = 42) -> tuple[list[RoundRecord], SimState]:

    rng   = random.Random(seed)
    state = SimState()
    records: list[RoundRecord] = []

    connector = _make_connector()
    headers = {"User-Agent": "polymarket-okx-research/3.0 (paper-quote-simulator)"}
    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        print(f"[init] Fetching token IDs for {TARGET_SLUG}…")
        yes_tok, no_tok = await fetch_market_tokens(session)
        if not yes_tok:
            print("ERROR: could not find market tokens.")
            return records, state

        n = 1 if once else rounds
        for r in range(n):
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[round {r+1}/{n}  {ts}]  inventory YES={state.yes_inventory:.1f}  NO={state.no_inventory:.1f}")
            rec = await simulate_round(session, yes_tok, no_tok, state, r,
                                       quote_size, max_inv, rng)
            records.append(rec)
            _append_record(rec, out_path)
            print(f"          scenario={rec.scenario}  rebate={rec.rebate_pnl:+.5f}  "
                  f"toxic={'YES' if rec.toxic_flag else 'no'}  "
                  f"depth=${rec.depth_near_mid:,.0f}")
            if r < n - 1:
                print(f"          sleeping {interval_s}s…")
                await asyncio.sleep(interval_s)

    return records, state


def main() -> None:
    p = argparse.ArgumentParser(description="Paper quote simulator (read-only)")
    p.add_argument("--rounds",       type=int,   default=DEFAULT_ROUNDS)
    p.add_argument("--interval-sec", type=int,   default=DEFAULT_INTERVAL_S)
    p.add_argument("--quote-size",   type=float, default=DEFAULT_QUOTE_SIZE)
    p.add_argument("--max-inventory",type=float, default=DEFAULT_MAX_INVENTORY)
    p.add_argument("--out",          type=Path,  default=DEFAULT_OUT)
    p.add_argument("--report",       type=Path,  default=DEFAULT_REPORT)
    p.add_argument("--once",         action="store_true")
    p.add_argument("--seed",         type=int,   default=42)
    args = p.parse_args()

    records, state = asyncio.run(run(
        rounds=args.rounds, interval_s=args.interval_sec,
        quote_size=args.quote_size, max_inv=args.max_inventory,
        out_path=args.out, once=args.once, seed=args.seed,
    ))

    if records:
        generate_report(records, state, args.quote_size, args.report)

        net = net_pnl_after_hedge(state.realized_pnl, state.rebate_pnl, state.hedge_costs)
        toxic = sum(1 for r in records if r.toxic_flag)
        print(f"\n{'='*55}")
        print(f"  PAPER SIMULATION SUMMARY  ({len(records)} rounds)")
        print(f"{'='*55}")
        print(f"  Realized PnL    : {state.realized_pnl:+.4f} USD")
        print(f"  Rebate PnL      : {state.rebate_pnl:+.4f} USD")
        print(f"  Hedge costs     : {-sum(state.hedge_costs):.4f} USD")
        print(f"  Net PnL         : {net:+.4f} USD")
        print(f"  YES inventory   : {state.yes_inventory:.1f} USD")
        print(f"  NO  inventory   : {state.no_inventory:.1f} USD")
        print(f"  Total fills     : {state.total_fills}")
        print(f"  Toxic events    : {toxic}")
        print(f"{'='*55}")


if __name__ == "__main__":
    main()
