"""
src/bybit_client.py — Bybit V5 public trade stream client (read-only, stats only).

STATS_ONLY. No orders. No API keys. No authentication required.
Public trade streams are unauthenticated and used here purely for price data.

Stream:
    wss://stream.bybit.com/v5/public/linear

Topics:
    publicTrade.BTCUSDT
    publicTrade.ETHUSDT
    publicTrade.SOLUSDT

Bybit V5 trade message format:
    {
        "topic": "publicTrade.BTCUSDT",
        "type": "snapshot",
        "ts": 1672304486868,
        "data": [
            {
                "T": 1672304486865,   # trade timestamp ms
                "s": "BTCUSDT",       # symbol
                "p": "16578.50",      # price
                "v": "0.001",         # volume/size
                "S": "Buy",           # side
                "i": "trade-id-xxx",  # trade ID
                "BT": false           # block trade flag
            },
            ...                       # one msg may contain multiple trades
        ]
    }
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

import aiohttp

log = logging.getLogger("bybit_client")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

BYBIT_WS_URLS: list[str] = [
    "wss://stream.bybit.com/v5/public/linear",
    "wss://stream.bybit.com/v5/public/linear",  # retry same; no official alt
]

BYBIT_TOPICS: list[str] = [
    "publicTrade.BTCUSDT",
    "publicTrade.ETHUSDT",
    "publicTrade.SOLUSDT",
]

_TOPIC_TO_ASSET: dict[str, str] = {
    "publicTrade.BTCUSDT": "BTC",
    "publicTrade.ETHUSDT": "ETH",
    "publicTrade.SOLUSDT": "SOL",
}

BYBIT_ASSET_TOPICS: dict[str, str] = {
    "BTC": "publicTrade.BTCUSDT",
    "ETH": "publicTrade.ETHUSDT",
    "SOL": "publicTrade.SOLUSDT",
}

PING_INTERVAL_S = 20.0   # Bybit requires client-side pings every <30s


# ─────────────────────────────────────────────────────────────────────────────
# Data structure
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PricePoint:
    ts: float    # Unix epoch seconds
    price: float


# ─────────────────────────────────────────────────────────────────────────────
# Pure functions (no I/O — fully unit-testable)
# ─────────────────────────────────────────────────────────────────────────────

def parse_bybit_trade(msg: dict) -> list[tuple[str, PricePoint]]:
    """
    Parse a Bybit V5 publicTrade WebSocket message.

    One message may contain multiple trade entries in the ``data`` list.
    Returns a list of ``(asset, PricePoint)`` for supported topics.
    Returns an empty list on invalid or unsupported messages.

    Expected Bybit V5 trade message::

        {
            "topic": "publicTrade.BTCUSDT",
            "type": "snapshot",
            "ts": 1672304486868,
            "data": [
                {"T": 1672304486865, "s": "BTCUSDT", "p": "16578.50", ...},
                ...
            ]
        }
    """
    topic = msg.get("topic", "")
    asset = _TOPIC_TO_ASSET.get(topic)
    if asset is None:
        return []

    data = msg.get("data")
    if not isinstance(data, list) or not data:
        return []

    results: list[tuple[str, PricePoint]] = []
    for trade in data:
        if not isinstance(trade, dict):
            continue
        price_str = trade.get("p")
        ts_ms = trade.get("T")
        if price_str is None or ts_ms is None:
            continue
        try:
            price = float(price_str)
            ts = int(ts_ms) / 1000.0
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        results.append((asset, PricePoint(ts=ts, price=price)))

    return results


def topic_for_asset(asset: str) -> Optional[str]:
    """Return the Bybit topic string for a canonical asset name."""
    return BYBIT_ASSET_TOPICS.get(asset)


# ─────────────────────────────────────────────────────────────────────────────
# SSL context
# ─────────────────────────────────────────────────────────────────────────────

def _make_ssl_ctx() -> ssl.SSLContext:
    import os
    if os.environ.get("DISABLE_SSL_VERIFY", "").strip() == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi  # type: ignore[import]
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL_CTX = _make_ssl_ctx()


# ─────────────────────────────────────────────────────────────────────────────
# Async WebSocket task
# ─────────────────────────────────────────────────────────────────────────────

async def bybit_ws_task(
    history: dict[str, deque],
    shutdown: asyncio.Event,
    topics: Optional[list[str]] = None,
    on_tick: Optional[Callable[[str, PricePoint], None]] = None,
    on_reconnect: Optional[Callable[[], None]] = None,
) -> None:
    """
    Stream Bybit V5 publicTrade events and write price points into *history*.

    Args:
        history:  dict mapping asset name → deque[PricePoint].
        shutdown: asyncio.Event; when set the task exits gracefully.
        topics:   list of Bybit topic strings (default: BTC/ETH/SOL).
        on_tick:  optional callback(asset, PricePoint) for each tick.

    Reconnects automatically with exponential back-off (max 30s).
    STATS_ONLY — no orders placed.
    """
    if topics is None:
        topics = BYBIT_TOPICS

    delay = 2.0
    url_idx = 0
    ticks = 0

    while not shutdown.is_set():
        url = BYBIT_WS_URLS[url_idx % len(BYBIT_WS_URLS)]
        try:
            connector = aiohttp.TCPConnector(ssl=_SSL_CTX)
            async with aiohttp.ClientSession(connector=connector) as session:
                timeout = aiohttp.ClientTimeout(connect=15)
                async with session.ws_connect(url, timeout=timeout) as ws:
                    # Subscribe
                    sub_msg = json.dumps({"op": "subscribe", "args": topics})
                    await ws.send_str(sub_msg)
                    log.info("[BYBIT] connected, subscribed: %s", topics)
                    delay = 2.0

                    # Ping task — Bybit requires pings every <30s
                    async def _ping() -> None:
                        while not ws.closed:
                            await asyncio.sleep(PING_INTERVAL_S)
                            if not ws.closed:
                                await ws.send_str(json.dumps({"op": "ping"}))

                    ping_task = asyncio.create_task(_ping())
                    try:
                        async for msg in ws:
                            if shutdown.is_set():
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(msg.data)
                                except json.JSONDecodeError:
                                    continue
                                # Ignore op responses (subscribe ack, ping/pong)
                                if "op" in data:
                                    continue
                                for asset, pt in parse_bybit_trade(data):
                                    history[asset].append(pt)
                                    ticks += 1
                                    if on_tick is not None:
                                        on_tick(asset, pt)
                            elif msg.type in (
                                aiohttp.WSMsgType.ERROR,
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSED,
                            ):
                                break
                    finally:
                        ping_task.cancel()

        except asyncio.CancelledError:
            log.info("[BYBIT] cancelled after %d ticks.", ticks)
            return
        except Exception as exc:
            url_idx += 1
            if on_reconnect is not None:
                on_reconnect()
            log.warning(
                "[BYBIT] disconnected after %d ticks: %s — retry in %.0fs",
                ticks, exc, delay,
            )
            try:
                await asyncio.wait_for(
                    asyncio.shield(shutdown.wait()), timeout=delay
                )
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 30.0)

    log.info("[BYBIT] shutdown. total ticks: %d", ticks)
