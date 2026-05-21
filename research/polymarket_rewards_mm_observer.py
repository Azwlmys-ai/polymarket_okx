"""
polymarket_rewards_mm_observer.py — read-only market-making observer.

Discovers Polymarket markets with active liquidity/maker rewards and evaluates
each for paper market-making attractiveness.

NO TRADING. NO WALLET. NO PRIVATE KEYS. READ-ONLY.

Usage:
    python3 research/polymarket_rewards_mm_observer.py [--limit N] [--out FILE]

Outputs:
    research/rewards_mm_candidates.jsonl   — one JSON record per candidate
    research/rewards_mm_report.md          — human-readable Top-20 report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import ssl
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import aiohttp

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

GAMMA_MARKETS_PATH = "/markets"
CLOB_BOOK_PATH = "/book"

HTTP_TIMEOUT_S = 12
MAX_WORKERS = 10          # parallel book fetches
GAMMA_PAGE_LIMIT = 100

# Market-making thresholds (paper only)
HEDGE_BREAKEVEN = 1.00    # YES_ask + NO_ask above this → round-trip loss before rewards
HEDGE_WARN = 1.02         # above this → strong adverse hedge cost
SPREAD_MIN_BPS = 10       # spread too tight → compete with bots
SPREAD_MAX_BPS = 2000     # spread too wide → nobody is trading
MIN_VOLUME_24H = 500      # USD
MIN_DAYS_TO_EXPIRY = 1

TAKER_FEE_RATE = 0.05     # 5 % (standard Polymarket)

DEFAULT_OUT = Path("research/rewards_mm_candidates.jsonl")
REPORT_OUT = Path("research/rewards_mm_report.md")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class BookState:
    """Parsed CLOB order book for one token (YES or NO)."""
    token_id: str
    bids: list[BookLevel] = field(default_factory=list)   # descending by price
    asks: list[BookLevel] = field(default_factory=list)   # ascending by price
    last_trade: float | None = None
    min_order_size: float = 5.0
    tick_size: float = 0.01
    fetch_error: str | None = None

    # --- computed convenience properties ---

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def midpoint(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return round((self.best_bid + self.best_ask) / 2, 6)
        return None

    @property
    def spread(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return round(self.best_ask - self.best_bid, 6)
        return None

    @property
    def spread_bps(self) -> float | None:
        mid = self.midpoint
        sp = self.spread
        if mid and sp and mid > 0:
            return round(sp / mid * 10_000, 1)
        return None

    def depth_usd(self, n_levels: int = 5) -> float:
        """Total USD size across the top-n bid+ask levels."""
        bid_sz = sum(lv.price * lv.size for lv in self.bids[:n_levels])
        ask_sz = sum(lv.price * lv.size for lv in self.asks[:n_levels])
        return round(bid_sz + ask_sz, 2)


@dataclass
class MMAnalysis:
    """Paper market-making analysis for one market."""
    # identifiers
    slug: str
    question: str
    yes_token_id: str
    no_token_id: str
    condition_id: str

    # market meta
    end_date: str
    days_to_expiry: float
    volume_24h: float
    liquidity: float
    rewards_min_size: float
    rewards_max_spread: float
    maker_base_fee_bps: int
    taker_fee_rate: float
    rebate_rate: float
    holding_rewards: bool

    # book state
    yes_book: BookState
    no_book: BookState

    # computed
    yes_mid: float | None
    no_mid: float | None
    yes_spread: float | None
    no_spread: float | None
    yes_spread_bps: float | None

    # hedge analysis
    yes_ask: float | None
    no_ask: float | None
    hedge_cost: float | None          # YES_ask + NO_ask (cost to lock in both sides)
    hedge_ok: bool                    # hedge_cost <= HEDGE_BREAKEVEN
    hedge_profit: float | None        # 1.0 - hedge_cost (positive = guaranteed profit)

    # paper quote
    paper_yes_bid: float | None       # we'd quote at inside spread
    paper_yes_ask: float | None
    paper_no_bid: float | None
    paper_no_ask: float | None
    paper_locked_profit: float | None # paper_yes_ask + paper_no_ask - 1.0
    paper_max_one_side_loss: float | None  # worst-case if only one side fills

    # maker rebate (per unit on YES side)
    maker_rebate_per_unit: float | None

    # scoring
    candidate_score: float            # 0–100, higher = better MM candidate
    recommend_observe: bool
    risk_score: str                   # LOW / MEDIUM / HIGH
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["yes_book"] = asdict(self.yes_book)
        d["no_book"] = asdict(self.no_book)
        return d


# ---------------------------------------------------------------------------
# Pure analysis functions (no I/O — fully testable)
# ---------------------------------------------------------------------------

def compute_hedge_cost(yes_ask: float | None, no_ask: float | None) -> float | None:
    """YES_ask + NO_ask. Lower is better for the market maker."""
    if yes_ask is None or no_ask is None:
        return None
    return round(yes_ask + no_ask, 6)


def compute_hedge_profit(hedge_cost: float | None) -> float | None:
    """
    Guaranteed profit per unit if both sides fill at ask.
    Positive  →  filling both sides at quoted asks beats payout of 1.0.
    """
    if hedge_cost is None:
        return None
    return round(1.0 - hedge_cost, 6)


def compute_paper_quotes(
    book: BookState,
    n_ticks_inside: int = 1,
) -> tuple[float | None, float | None]:
    """
    Paper maker quotes: bid/ask one tick inside the current best bid/ask.

    Returns (paper_bid, paper_ask).
    """
    tick = book.tick_size or 0.01
    bid = book.best_bid
    ask = book.best_ask
    if bid is None or ask is None:
        return None, None
    p_bid = round(min(bid + tick * n_ticks_inside, 0.99), 6)
    p_ask = round(max(ask - tick * n_ticks_inside, 0.01), 6)
    if p_bid >= p_ask:          # spread too tight for inside quote
        p_bid = bid
        p_ask = ask
    return p_bid, p_ask


def compute_paper_locked_profit(
    paper_yes_ask: float | None,
    paper_no_ask: float | None,
) -> float | None:
    """
    Round-trip profit if BOTH our paper asks fill (someone buys YES & NO from us).
    = YES_ask + NO_ask - 1.0
    Positive → guaranteed win on completed round-trip.
    """
    if paper_yes_ask is None or paper_no_ask is None:
        return None
    return round(paper_yes_ask + paper_no_ask - 1.0, 6)


def compute_max_one_side_loss(
    paper_yes_ask: float | None,
    paper_no_ask: float | None,
) -> float | None:
    """
    Worst-case loss if only ONE side fills and the other side moves against us.

    If YES fills at paper_yes_ask and YES actually wins:
        payout = 1.0, received = paper_yes_ask → loss = 1.0 - paper_yes_ask
    The analogous case holds for NO.

    Returns the larger of the two (conservative).
    """
    losses = []
    if paper_yes_ask is not None:
        losses.append(round(1.0 - paper_yes_ask, 6))
    if paper_no_ask is not None:
        losses.append(round(1.0 - paper_no_ask, 6))
    return max(losses) if losses else None


def compute_maker_rebate(
    fill_price: float,
    taker_fee_rate: float,
    rebate_rate: float,
) -> float:
    """
    Maker rebate per unit received when a taker fills our limit order.

    rebate = rebate_rate * taker_fee_rate * (1 - fill_price)
    """
    return round(rebate_rate * taker_fee_rate * (1.0 - fill_price), 6)


def candidate_score(
    rewards_min_size: float,
    rewards_max_spread: float,
    hedge_cost: float | None,
    spread_bps: float | None,
    volume_24h: float,
    days_to_expiry: float,
    depth_usd: float,
    holding_rewards: bool,
) -> float:
    """
    Score a market for MM attractiveness (0–100).

    Higher score = better candidate.
    """
    score = 0.0

    # Active rewards program is the primary qualifier
    if rewards_min_size > 0 and rewards_max_spread > 0:
        score += 35.0
    elif rewards_min_size > 0 or rewards_max_spread > 0:
        score += 15.0

    # Holding rewards add extra passive income
    if holding_rewards:
        score += 5.0

    # Hedge attractiveness
    if hedge_cost is not None:
        if hedge_cost < 0.98:
            score += 25.0          # excellent — guaranteed profit on round-trip
        elif hedge_cost < 1.00:
            score += 20.0          # good
        elif hedge_cost < 1.01:
            score += 12.0          # marginal
        elif hedge_cost < 1.02:
            score += 5.0           # expensive hedge
        # > 1.02 → 0 points

    # Spread quality
    if spread_bps is not None:
        if SPREAD_MIN_BPS < spread_bps < 200:
            score += 15.0          # optimal: tight but profitable
        elif spread_bps <= SPREAD_MIN_BPS:
            score += 5.0           # too tight
        elif spread_bps < 500:
            score += 10.0          # wide but workable
        # very wide → 0 points

    # Volume (liquidity signal)
    if volume_24h >= 50_000:
        score += 10.0
    elif volume_24h >= 5_000:
        score += 6.0
    elif volume_24h >= 500:
        score += 2.0

    # Time horizon
    if days_to_expiry >= 30:
        score += 5.0
    elif days_to_expiry >= 7:
        score += 3.0
    elif days_to_expiry >= 1:
        score += 1.0

    # Book depth
    if depth_usd >= 10_000:
        score += 5.0
    elif depth_usd >= 1_000:
        score += 2.0

    return round(min(score, 100.0), 2)


def risk_label(
    hedge_cost: float | None,
    days_to_expiry: float,
    spread_bps: float | None,
) -> str:
    """Return LOW / MEDIUM / HIGH risk label."""
    high = (
        (hedge_cost is not None and hedge_cost > HEDGE_WARN)
        or days_to_expiry < 1
        or (spread_bps is not None and spread_bps > SPREAD_MAX_BPS)
    )
    low = (
        hedge_cost is not None and hedge_cost < 1.00
        and days_to_expiry >= 7
        and spread_bps is not None and SPREAD_MIN_BPS < spread_bps < 500
    )
    if high:
        return "HIGH"
    if low:
        return "LOW"
    return "MEDIUM"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _make_connector() -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(ssl=_SSL_CTX, limit=MAX_WORKERS)


async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    params: dict | None = None,
) -> Any:
    try:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
        async with session.get(url, params=params, timeout=timeout) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)
    except Exception as exc:
        return {"_error": str(exc)}


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

async def fetch_gamma_markets(
    session: aiohttp.ClientSession,
    limit: int = GAMMA_PAGE_LIMIT,
) -> list[dict]:
    """Fetch active Gamma markets. Returns raw list."""
    url = f"{GAMMA_BASE}{GAMMA_MARKETS_PATH}"
    params = {"limit": limit, "active": "true", "closed": "false"}
    data = await _get_json(session, url, params)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "_error" in data:
            print(f"[warn] Gamma fetch error: {data['_error']}", file=sys.stderr)
            return []
        return data.get("data") or data.get("markets") or []
    return []


async def fetch_clob_book(
    session: aiohttp.ClientSession,
    token_id: str,
) -> BookState:
    """Fetch CLOB order book for one token."""
    url = f"{CLOB_BASE}{CLOB_BOOK_PATH}"
    data = await _get_json(session, url, {"token_id": token_id})

    if not isinstance(data, dict) or "_error" in data:
        return BookState(
            token_id=token_id,
            fetch_error=data.get("_error") if isinstance(data, dict) else "bad response",
        )

    def _parse_levels(raw: list) -> list[BookLevel]:
        levels = []
        for entry in raw or []:
            try:
                levels.append(BookLevel(price=float(entry["price"]), size=float(entry["size"])))
            except (KeyError, ValueError):
                pass
        return levels

    bids = sorted(_parse_levels(data.get("bids")), key=lambda x: x.price, reverse=True)
    asks = sorted(_parse_levels(data.get("asks")), key=lambda x: x.price)

    lt_raw = data.get("last_trade_price")
    try:
        last_trade = float(lt_raw) if lt_raw else None
    except (ValueError, TypeError):
        last_trade = None

    tick = None
    try:
        tick = float(data.get("tick_size") or 0.01)
    except (ValueError, TypeError):
        tick = 0.01

    min_sz = None
    try:
        min_sz = float(data.get("min_order_size") or 5.0)
    except (ValueError, TypeError):
        min_sz = 5.0

    return BookState(
        token_id=token_id,
        bids=bids,
        asks=asks,
        last_trade=last_trade,
        min_order_size=min_sz or 5.0,
        tick_size=tick or 0.01,
    )


# ---------------------------------------------------------------------------
# Analysis builder
# ---------------------------------------------------------------------------

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (ValueError, TypeError):
        return default


def _parse_end_date(raw: str | None) -> tuple[str, float]:
    """Return (iso_str, days_to_expiry)."""
    if not raw:
        return "unknown", 0.0
    try:
        s = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        now = datetime.now(timezone.utc)
        days = (dt - now).total_seconds() / 86400
        return raw, round(days, 2)
    except Exception:
        return raw, 0.0


def _token_ids(market: dict) -> tuple[str, str]:
    """Return (yes_token_id, no_token_id)."""
    raw = market.get("clobTokenIds") or "[]"
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        yes = str(ids[0]) if len(ids) > 0 else ""
        no  = str(ids[1]) if len(ids) > 1 else ""
        return yes, no
    except Exception:
        return "", ""


def build_analysis(market: dict, yes_book: BookState, no_book: BookState) -> MMAnalysis:
    """Combine raw market dict + two books into an MMAnalysis."""
    slug       = market.get("slug") or market.get("id") or "unknown"
    question   = (market.get("question") or market.get("groupItemTitle") or slug)[:120]
    yes_tok, no_tok = _token_ids(market)
    cond_id    = market.get("conditionId") or ""
    end_date, days = _parse_end_date(market.get("endDate"))
    vol_24h    = _safe_float(market.get("volume24hr"))
    liquidity  = _safe_float(market.get("liquidity"))

    fee_sched  = market.get("feeSchedule") or {}
    taker_rate = _safe_float(fee_sched.get("rate"), TAKER_FEE_RATE)
    rebate     = _safe_float(fee_sched.get("rebateRate"), 0.25)

    rms        = _safe_float(market.get("rewardsMinSize"))
    rmx        = _safe_float(market.get("rewardsMaxSpread"))
    maker_fee  = int(market.get("makerBaseFee") or 0)
    holding    = bool(market.get("holdingRewardsEnabled"))

    yes_ask    = yes_book.best_ask
    no_ask     = no_book.best_ask
    hedge_cost = compute_hedge_cost(yes_ask, no_ask)
    hedge_prof = compute_hedge_profit(hedge_cost)

    p_yes_bid, p_yes_ask = compute_paper_quotes(yes_book)
    p_no_bid,  p_no_ask  = compute_paper_quotes(no_book)

    locked_profit      = compute_paper_locked_profit(p_yes_ask, p_no_ask)
    max_one_side_loss  = compute_max_one_side_loss(p_yes_ask, p_no_ask)

    rebate_per_unit = (
        compute_maker_rebate(p_yes_ask or 0.5, taker_rate, rebate)
        if p_yes_ask else None
    )

    depth = yes_book.depth_usd() + no_book.depth_usd()

    score = candidate_score(
        rewards_min_size=rms,
        rewards_max_spread=rmx,
        hedge_cost=hedge_cost,
        spread_bps=yes_book.spread_bps,
        volume_24h=vol_24h,
        days_to_expiry=days,
        depth_usd=depth,
        holding_rewards=holding,
    )

    risk = risk_label(hedge_cost, days, yes_book.spread_bps)

    notes = []
    if yes_book.fetch_error:
        notes.append(f"YES book error: {yes_book.fetch_error}")
    if no_book.fetch_error:
        notes.append(f"NO book error: {no_book.fetch_error}")
    if hedge_cost and hedge_cost > HEDGE_WARN:
        notes.append(f"Hedge cost {hedge_cost:.3f} > {HEDGE_WARN} — expensive round-trip")
    if days < 1:
        notes.append("Expires within 24h — very short window")
    if rms > 0 and rmx > 0:
        notes.append(f"Active rewards: min_size={rms}, max_spread={rmx}")

    recommend = (
        score >= 40
        and days >= MIN_DAYS_TO_EXPIRY
        and (hedge_cost is None or hedge_cost <= HEDGE_WARN)
        and (yes_book.best_bid is not None)
    )

    return MMAnalysis(
        slug=slug,
        question=question,
        yes_token_id=yes_tok,
        no_token_id=no_tok,
        condition_id=cond_id,
        end_date=end_date,
        days_to_expiry=days,
        volume_24h=vol_24h,
        liquidity=liquidity,
        rewards_min_size=rms,
        rewards_max_spread=rmx,
        maker_base_fee_bps=maker_fee,
        taker_fee_rate=taker_rate,
        rebate_rate=rebate,
        holding_rewards=holding,
        yes_book=yes_book,
        no_book=no_book,
        yes_mid=yes_book.midpoint,
        no_mid=no_book.midpoint,
        yes_spread=yes_book.spread,
        no_spread=no_book.spread,
        yes_spread_bps=yes_book.spread_bps,
        yes_ask=yes_ask,
        no_ask=no_ask,
        hedge_cost=hedge_cost,
        hedge_ok=hedge_cost is not None and hedge_cost <= HEDGE_BREAKEVEN,
        hedge_profit=hedge_prof,
        paper_yes_bid=p_yes_bid,
        paper_yes_ask=p_yes_ask,
        paper_no_bid=p_no_bid,
        paper_no_ask=p_no_ask,
        paper_locked_profit=locked_profit,
        paper_max_one_side_loss=max_one_side_loss,
        maker_rebate_per_unit=rebate_per_unit,
        candidate_score=score,
        recommend_observe=recommend,
        risk_score=risk,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Main async pipeline
# ---------------------------------------------------------------------------

async def run(limit: int, out_path: Path) -> list[MMAnalysis]:
    connector = _make_connector()
    headers = {"User-Agent": "polymarket-okx-research/2.0 (read-only observer)"}
    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        print(f"[fetch] Gamma markets (limit={limit})…")
        markets = await fetch_gamma_markets(session, limit=limit)
        print(f"[fetch] Got {len(markets)} markets")

        # Fetch YES + NO books in parallel
        semaphore = asyncio.Semaphore(MAX_WORKERS)

        async def _fetch_both(market: dict) -> tuple[dict, BookState, BookState]:
            yes_tok, no_tok = _token_ids(market)
            async with semaphore:
                yes_b, no_b = await asyncio.gather(
                    fetch_clob_book(session, yes_tok) if yes_tok else asyncio.coroutine(lambda: BookState(token_id=""))(),
                    fetch_clob_book(session, no_tok)  if no_tok  else asyncio.coroutine(lambda: BookState(token_id=""))(),
                )
            return market, yes_b, no_b

        print(f"[fetch] Fetching {len(markets)} YES/NO book pairs…")
        results = await asyncio.gather(*[_fetch_both(m) for m in markets])

        analyses = []
        for market, yes_b, no_b in results:
            a = build_analysis(market, yes_b, no_b)
            analyses.append(a)

        # Sort by score descending
        analyses.sort(key=lambda a: a.candidate_score, reverse=True)

        # Write JSONL
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            for a in analyses:
                f.write(json.dumps(a.to_dict()) + "\n")
        print(f"[out] Wrote {len(analyses)} records → {out_path}")

        return analyses


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _f(v: float | None, fmt: str = ".4f") -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:{fmt}}"


def generate_report(analyses: list[MMAnalysis], report_path: Path) -> None:
    top20 = analyses[:20]
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines: list[str] = []
    a = lines.append

    a("# Polymarket Rewards Market-Making Observer — Report")
    a("")
    a(f"> Generated: {now}")
    a(f"> Markets scanned: {len(analyses)}")
    a(f"> Top-20 shown below. Full results: rewards_mm_candidates.jsonl")
    a("")

    # Summary stats
    with_rewards   = [x for x in analyses if x.rewards_min_size > 0 or x.rewards_max_spread > 0]
    hedge_ok_cnt   = sum(1 for x in analyses if x.hedge_ok)
    recommended    = [x for x in analyses if x.recommend_observe]
    a("## Summary")
    a("")
    a(f"| | Count |")
    a(f"|---|---|")
    a(f"| Total markets scanned | {len(analyses)} |")
    a(f"| With active rewards params | {len(with_rewards)} |")
    a(f"| Hedge cost ≤ 1.00 (risk-free round-trip) | {hedge_ok_cnt} |")
    a(f"| Recommended for paper observation | {len(recommended)} |")
    a("")

    # Top 20 table
    a("## Top 20 Candidates (by score)")
    a("")
    a("| # | Slug | Midpoint | Spread | YES+NO Cost | Locked Profit | Risk | Score | Observe |")
    a("|---|------|---------|--------|-------------|---------------|------|-------|---------|")
    for i, m in enumerate(top20, 1):
        hedge_str = _f(m.hedge_cost, ".3f")
        locked_str = _f(m.paper_locked_profit, "+.4f")
        obs = "✅" if m.recommend_observe else "—"
        a(
            f"| {i} "
            f"| `{m.slug[:40]}` "
            f"| {_f(m.yes_mid, '.3f')} "
            f"| {_f(m.yes_spread, '.3f')} "
            f"| {hedge_str} "
            f"| {locked_str} "
            f"| {m.risk_score} "
            f"| {m.candidate_score:.1f} "
            f"| {obs} |"
        )
    a("")

    # Detailed cards for Top 20
    a("## Detailed Candidate Cards")
    a("")
    for i, m in enumerate(top20, 1):
        a(f"### {i}. {m.question[:80]}")
        a("")
        a(f"**Slug**: `{m.slug}`  ")
        a(f"**Score**: {m.candidate_score:.1f} / 100  |  **Risk**: {m.risk_score}  |  **Observe**: {'✅ YES' if m.recommend_observe else '— NO'}")
        a("")
        a(f"| Field | Value |")
        a(f"|-------|-------|")
        a(f"| YES token | `{m.yes_token_id[:24]}…` |")
        a(f"| NO token  | `{m.no_token_id[:24]}…`  |")
        a(f"| End date  | {m.end_date[:19]} ({m.days_to_expiry:.1f}d) |")
        a(f"| Volume 24h | ${m.volume_24h:,.0f} |")
        a(f"| Liquidity  | ${m.liquidity:,.0f} |")
        a(f"| Rewards minSize | {m.rewards_min_size} |")
        a(f"| Rewards maxSpread | {m.rewards_max_spread} |")
        a(f"| Maker rebate rate | {m.rebate_rate:.0%} of taker fee |")
        a(f"| Holding rewards | {'Yes' if m.holding_rewards else 'No'} |")
        a("")
        a(f"**Order-book snapshot**")
        a("")
        a(f"| | YES | NO |")
        a(f"|---|---|---|")
        a(f"| Best Bid | {_f(m.yes_book.best_bid, '.3f')} | {_f(m.no_book.best_bid, '.3f')} |")
        a(f"| Best Ask | {_f(m.yes_book.best_ask, '.3f')} | {_f(m.no_book.best_ask, '.3f')} |")
        a(f"| Midpoint | {_f(m.yes_mid, '.3f')} | {_f(m.no_mid, '.3f')} |")
        a(f"| Spread   | {_f(m.yes_spread, '.3f')} | {_f(m.no_spread, '.3f')} |")
        a(f"| Spread bps | {_f(m.yes_spread_bps, '.0f')} | — |")
        a("")
        a(f"**Hedge & paper-quote analysis**")
        a("")
        a(f"| Metric | Value |")
        a(f"|--------|-------|")
        a(f"| YES_ask + NO_ask (hedge cost) | {_f(m.hedge_cost, '.4f')} |")
        a(f"| Hedge profit (1 − cost) | {_f(m.hedge_profit, '+.4f')} |")
        a(f"| Hedge ≤ 1.00 | {'✅' if m.hedge_ok else '❌'} |")
        a(f"| Paper YES bid / ask | {_f(m.paper_yes_bid, '.3f')} / {_f(m.paper_yes_ask, '.3f')} |")
        a(f"| Paper NO bid / ask | {_f(m.paper_no_bid, '.3f')} / {_f(m.paper_no_ask, '.3f')} |")
        a(f"| Locked profit (both sides fill) | {_f(m.paper_locked_profit, '+.4f')} |")
        a(f"| Max one-side loss | {_f(m.paper_max_one_side_loss, '.4f')} |")
        a(f"| Maker rebate / unit (YES) | {_f(m.maker_rebate_per_unit, '.5f')} |")
        if m.notes:
            a("")
            a("**Notes**: " + " · ".join(m.notes))
        a("")

    a("---")
    a("*Read-only observer. No orders placed. No wallet required.*")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[out] Report → {report_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Polymarket rewards market-making observer (read-only)"
    )
    parser.add_argument("--limit", type=int, default=100, help="Max markets to fetch (default 100)")
    parser.add_argument("--out",   type=Path, default=DEFAULT_OUT, help="JSONL output path")
    parser.add_argument("--report", type=Path, default=REPORT_OUT, help="Markdown report path")
    args = parser.parse_args()

    analyses = asyncio.run(run(limit=args.limit, out_path=args.out))
    generate_report(analyses, args.report)

    # Print Top-10 to stdout
    print("\n" + "=" * 68)
    print("  TOP 10 MM CANDIDATES")
    print("=" * 68)
    print(f"  {'#':<3} {'Score':>5}  {'Risk':<6}  {'Hedge':>6}  {'Locked':>7}  Slug")
    print("-" * 68)
    for i, m in enumerate(analyses[:10], 1):
        h  = _f(m.hedge_cost, ".3f")
        lp = _f(m.paper_locked_profit, "+.4f")
        obs = "✅" if m.recommend_observe else "  "
        print(f"  {i:<3} {m.candidate_score:>5.1f}  {m.risk_score:<6}  {h:>6}  {lp:>7}  {obs} {m.slug[:42]}")
    print("=" * 68)


if __name__ == "__main__":
    main()
