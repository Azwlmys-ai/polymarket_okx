from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MarketSource(str, Enum):
    OKX = "okx"
    POLYMARKET = "polymarket"


class MarketSnapshot(BaseModel):
    ts_ms: int
    source: MarketSource
    market_id: str
    symbol: str | None = None
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    last: float | None = None
    liquidity: float | None = None
    volume_24h: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class LagRecord(BaseModel):
    ts_ms: int
    exchange_source: MarketSource
    prediction_source: MarketSource
    asset: str
    market_id: str
    exchange_move_ts_ms: int
    prediction_response_ts_ms: int
    lag_ms: int
    exchange_price_before: float | None = None
    exchange_price_after: float | None = None
    prediction_price_before: float | None = None
    prediction_price_after: float | None = None
    notes: str | None = None


class PaperTrade(BaseModel):
    opened_ts_ms: int
    market_id: str
    asset: str
    side: str
    entry_price: float
    notional: float
    quantity: float
    fees: float = 0.0
    slippage: float = 0.0
    reason: str
    status: str = "open"

