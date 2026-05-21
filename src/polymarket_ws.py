"""
src/polymarket_ws.py — Polymarket CLOB market WebSocket client (read-only).

Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market and streams
real-time price events for subscribed token IDs.

Supported event types:
  book            — full orderbook snapshot (sent on subscribe + on changes)
  best_bid_ask    — lightweight bid/ask update (sub-second latency)
  price_change    — YES/NO price deltas across multiple outcomes
  last_trade_price — executed trade price
  new_market      — admin/metadata event (ignored for price purposes)

Output: PolyWSEvent dataclass with ms-precision timestamp.

NO AUTHENTICATION. NO ORDER PLACEMENT. NO WALLET. READ-ONLY.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)

POLY_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------

@dataclass
class PolyWSEvent:
    """
    Unified price event from the Polymarket CLOB market WebSocket.

    ts_ms        : wall-clock timestamp in milliseconds (from WS message)
    market_id    : condition_id hex (e.g. '0xabcd…')
    token_id     : asset_id (decimal token ID string) for the affected side
    yes_price    : best estimate of YES price (mid when bid+ask available)
    best_bid     : best bid price for this token (None if not in event)
    best_ask     : best ask price for this token (None if not in event)
    mid          : (best_bid + best_ask) / 2 when both present, else None
    last_trade   : last executed trade price (None if not in event)
    source       : raw event_type string
    """
    ts_ms:      int
    market_id:  str
    token_id:   str
    yes_price:  Optional[float]
    best_bid:   Optional[float]
    best_ask:   Optional[float]
    mid:        Optional[float]
    last_trade: Optional[float]
    source:     str   # "book" | "best_bid_ask" | "last_trade_price" | "price_change"


# ---------------------------------------------------------------------------
# Pure parsing functions (no I/O — fully unit-testable)
# ---------------------------------------------------------------------------

def _safe_float(v: Any) -> Optional[float]:
    """Convert str/int/float → float. Returns None on any failure."""
    try:
        f = float(v)
        return f if 0.0 <= f <= 1.0 else None   # prices must be valid probabilities
    except (TypeError, ValueError):
        return None


def _parse_levels(raw: list) -> list[tuple[float, float]]:
    """
    Parse a list of {"price": str, "size": str} dicts.

    Returns sorted list of (price, size) tuples. Malformed entries are skipped.
    """
    levels: list[tuple[float, float]] = []
    for entry in raw:
        try:
            p = float(entry["price"])
            s = float(entry["size"])
            if 0.0 < p <= 1.0 and s >= 0:
                levels.append((p, s))
        except (KeyError, TypeError, ValueError):
            continue
    return levels


def _mid(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is not None and ask is not None:
        return round((bid + ask) / 2.0, 6)
    if bid is not None:
        return bid
    if ask is not None:
        return ask
    return None


def parse_book_event(evt: dict) -> Optional[PolyWSEvent]:
    """
    Parse a 'book' event (full orderbook snapshot).

    Extracts best bid / best ask from sorted bids/asks lists,
    plus last_trade_price if present.
    """
    try:
        token_id  = str(evt.get("asset_id") or "")
        market_id = str(evt.get("market") or "")
        ts_ms     = int(evt.get("timestamp") or 0)

        if not token_id or not market_id or ts_ms == 0:
            return None

        raw_bids = evt.get("bids") or []
        raw_asks = evt.get("asks") or []

        bids = sorted(_parse_levels(raw_bids), key=lambda x: x[0], reverse=True)
        asks = sorted(_parse_levels(raw_asks), key=lambda x: x[0])

        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        mid      = _mid(best_bid, best_ask)

        lt_raw     = evt.get("last_trade_price")
        last_trade = _safe_float(lt_raw)

        yes_price  = mid if mid is not None else last_trade

        return PolyWSEvent(
            ts_ms=ts_ms, market_id=market_id, token_id=token_id,
            yes_price=yes_price, best_bid=best_bid, best_ask=best_ask,
            mid=mid, last_trade=last_trade, source="book",
        )
    except Exception:
        return None


def parse_best_bid_ask_event(evt: dict) -> Optional[PolyWSEvent]:
    """
    Parse a 'best_bid_ask' event (lightweight bid/ask update).

    This is the most frequent and most time-precise event type.
    """
    try:
        token_id  = str(evt.get("asset_id") or "")
        market_id = str(evt.get("market") or "")
        ts_ms     = int(evt.get("timestamp") or 0)

        if not token_id or not market_id or ts_ms == 0:
            return None

        best_bid = _safe_float(evt.get("best_bid"))
        best_ask = _safe_float(evt.get("best_ask"))
        mid      = _mid(best_bid, best_ask)

        return PolyWSEvent(
            ts_ms=ts_ms, market_id=market_id, token_id=token_id,
            yes_price=mid, best_bid=best_bid, best_ask=best_ask,
            mid=mid, last_trade=None, source="best_bid_ask",
        )
    except Exception:
        return None


def parse_last_trade_event(evt: dict) -> Optional[PolyWSEvent]:
    """
    Parse a 'last_trade_price' event (individual executed trade).

    Provides executed price, side (BUY/SELL), and size.
    """
    try:
        token_id  = str(evt.get("asset_id") or "")
        market_id = str(evt.get("market") or "")
        ts_ms     = int(evt.get("timestamp") or 0)

        if not token_id or not market_id or ts_ms == 0:
            return None

        last_trade = _safe_float(evt.get("price"))

        return PolyWSEvent(
            ts_ms=ts_ms, market_id=market_id, token_id=token_id,
            yes_price=last_trade, best_bid=None, best_ask=None,
            mid=None, last_trade=last_trade, source="last_trade_price",
        )
    except Exception:
        return None


def parse_price_change_event(evt: dict) -> list[PolyWSEvent]:
    """
    Parse a 'price_change' event (may contain updates for multiple outcomes).

    Returns one PolyWSEvent per affected asset_id (may be empty).
    """
    out: list[PolyWSEvent] = []
    try:
        market_id = str(evt.get("market") or "")
        ts_ms     = int(evt.get("timestamp") or 0)
        if not market_id or ts_ms == 0:
            return []

        changes = evt.get("price_changes") or []
        for ch in changes:
            try:
                token_id = str(ch.get("asset_id") or "")
                if not token_id:
                    continue
                price = _safe_float(ch.get("price"))
                bid   = _safe_float(ch.get("best_bid"))
                ask   = _safe_float(ch.get("best_ask"))
                mid   = _mid(bid, ask)
                yes_p = mid if mid is not None else price
                if yes_p is None and bid is None and ask is None:
                    continue   # no usable price data — skip silently
                out.append(PolyWSEvent(
                    ts_ms=ts_ms, market_id=market_id, token_id=token_id,
                    yes_price=yes_p, best_bid=bid, best_ask=ask,
                    mid=mid, last_trade=None, source="price_change",
                ))
            except Exception:
                continue
    except Exception:
        pass
    return out


def parse_ws_message(raw: Any) -> list[PolyWSEvent]:
    """
    Top-level parser for a single WS message payload.

    raw may be a dict (single event) or a list of dicts.
    Unknown or malformed events are silently skipped.
    Returns a (possibly empty) list of PolyWSEvent objects.
    """
    if raw is None:
        return []

    events: list[dict] = raw if isinstance(raw, list) else [raw]
    out: list[PolyWSEvent] = []

    for evt in events:
        if not isinstance(evt, dict):
            continue
        etype = evt.get("event_type", "")

        if etype == "book":
            ev = parse_book_event(evt)
            if ev:
                out.append(ev)
        elif etype == "best_bid_ask":
            ev = parse_best_bid_ask_event(evt)
            if ev:
                out.append(ev)
        elif etype == "last_trade_price":
            ev = parse_last_trade_event(evt)
            if ev:
                out.append(ev)
        elif etype == "price_change":
            out.extend(parse_price_change_event(evt))
        # new_market and unknown types are ignored (not price-relevant)

    return out


# ---------------------------------------------------------------------------
# Async WebSocket connector
# ---------------------------------------------------------------------------

async def polymarket_ws_task(
    token_ids: list[str],
    on_event,                    # async callable: (PolyWSEvent) -> None
    shutdown,                    # asyncio.Event
    ssl_ctx=None,
) -> None:
    """
    Persistent WebSocket connection to the Polymarket CLOB market channel.

    Subscribes to *token_ids* (YES + NO tokens for each market).
    Calls *on_event(ev)* for every parsed PolyWSEvent.
    Reconnects on disconnect until *shutdown* is set.

    ssl_ctx: optional SSL context (useful to bypass cert verification on macOS).

    NO AUTHENTICATION. NO ORDER PLACEMENT. READ-ONLY.
    """
    import asyncio
    import json

    import aiohttp

    if ssl_ctx is None:
        import ssl
        ssl_ctx = ssl.create_default_context()
        try:
            import certifi
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode    = ssl.CERT_NONE

    sub_payload = json.dumps({
        "assets_ids":            token_ids,
        "type":                  "market",
        "custom_feature_enabled": True,
    })

    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    reconnects = 0

    async with aiohttp.ClientSession(connector=connector) as session:
        while not shutdown.is_set():
            try:
                async with session.ws_connect(
                    POLY_MARKET_WS_URL,
                    heartbeat=25,
                    timeout=aiohttp.ClientTimeout(connect=15, total=None),
                ) as ws:
                    await ws.send_str(sub_payload)
                    log.info(
                        "[poly-ws] connected (reconnect #%d), subscribed %d tokens",
                        reconnects, len(token_ids),
                    )

                    async for msg in ws:
                        if shutdown.is_set():
                            return
                        if msg.type not in (
                            aiohttp.WSMsgType.TEXT,
                            aiohttp.WSMsgType.BINARY,
                        ):
                            if msg.type in (
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                log.debug("[poly-ws] %s — reconnecting", msg.type)
                                break
                            continue

                        try:
                            raw = json.loads(
                                msg.data if msg.type == aiohttp.WSMsgType.TEXT
                                else msg.data.decode()
                            )
                        except Exception:
                            continue

                        parsed = parse_ws_message(raw)
                        for ev in parsed:
                            try:
                                await on_event(ev)
                            except Exception as exc:
                                log.debug("[poly-ws] on_event error: %s", exc)

            except Exception as exc:
                if not shutdown.is_set():
                    reconnects += 1
                    log.warning("[poly-ws] error (%s) — reconnect #%d in 3s", exc, reconnects)
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(shutdown.wait()), timeout=3.0
                        )
                    except asyncio.TimeoutError:
                        pass
