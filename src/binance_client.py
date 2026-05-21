"""
src/binance_client.py — Binance trade stream client (read-only, stats only).

STATS_ONLY. No orders. No API keys. No authentication required.
Public trade stream is unauthenticated and used here purely for price data.

Streams used:
    wss://stream.binance.com:9443/ws/btcusdt@trade/ethusdt@trade/solusdt@trade

Usage (standalone):
    import asyncio
    from collections import defaultdict, deque
    from src.binance_client import binance_ws_task, PricePoint

    history = defaultdict(lambda: deque(maxlen=3000))
    shutdown = asyncio.Event()
    asyncio.run(binance_ws_task(history, shutdown))
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

import aiohttp

log = logging.getLogger("bnb_client")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Canonical asset name → Binance stream symbol
BINANCE_ASSET_STREAMS: dict[str, str] = {
    "BTC": "btcusdt@trade",
    "ETH": "ethusdt@trade",
    "SOL": "solusdt@trade",
}

# Binance symbol (uppercase) → asset name
_SYMBOL_TO_ASSET: dict[str, str] = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
}

BINANCE_WS_URLS: list[str] = [
    "wss://stream.binance.com:9443/ws",
    "wss://stream.binance.com:443/ws",      # TLS fallback
]


# ─────────────────────────────────────────────────────────────────────────────
# Data structure (re-used from poly_lead_stats to avoid circular import)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PricePoint:
    ts: float    # Unix epoch seconds
    price: float


# ─────────────────────────────────────────────────────────────────────────────
# Pure functions (no I/O — fully unit-testable)
# ─────────────────────────────────────────────────────────────────────────────

def parse_binance_trade(msg: dict) -> Optional[tuple[str, PricePoint]]:
    """
    Parse a Binance ``trade`` WebSocket message.

    Returns ``(asset, PricePoint)`` for supported symbols, or ``None``.

    Expected Binance trade message format::

        {
            "e": "trade",        # event type
            "E": 1672515782136,  # event time (ms)
            "s": "BTCUSDT",      # symbol
            "p": "30000.00",     # price
            "T": 1672515782136,  # trade time (ms)
            ...
        }
    """
    if msg.get("e") != "trade":
        return None

    sym = (msg.get("s") or "").upper()
    asset = _SYMBOL_TO_ASSET.get(sym)
    if asset is None:
        return None

    price_str = msg.get("p")
    ts_ms = msg.get("T") or msg.get("E")  # prefer trade time over event time
    if not price_str or ts_ms is None:
        return None

    try:
        price = float(price_str)
        ts = int(ts_ms) / 1000.0
    except (TypeError, ValueError):
        return None

    if price <= 0:
        return None

    return asset, PricePoint(ts=ts, price=price)


def asset_to_stream(asset: str) -> Optional[str]:
    """Return the Binance stream name for a canonical asset (e.g. 'BTC' → 'btcusdt@trade')."""
    return BINANCE_ASSET_STREAMS.get(asset)


def stream_url(assets: list[str], base_url: str = BINANCE_WS_URLS[0]) -> str:
    """Build a combined-stream WebSocket URL for the given assets."""
    streams = "/".join(
        BINANCE_ASSET_STREAMS[a] for a in assets if a in BINANCE_ASSET_STREAMS
    )
    return f"{base_url}/{streams}"


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

async def binance_ws_task(
    history: dict[str, deque],
    shutdown: asyncio.Event,
    assets: Optional[list[str]] = None,
    on_tick: Optional[Callable[[str, PricePoint], None]] = None,
    on_reconnect: Optional[Callable[[], None]] = None,
) -> None:
    """
    Stream Binance trade events and write price points into *history*.

    Args:
        history:  dict mapping asset name → deque[PricePoint].
                  Caller owns this dict (e.g. ``state.binance_history``).
        shutdown: asyncio.Event; when set the task exits gracefully.
        assets:   list of asset names to subscribe (default: BTC/ETH/SOL).
        on_tick:  optional callback(asset, PricePoint) for each tick.

    Reconnects automatically with exponential back-off (max 30s).
    STATS_ONLY — no orders placed.
    """
    if assets is None:
        assets = list(BINANCE_ASSET_STREAMS.keys())

    delay = 2.0
    url_idx = 0
    ticks = 0

    while not shutdown.is_set():
        base = BINANCE_WS_URLS[url_idx % len(BINANCE_WS_URLS)]
        url = stream_url(assets, base)
        try:
            connector = aiohttp.TCPConnector(ssl=_SSL_CTX)
            async with aiohttp.ClientSession(connector=connector) as session:
                timeout = aiohttp.ClientTimeout(connect=15)
                async with session.ws_connect(url, timeout=timeout) as ws:
                    log.info("[BNB] connected: %s", url)
                    delay = 2.0  # reset on success

                    async for msg in ws:
                        if shutdown.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except json.JSONDecodeError:
                                continue
                            result = parse_binance_trade(data)
                            if result is not None:
                                asset, pt = result
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

        except asyncio.CancelledError:
            log.info("[BNB] cancelled after %d ticks.", ticks)
            return
        except Exception as exc:
            url_idx += 1
            if on_reconnect is not None:
                on_reconnect()
            log.warning(
                "[BNB] disconnected after %d ticks: %s — retry in %.0fs",
                ticks, exc, delay,
            )
            try:
                await asyncio.wait_for(
                    asyncio.shield(shutdown.wait()), timeout=delay
                )
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 30.0)

    log.info("[BNB] shutdown. total ticks: %d", ticks)
