"""
rewards_mm_timeseries.py — multi-round orderbook time-series sampler for
Polymarket rewards market-making candidates.

Reads top candidates from rewards_mm_candidates.jsonl, then repeatedly fetches
live orderbook snapshots to verify whether the MM opportunity is stable across
time or a one-shot snapshot artefact.

NO TRADING. NO WALLET. NO PRIVATE KEYS.

Usage:
    # Quick single-round scan (test / preview):
    python3 research/rewards_mm_timeseries.py --once --top 10

    # Full 2-hour run (8 rounds × 15 min):
    python3 research/rewards_mm_timeseries.py --rounds 8 --interval-sec 900 --top 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev
from typing import Any

import aiohttp

# Ensure project root is importable when running as `python3 research/script.py`
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).parent.parent))

# reuse HTTP/book helpers from the existing observer
from research.polymarket_rewards_mm_observer import (
    _make_connector,
    fetch_clob_book,
    BookState,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_INPUT  = Path("research/rewards_mm_candidates.jsonl")
DEFAULT_OUT    = Path("research/rewards_mm_timeseries.jsonl")
DEFAULT_REPORT = Path("research/rewards_mm_timeseries_report.md")

DEFAULT_TOP      = 20
DEFAULT_ROUNDS   = 8
DEFAULT_INTERVAL = 900   # seconds between rounds (15 min)
MAX_WORKERS      = 12
DEPTH_PCT        = 0.05  # ± 5 % of midpoint for depth_near_mid


# ---------------------------------------------------------------------------
# Pure analysis functions (no I/O — fully unit-testable)
# ---------------------------------------------------------------------------

def compute_net_edge(
    hedge_cost: float | None,
    rebate_rate: float,
    taker_fee_rate: float,
) -> float | None:
    """
    Per-unit edge on a completed YES+NO round-trip, after maker rebates.

      net_edge = (yes_ask + no_ask - 1.0)          # spread profit (usually negative)
               + rebate_rate * taker_fee_rate        # rebate on YES fill
                 * (1 - yes_ask)
               + rebate_rate * taker_fee_rate        # rebate on NO fill
                 * (1 - no_ask)

    Simplified using hedge_cost = yes_ask + no_ask:
      net_edge = (hedge_cost - 1.0) + rebate_rate * taker_fee_rate * (2 - hedge_cost)

    Positive → profitable round-trip; negative → loss even with rebates.
    """
    if hedge_cost is None:
        return None
    spread_loss = hedge_cost - 1.0
    rebate_income = rebate_rate * taker_fee_rate * (2.0 - hedge_cost)
    return round(spread_loss + rebate_income, 7)


def compute_estimated_rebate(
    hedge_cost: float | None,
    rebate_rate: float,
    taker_fee_rate: float,
) -> float | None:
    """
    Total maker rebate received when BOTH YES and NO sides fill once.
    = rebate_rate * taker_fee_rate * (2 - hedge_cost)
    """
    if hedge_cost is None:
        return None
    return round(rebate_rate * taker_fee_rate * (2.0 - hedge_cost), 7)


def compute_depth_near_mid(
    bids: list[dict],
    asks: list[dict],
    midpoint: float | None,
    pct: float = DEPTH_PCT,
) -> float:
    """
    Total USD depth (price × size) on both sides within ±pct of midpoint.

    bids/asks are lists of {"price": ..., "size": ...} dicts.
    Returns 0.0 if midpoint is None or books are empty.
    """
    if not midpoint or midpoint <= 0:
        return 0.0
    lo = midpoint * (1.0 - pct)
    hi = midpoint * (1.0 + pct)
    total = 0.0
    for lvl in bids:
        try:
            p, s = float(lvl["price"]), float(lvl["size"])
            if lo <= p <= hi:
                total += p * s
        except (KeyError, ValueError, TypeError):
            pass
    for lvl in asks:
        try:
            p, s = float(lvl["price"]), float(lvl["size"])
            if lo <= p <= hi:
                total += p * s
        except (KeyError, ValueError, TypeError):
            pass
    return round(total, 2)


def round_stability(
    yes_ask_now: float | None,
    yes_bid_now: float | None,
    yes_ask_prev: float | None,
    yes_bid_prev: float | None,
) -> float:
    """
    Quote stability relative to previous round (0.0 – 1.0).

    1.0 = quotes identical to last round.
    0.0 = quotes changed by ≥ 10 % (or book unavailable).

    Uses YES side as representative; NO side assumed correlated.
    """
    if yes_ask_now is None or yes_bid_now is None:
        return 0.0
    if yes_ask_prev is None or yes_bid_prev is None:
        return 1.0   # first round — no comparison
    ask_chg = abs(yes_ask_now - yes_ask_prev) / max(abs(yes_ask_prev), 1e-9)
    bid_chg = abs(yes_bid_now - yes_bid_prev) / max(abs(yes_bid_prev), 1e-9)
    avg_chg = (ask_chg + bid_chg) / 2.0
    return round(max(0.0, 1.0 - avg_chg / 0.10), 4)


def hedge_cost_cv(hedge_costs: list[float]) -> float | None:
    """
    Coefficient of variation of hedge_cost across rounds.
    Lower = more stable. Returns None if < 2 data points.
    """
    if len(hedge_costs) < 2:
        return None
    m = mean(hedge_costs)
    if m == 0:
        return None
    return round(stdev(hedge_costs) / m, 6)


def stability_score(
    hedge_costs: list[float],
    spread_bps_list: list[float],
) -> float:
    """
    Aggregate stability score 0–100 across all rounds for one market.

    Components:
    - hedge_cost coefficient of variation (lower → more stable)
    - spread_bps range ratio = (max-min)/mean (lower → more stable)

    Score = 100 means perfectly stable; 0 means highly volatile.
    """
    if not hedge_costs:
        return 0.0
    m = mean(hedge_costs)
    hc_cv = (stdev(hedge_costs) / m) if len(hedge_costs) >= 2 and m > 0 else 0.0
    hc_penalty = min(hc_cv * 100.0, 1.0)   # 1% CV → 100% penalty at CV=0.01

    sp_penalty = 0.0
    if spread_bps_list and len(spread_bps_list) >= 2:
        sp_m = mean(spread_bps_list)
        sp_range = max(spread_bps_list) - min(spread_bps_list)
        sp_penalty = min(sp_range / sp_m if sp_m > 0 else 1.0, 1.0)

    raw = 1.0 - 0.6 * hc_penalty - 0.4 * sp_penalty
    return round(max(0.0, min(100.0, raw * 100.0)), 1)


def verdict_for(
    net_edge_mean: float | None,
    availability: float,
    stab: float,
    n_rounds: int,
    avg_depth_near_mid: float = 0.0,
    spread_bps_mean: float | None = None,
    hc_cv: float | None = None,
    rewards_avail: float = 0.0,
) -> str:
    """
    GO / WATCH / NO-GO classification with False-GO guardrails.

    GO hard conditions (ALL must hold):
      - net_edge_mean > 0
      - book availability >= 80%
      - rewards_availability >= 80%
      - avg_depth_near_mid > 0        ← non-zero tradeable depth
      - spread_bps_mean <= 300        ← spread not prohibitively wide
      - hedge_cost_cv < 0.01          ← stable pricing across rounds
      - stability_score >= 60

    WATCH: net_edge > 0 AND availability >= 50%,
           but at least one GO hard condition is missing.

    NO-GO: net_edge <= 0, availability < 50%, or rewards_availability < 50%.
    """
    if net_edge_mean is None or n_rounds == 0:
        return "NO-GO"

    # Hard NO-GO gates
    if net_edge_mean <= 0:
        return "NO-GO"
    if availability < 0.50:
        return "NO-GO"
    if rewards_avail < 0.50:
        return "NO-GO"

    # All GO conditions
    go_depth  = avg_depth_near_mid > 0
    go_spread = spread_bps_mean is not None and spread_bps_mean <= 300
    go_cv     = hc_cv is not None and hc_cv < 0.01
    go_rew    = rewards_avail >= 0.80
    go_avail  = availability >= 0.80
    go_stab   = stab >= 60

    if go_depth and go_spread and go_cv and go_rew and go_avail and go_stab:
        return "GO"

    # Positive edge + baseline availability → WATCH
    return "WATCH"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Sample:
    """One snapshot of one market at one point in time."""
    ts: str
    slug: str
    round_n: int

    # live book
    yes_bid: float | None
    yes_ask: float | None
    no_bid:  float | None
    no_ask:  float | None

    # derived from live books
    midpoint:     float | None
    spread_bps:   float | None
    hedge_cost:   float | None
    locked_profit: float | None   # yes_ask + no_ask - 1.0
    estimated_rebate: float | None
    net_edge:     float | None    # locked_profit + estimated_rebate
    depth_near_mid: float

    # book availability flags
    has_yes_book: bool
    has_no_book:  bool

    # static from candidate (doesn't change across rounds)
    rewards_min_size:  float
    rewards_max_spread: float
    rebate_rate:       float
    taker_fee_rate:    float
    has_rewards:       bool
    neg_risk:          bool

    # per-round stability vs. previous round (1.0 if first round)
    quote_stability_score: float = 1.0

    # fetch metadata
    yes_fetch_error: str | None = None
    no_fetch_error:  str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MarketStats:
    """Aggregated statistics for one market across all rounds."""
    slug: str
    n_rounds: int

    availability: float           # fraction of rounds with valid YES book

    hedge_cost_mean: float | None
    hedge_cost_min:  float | None
    hedge_cost_max:  float | None
    hedge_cost_std:  float | None
    hedge_cost_cv:   float | None

    spread_bps_mean: float | None
    spread_bps_min:  float | None
    spread_bps_max:  float | None

    net_edge_mean: float | None
    net_edge_min:  float | None
    net_edge_max:  float | None

    rewards_availability: float   # fraction of rounds where has_rewards=True
    avg_depth_near_mid:   float

    stability_score: float        # 0–100
    verdict: str                  # GO / WATCH / NO-GO

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_samples(slug: str, samples: list[Sample]) -> MarketStats:
    """Build per-market statistics from a list of per-round samples."""
    valid = [s for s in samples if s.has_yes_book]
    avail = len(valid) / len(samples) if samples else 0.0

    def _stats(vals: list[float]) -> tuple:
        if not vals:
            return None, None, None, None
        m = mean(vals)
        mn, mx = min(vals), max(vals)
        sd = stdev(vals) if len(vals) >= 2 else 0.0
        return round(m, 6), round(mn, 6), round(mx, 6), round(sd, 6)

    hc_vals = [s.hedge_cost for s in valid if s.hedge_cost is not None]
    sp_vals  = [s.spread_bps for s in valid if s.spread_bps is not None]
    ne_vals  = [s.net_edge for s in valid if s.net_edge is not None]
    depth_vals = [s.depth_near_mid for s in valid]

    hc_m, hc_mn, hc_mx, hc_sd = _stats(hc_vals)
    sp_m, sp_mn, sp_mx, _     = _stats(sp_vals)
    ne_m, ne_mn, ne_mx, _     = _stats(ne_vals)

    hc_cv_val = hedge_cost_cv(hc_vals)
    stab      = stability_score(hc_vals, sp_vals)
    rew_avail = mean([1.0 if s.has_rewards else 0.0 for s in samples]) if samples else 0.0
    avg_depth = round(mean(depth_vals), 2) if depth_vals else 0.0
    ver       = verdict_for(
        ne_m, avail, stab, len(samples),
        avg_depth_near_mid=avg_depth,
        spread_bps_mean=sp_m,
        hc_cv=hc_cv_val,
        rewards_avail=rew_avail,
    )

    return MarketStats(
        slug=slug,
        n_rounds=len(samples),
        availability=round(avail, 4),
        hedge_cost_mean=hc_m,
        hedge_cost_min=hc_mn,
        hedge_cost_max=hc_mx,
        hedge_cost_std=hc_sd,
        hedge_cost_cv=hc_cv_val,
        spread_bps_mean=sp_m,
        spread_bps_min=sp_mn,
        spread_bps_max=sp_mx,
        net_edge_mean=ne_m,
        net_edge_min=ne_mn,
        net_edge_max=ne_mx,
        rewards_availability=round(rew_avail, 4),
        avg_depth_near_mid=avg_depth,
        stability_score=stab,
        verdict=ver,
    )


# ---------------------------------------------------------------------------
# Live snapshot
# ---------------------------------------------------------------------------

def _bids_asks_raw(book: BookState) -> tuple[list[dict], list[dict]]:
    return (
        [{"price": str(lv.price), "size": str(lv.size)} for lv in book.bids],
        [{"price": str(lv.price), "size": str(lv.size)} for lv in book.asks],
    )


async def sample_market(
    session: aiohttp.ClientSession,
    candidate: dict,
    round_n: int,
    prev: Sample | None,
    semaphore: asyncio.Semaphore,
) -> Sample:
    """Fetch fresh YES/NO books and build a Sample."""
    yes_tok = candidate.get("yes_token_id", "")
    no_tok  = candidate.get("no_token_id",  "")
    slug    = candidate.get("slug", "unknown")

    async with semaphore:
        yes_b, no_b = await asyncio.gather(
            fetch_clob_book(session, yes_tok) if yes_tok else _empty_book(yes_tok),
            fetch_clob_book(session, no_tok)  if no_tok  else _empty_book(no_tok),
        )

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    yes_bid = yes_b.best_bid
    yes_ask = yes_b.best_ask
    no_bid  = no_b.best_bid
    no_ask  = no_b.best_ask

    mid    = yes_b.midpoint
    sp_bps = yes_b.spread_bps

    hc = (yes_ask + no_ask) if (yes_ask is not None and no_ask is not None) else None
    locked = round(hc - 1.0, 7) if hc is not None else None

    rebate_r = candidate.get("rebate_rate", 0.25)
    taker_r  = candidate.get("taker_fee_rate", 0.05)

    est_reb = compute_estimated_rebate(hc, rebate_r, taker_r)
    net     = compute_net_edge(hc, rebate_r, taker_r)

    bids_raw, asks_raw = _bids_asks_raw(yes_b)
    depth = compute_depth_near_mid(bids_raw, asks_raw, mid)

    has_rewards = (
        (candidate.get("rewards_min_size") or 0) > 0
        or (candidate.get("rewards_max_spread") or 0) > 0
    )

    stab = round_stability(
        yes_ask, yes_bid,
        prev.yes_ask if prev else None,
        prev.yes_bid if prev else None,
    )

    return Sample(
        ts=ts,
        slug=slug,
        round_n=round_n,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        midpoint=mid,
        spread_bps=sp_bps,
        hedge_cost=hc,
        locked_profit=locked,
        estimated_rebate=est_reb,
        net_edge=net,
        depth_near_mid=depth,
        has_yes_book=yes_bid is not None,
        has_no_book=no_bid is not None,
        rewards_min_size=candidate.get("rewards_min_size") or 0.0,
        rewards_max_spread=candidate.get("rewards_max_spread") or 0.0,
        rebate_rate=rebate_r,
        taker_fee_rate=taker_r,
        has_rewards=has_rewards,
        neg_risk=bool(candidate.get("neg_risk") or False),
        quote_stability_score=stab,
        yes_fetch_error=yes_b.fetch_error,
        no_fetch_error=no_b.fetch_error,
    )


async def _empty_book(token_id: str) -> BookState:
    return BookState(token_id=token_id, fetch_error="no token id")


# ---------------------------------------------------------------------------
# JSONL persistence
# ---------------------------------------------------------------------------

def _append_samples(samples: list[Sample], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s.to_dict()) + "\n")


def load_samples(path: Path) -> list[Sample]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                out.append(Sample(**d))
            except Exception:
                pass
    return out


def load_candidates(path: Path, top: int) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    records.sort(key=lambda r: r.get("candidate_score", 0), reverse=True)
    return records[:top]


# ---------------------------------------------------------------------------
# Main async runner
# ---------------------------------------------------------------------------

async def run_round(
    candidates: list[dict],
    session: aiohttp.ClientSession,
    round_n: int,
    prev_by_slug: dict[str, Sample],
) -> list[Sample]:
    sem = asyncio.Semaphore(MAX_WORKERS)
    tasks = [
        sample_market(session, cand, round_n, prev_by_slug.get(cand["slug"]), sem)
        for cand in candidates
    ]
    return list(await asyncio.gather(*tasks))


async def run(
    candidates: list[dict],
    out_path: Path,
    rounds: int,
    interval_s: int,
    once: bool,
) -> list[Sample]:
    all_samples: list[Sample] = []
    prev_by_slug: dict[str, Sample] = {}

    connector = _make_connector()
    headers = {"User-Agent": "polymarket-okx-research/2.0 (timeseries-observer)"}
    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        n_rounds = 1 if once else rounds
        for r in range(n_rounds):
            ts_now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[round {r+1}/{n_rounds}  {ts_now}] sampling {len(candidates)} markets…")
            samples = await run_round(candidates, session, r, prev_by_slug)
            all_samples.extend(samples)
            _append_samples(samples, out_path)
            for s in samples:
                prev_by_slug[s.slug] = s
            # brief per-round summary
            valid = [s for s in samples if s.has_yes_book]
            net_pos = sum(1 for s in valid if (s.net_edge or 0) > 0)
            print(
                f"         {len(valid)}/{len(samples)} books ok  "
                f"net_edge>0: {net_pos}  "
                f"avg_hedge={mean([s.hedge_cost for s in valid if s.hedge_cost]) if valid else 'N/A'}"
            )
            if r < n_rounds - 1:
                print(f"         sleeping {interval_s}s…")
                await asyncio.sleep(interval_s)

    return all_samples


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _flt(v: Any, fmt: str = ".4f") -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:{fmt}}"


def generate_report(all_samples: list[Sample], report_path: Path) -> list[MarketStats]:
    from itertools import groupby

    by_slug: dict[str, list[Sample]] = {}
    for s in all_samples:
        by_slug.setdefault(s.slug, []).append(s)

    stats_list = [aggregate_samples(slug, samps) for slug, samps in by_slug.items()]
    stats_list.sort(key=lambda s: (
        0 if s.verdict == "GO" else (1 if s.verdict == "WATCH" else 2),
        -(s.net_edge_mean or -99),
        -s.stability_score,
    ))

    lines: list[str] = []
    a = lines.append
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    a("# Polymarket Rewards MM — Time-Series Stability Report")
    a("")
    a(f"> Generated: {ts}")
    a(f"> Total samples: {len(all_samples)}")
    a(f"> Markets tracked: {len(by_slug)}")
    a(f"> Rounds: {max((s.round_n for s in all_samples), default=0) + 1}")
    a("")

    # Summary
    go    = [s for s in stats_list if s.verdict == "GO"]
    watch = [s for s in stats_list if s.verdict == "WATCH"]
    nogo  = [s for s in stats_list if s.verdict == "NO-GO"]
    a("## Summary")
    a("")
    a(f"| Verdict | Count |")
    a(f"|---------|-------|")
    a(f"| ✅ GO    | {len(go)} |")
    a(f"| 👁 WATCH | {len(watch)} |")
    a(f"| ❌ NO-GO | {len(nogo)} |")
    a("")

    # Stability ranking table
    a("## Stability Ranking (all markets)")
    a("")
    a("| # | Slug | Verdict | Avail | HC mean | HC cv | Net edge | Stab | SpBps |")
    a("|---|------|---------|-------|---------|-------|----------|------|-------|")
    for i, s in enumerate(stats_list, 1):
        icon = "✅" if s.verdict == "GO" else ("👁" if s.verdict == "WATCH" else "❌")
        a(
            f"| {i} "
            f"| `{s.slug[:38]}` "
            f"| {icon} {s.verdict} "
            f"| {s.availability:.0%} "
            f"| {_flt(s.hedge_cost_mean, '.4f')} "
            f"| {_flt(s.hedge_cost_cv, '.5f')} "
            f"| {_flt(s.net_edge_mean, '+.5f')} "
            f"| {s.stability_score:.1f} "
            f"| {_flt(s.spread_bps_mean, '.0f')} |"
        )
    a("")

    # Detailed cards per market
    a("## Detailed Market Analysis")
    a("")
    for s in stats_list:
        icon = "✅ GO" if s.verdict == "GO" else ("👁 WATCH" if s.verdict == "WATCH" else "❌ NO-GO")
        a(f"### `{s.slug}` — {icon}")
        a("")
        a(f"| Metric | Value |")
        a(f"|--------|-------|")
        a(f"| Rounds sampled | {s.n_rounds} |")
        a(f"| Book availability | {s.availability:.0%} |")
        a(f"| Rewards availability | {s.rewards_availability:.0%} |")
        a(f"| Avg depth near mid | ${s.avg_depth_near_mid:,.0f} |")
        a(f"| **Hedge cost** | mean={_flt(s.hedge_cost_mean, '.5f')} min={_flt(s.hedge_cost_min, '.5f')} max={_flt(s.hedge_cost_max, '.5f')} |")
        a(f"| Hedge cost CV | {_flt(s.hedge_cost_cv, '.5f')} |")
        a(f"| Spread bps | mean={_flt(s.spread_bps_mean, '.0f')} min={_flt(s.spread_bps_min, '.0f')} max={_flt(s.spread_bps_max, '.0f')} |")
        a(f"| **Net edge / round-trip** | mean={_flt(s.net_edge_mean, '+.5f')} min={_flt(s.net_edge_min, '+.5f')} max={_flt(s.net_edge_max, '+.5f')} |")
        a(f"| Quote stability score | {s.stability_score:.1f} / 100 |")
        a(f"| **Verdict** | **{icon}** |")
        a("")

    # Verdict explanations
    a("## Verdict Criteria")
    a("")
    a("| Verdict | Conditions (ALL required for GO; ANY sufficient for NO-GO) |")
    a("|---------|--------------------------------------------------------------|")
    a("| ✅ GO | net_edge > 0, book_avail ≥ 80%, rewards_avail ≥ 80%, **depth > 0**, **spread_bps ≤ 300**, **hedge_cost_cv < 0.01**, stability ≥ 60 |")
    a("| 👁 WATCH | net_edge > 0, book_avail ≥ 50% — but at least one GO condition fails |")
    a("| ❌ NO-GO | net_edge ≤ 0, OR book_avail < 50%, OR rewards_avail < 50% |")
    a("")
    a("*With only 1 round, cv=None and stability=100 by definition — run ≥ 2 rounds for meaningful variance.*")
    a("")
    a("## False-GO Guardrails")
    a("")
    a("Earlier versions classified markets as GO based solely on net_edge, availability, and stability. "
      "This allowed markets with **zero tradeable depth** and **spreads of 1000–6000+ bps** to reach GO, "
      "which is misleading: the positive net_edge is real on paper but *unreachable* when no orders sit near the midpoint.")
    a("")
    a("Three new hard gates now block GO:")
    a("")
    a("| Gate | Threshold | Rationale |")
    a("|------|-----------|-----------|")
    a("| `avg_depth_near_mid > 0` | any USD depth ±5% of mid | Zero depth = no fills possible, reward is theoretical |")
    a("| `spread_bps_mean ≤ 300` | ≤ 300 bps | Spreads > 300 bps signal illiquid or speculative books with high adverse-selection risk |")
    a("| `hedge_cost_cv < 0.01` | CV < 1% | Hedge cost instability implies the spread-profit estimate is noisy and unreliable |")
    a("")
    a("Markets that are stable and rebate-positive but fail these gates are demoted to **WATCH**: "
      "worth monitoring, but not ready for paper quoting.")
    a("")
    a("---")
    a("*Read-only observer. No orders placed. No wallet required.*")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[out] Report → {report_path}")
    return stats_list


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Polymarket rewards MM time-series sampler (read-only)"
    )
    parser.add_argument("--input",        type=Path,  default=DEFAULT_INPUT)
    parser.add_argument("--out",          type=Path,  default=DEFAULT_OUT)
    parser.add_argument("--report",       type=Path,  default=DEFAULT_REPORT)
    parser.add_argument("--top",          type=int,   default=DEFAULT_TOP)
    parser.add_argument("--interval-sec", type=int,   default=DEFAULT_INTERVAL)
    parser.add_argument("--rounds",       type=int,   default=DEFAULT_ROUNDS)
    parser.add_argument("--once",         action="store_true",
                        help="Single round then exit (--rounds ignored)")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERROR: {args.input} not found. Run polymarket_rewards_mm_observer.py first.")
        sys.exit(1)

    candidates = load_candidates(args.input, args.top)
    print(f"[init] Loaded {len(candidates)} candidates from {args.input}")

    all_samples = asyncio.run(run(
        candidates=candidates,
        out_path=args.out,
        rounds=args.rounds,
        interval_s=args.interval_sec,
        once=args.once,
    ))

    stats = generate_report(all_samples, args.report)

    # stdout summary
    print(f"\n{'='*65}")
    print("  TIME-SERIES SUMMARY")
    print(f"{'='*65}")
    print(f"  {'Slug':<44}  {'Verdict':<8}  {'NetEdge':>9}  {'Stab':>5}")
    print(f"  {'-'*44}  {'-'*8}  {'-'*9}  {'-'*5}")
    for s in stats[:args.top]:
        ne = _flt(s.net_edge_mean, "+.5f") if s.net_edge_mean is not None else "    —"
        print(f"  {s.slug[:44]:<44}  {s.verdict:<8}  {ne:>9}  {s.stability_score:>5.1f}")
    print(f"{'='*65}")


if __name__ == "__main__":
    main()
