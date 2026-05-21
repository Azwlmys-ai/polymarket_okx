"""
research/models.py — Research data models for microstructure & event alpha.

STATS_ONLY — No real orders. No capital at risk.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class MarketPhase(str, Enum):
    EARLY = "early"          # >2h to expiry
    MID = "mid"              # 30min-2h to expiry
    LATE = "late"            # 60s-30min to expiry
    SETTLEMENT = "settlement"  # <=60s to expiry


class VolatilityRegime(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SignalDirection(str, Enum):
    JUMP = "jump"
    DROP = "drop"
    NEUTRAL = "neutral"


class ExperimentType(str, Enum):
    SETTLEMENT_REVERSION = "settlement_reversion"
    POLY_PRICE_LAG = "poly_price_lag"
    SPREAD_DISTORTION = "spread_distortion"
    REFERENCE_MISPRICING = "reference_mispricing"
    NEWS_EVENT = "news_event"


# ─────────────────────────────────────────────────────────────────────────────
# Core research signal — records every microstructure/event observation
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ResearchSignal:
    """One tagged observation with full feature vector."""

    # ── Identity ───────────────────────────────────────────────────────────────
    timestamp: float           # unix epoch (wall clock)
    market_id: str
    market_question: str
    experiment: ExperimentType

    # ── Time to expiry ─────────────────────────────────────────────────────────
    time_to_expiry_s: float

    # ── Polymarket state ───────────────────────────────────────────────────────
    poly_yes_price: float
    poly_no_price: float
    poly_spread: float         # no - (1-yes) or actual bid-ask if available
    poly_volume: float
    poly_last_price_change: float  # change in YES over last poll interval

    # ── BTC spot state ─────────────────────────────────────────────────────────
    btc_price: float
    btc_30s_return: Optional[float] = None
    btc_60s_return: Optional[float] = None
    btc_120s_return: Optional[float] = None
    btc_volatility_60s: Optional[float] = None

    # ── Cross-exchange reference pricing ─────────────────────────────────────
    okx_price: Optional[float] = None
    binance_price: Optional[float] = None
    bybit_price: Optional[float] = None
    median_price: Optional[float] = None
    price_to_beat: Optional[float] = None
    distance_to_beat_bps: Optional[float] = None
    exchange_spread_bps: Optional[float] = None
    direction_consensus: Optional[str] = None
    poly_midpoint: Optional[float] = None
    mispricing_bps: Optional[float] = None

    # ── Signal metadata ────────────────────────────────────────────────────────
    signal_direction: SignalDirection = SignalDirection.NEUTRAL
    signal_strength: float = 0.0       # normalized 0–1
    trigger_reason: str = ""

    # ── Tags ───────────────────────────────────────────────────────────────────
    market_phase: MarketPhase = MarketPhase.EARLY
    volatility_regime: VolatilityRegime = VolatilityRegime.MEDIUM

    # ── Forward outcomes (filled later) ────────────────────────────────────────
    btc_return_15s: Optional[float] = None
    btc_return_30s: Optional[float] = None
    btc_return_60s: Optional[float] = None
    btc_return_300s: Optional[float] = None
    poly_return_15s: Optional[float] = None
    poly_return_30s: Optional[float] = None
    poly_return_60s: Optional[float] = None
    poly_return_300s: Optional[float] = None

    # ── Lead-lag specific (Experiment B) ───────────────────────────────────────
    poly_move_timestamp: Optional[float] = None
    okx_move_timestamp: Optional[float] = None
    lead_lag_ms: Optional[float] = None  # positive = Poly leads

    # ── Spread distortion specific (Experiment C) ──────────────────────────────
    spread_before: Optional[float] = None
    spread_after: Optional[float] = None
    liquidity_before: Optional[float] = None
    liquidity_after: Optional[float] = None

    def to_dict(self) -> dict:
        """Serialize to dict for JSON output."""
        d = {}
        for k, v in self.__dict__.items():
            if isinstance(v, Enum):
                d[k] = v.value
            elif v is not None:
                d[k] = v
            else:
                d[k] = None
        d["timestamp_iso"] = datetime.fromtimestamp(
            self.timestamp, tz=timezone.utc
        ).isoformat()
        return d

    def to_jsonl(self) -> str:
        """Serialize to JSONL line (compact, no extra whitespace)."""
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


# ─────────────────────────────────────────────────────────────────────────────
# News event model (Phase 2)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class NewsEvent:
    """Lightweight news/event observation."""

    event_timestamp: float           # unix epoch
    event_source: str                # "twitter", "rss", "manual", etc.
    event_type: str                  # "etf", "fed", "hack", "liquidation", "macro", "other"
    headline: str                    # short text summary
    event_sentiment: str             # "bullish", "bearish", "neutral"
    keywords: list[str] = field(default_factory=list)

    # ── Outcome tracking (filled after window) ─────────────────────────────────
    btc_price_at_event: Optional[float] = None
    btc_move_after_60s: Optional[float] = None
    btc_move_after_300s: Optional[float] = None
    poly_yes_price_at_event: Optional[float] = None
    poly_price_change: Optional[float] = None
    poly_market_id: Optional[str] = None

    # ── Comparison: who moved first? ───────────────────────────────────────────
    poly_first_move_ms: Optional[float] = None   # ms Poly moved before OKX
    okx_first_move_ms: Optional[float] = None    # ms OKX moved before Poly

    def to_dict(self) -> dict:
        import json
        d = {}
        for k, v in self.__dict__.items():
            if v is not None:
                d[k] = v
            else:
                d[k] = None
        d["event_timestamp_iso"] = datetime.fromtimestamp(
            self.event_timestamp, tz=timezone.utc
        ).isoformat()
        return d

    def to_jsonl(self) -> str:
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


# ─────────────────────────────────────────────────────────────────────────────
# Experiment result aggregation
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ExperimentSummary:
    """Aggregated statistics for one experiment type."""

    experiment: ExperimentType
    signal_count: int = 0
    regime_breakdown: dict[str, int] = field(default_factory=dict)  # phase -> count
    vol_breakdown: dict[str, int] = field(default_factory=dict)     # regime -> count

    # Core statistics
    win_rate: Optional[float] = None
    mean_return: Optional[float] = None
    median_return: Optional[float] = None
    max_drawdown: Optional[float] = None
    expectancy: Optional[float] = None    # avg(return * direction)

    # Fee-adjusted (assume 2% round-trip on Poly + 0.1% on OKX)
    fee_adjusted_pnl: Optional[float] = None

    # Breakdowns
    top_conditions: list[dict] = field(default_factory=list)
    worst_conditions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "experiment": self.experiment.value,
            "signal_count": self.signal_count,
            "regime_breakdown": self.regime_breakdown,
            "vol_breakdown": self.vol_breakdown,
            "win_rate": self.win_rate,
            "mean_return": self.mean_return,
            "median_return": self.median_return,
            "max_drawdown": self.max_drawdown,
            "expectancy": self.expectancy,
            "fee_adjusted_pnl": self.fee_adjusted_pnl,
            "top_conditions": self.top_conditions,
            "worst_conditions": self.worst_conditions,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Raw event (before tagging)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class RawEvent:
    """Minimal raw observation before feature extraction and tagging."""

    timestamp: float          # unix epoch
    event_type: str           # "tick", "trade", "orderbook", "settlement"
    market_id: str
    data: dict = field(default_factory=dict)  # Arbitrary payload

    def to_dict(self) -> dict:
        d = {"timestamp": self.timestamp, "event_type": self.event_type,
             "market_id": self.market_id, "data": self.data}
        d["timestamp_iso"] = datetime.fromtimestamp(
            self.timestamp, tz=timezone.utc
        ).isoformat()
        return d

    def to_jsonl(self) -> str:
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


# ─────────────────────────────────────────────────────────────────────────────
# Helper: classify market phase from TTL
# ─────────────────────────────────────────────────────────────────────────────


def classify_market_phase(ttl_s: float) -> MarketPhase:
    """Classify market phase based on seconds to expiry."""
    if ttl_s <= 60:
        return MarketPhase.SETTLEMENT
    elif ttl_s <= 1800:      # 30 min
        return MarketPhase.LATE
    elif ttl_s <= 7200:      # 2 hours
        return MarketPhase.MID
    else:
        return MarketPhase.EARLY


def classify_volatility_regime(vol_60s: Optional[float]) -> VolatilityRegime:
    """
    Classify volatility regime from 60s BTC volatility.

    Thresholds (BTC minute-level):
      - low:  < 0.05%  (5 bps)
      - high: > 0.20%  (20 bps)
    """
    if vol_60s is None:
        return VolatilityRegime.MEDIUM
    if vol_60s < 0.0005:
        return VolatilityRegime.LOW
    elif vol_60s > 0.0020:
        return VolatilityRegime.HIGH
    else:
        return VolatilityRegime.MEDIUM
