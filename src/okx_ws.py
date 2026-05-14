"""
okx_ws.py — OKX public WebSocket client (read-only, no API keys).

Subscribes to the OKX spot tickers channel for configured symbols and
yields parsed MarketSnapshot objects.  The client never touches private
endpoints, never sends orders, and never handles credentials.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
from typing import AsyncIterator

import aiohttp
import certifi

from src.models import MarketSnapshot, MarketSource


def _make_ssl_ctx() -> ssl.SSLContext:
    """Build an SSL context using certifi's CA bundle.

    If DISABLE_SSL_VERIFY=1 is set, verification is disabled entirely
    (useful behind corporate MITM proxies where certifi won't help).
    """
    if os.environ.get("DISABLE_SSL_VERIFY", "").strip() == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context(cafile=certifi.where())


_SSL_CTX: ssl.SSLContext = _make_ssl_ctx()

logger = logging.getLogger(__name__)

# OKX keeps connections alive with application-level ping/pong.
_PING_INTERVAL_S: float = 20.0
_CONNECT_TIMEOUT_S: float = 15.0
_MAX_RECONNECT_DELAY_S: float = 30.0


def parse_ticker_message(raw: dict) -> MarketSnapshot | None:
    """
    Parse one OKX WS push message into a MarketSnapshot.

    Returns None when the message is not a ticker data push
    (e.g. subscription confirmations, pong frames, error notices).

    Pure function — no I/O, easy to unit-test.
    """
    if raw.get("event") is not None:
        # subscription / error confirmation frames — skip
        return None

    arg = raw.get("arg", {})
    if arg.get("channel") != "tickers":
        return None

    data_list = raw.get("data")
    if not data_list:
        return None

    item = data_list[0]

    def _float(key: str) -> float | None:
        val = item.get(key)
        try:
            return float(val) if val not in (None, "", "0") else None
        except (TypeError, ValueError):
            return None

    inst_id: str = item.get("instId", arg.get("instId", "UNKNOWN"))
    ts_str = item.get("ts")
    ts_ms = int(ts_str) if ts_str else int(time.time() * 1000)

    last = _float("last")
    bid = _float("bidPx")
    ask = _float("askPx")
    mid: float | None = None
    if bid is not None and ask is not None:
        mid = round((bid + ask) / 2, 8)

    return MarketSnapshot(
        ts_ms=ts_ms,
        source=MarketSource.OKX,
        market_id=inst_id,
        symbol=inst_id,
        bid=bid,
        ask=ask,
        mid=mid,
        last=last,
        liquidity=_float("bidSz"),
        volume_24h=_float("vol24h"),
        raw=item,
    )


class OkxWsCollector:
    """
    Connects to the OKX public WebSocket and streams MarketSnapshot rows.

    Usage::

        async with OkxWsCollector(ws_url, symbols) as collector:
            async for snapshot in collector.stream(duration_s=30):
                print(snapshot)

    The collector handles:
    - JSON subscribe/unsubscribe lifecycle
    - application-level ping (plain "ping" text frame every 20 s)
    - basic reconnect with exponential back-off
    """

    def __init__(self, ws_url: str, symbols: list[str]) -> None:
        self._ws_url = ws_url
        self._symbols = symbols
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "OkxWsCollector":
        connector = aiohttp.TCPConnector(ssl=_SSL_CTX)
        self._session = aiohttp.ClientSession(connector=connector)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def stream(
        self,
        duration_s: float | None = None,
        max_count: int | None = None,
    ) -> AsyncIterator[MarketSnapshot]:
        """
        Yield MarketSnapshot objects from OKX tickers.

        Stop when *duration_s* seconds have elapsed or *max_count*
        snapshots have been yielded — whichever comes first.
        If both are None the stream runs until cancelled.
        """
        deadline = time.monotonic() + duration_s if duration_s else None
        yielded = 0
        delay = 2.0

        while True:
            if deadline and time.monotonic() >= deadline:
                return
            if max_count is not None and yielded >= max_count:
                return

            try:
                async for snapshot in self._run_connection(deadline, max_count, yielded):
                    yield snapshot
                    yielded += 1
                    if max_count is not None and yielded >= max_count:
                        return
                # clean exit — done
                return
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError) as exc:
                logger.warning("OKX WS connection lost (%s); reconnecting in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _MAX_RECONNECT_DELAY_S)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_connection(
        self,
        deadline: float | None,
        max_count: int | None,
        already_yielded: int,
    ) -> AsyncIterator[MarketSnapshot]:
        assert self._session is not None, "Use as async context manager"

        timeout = aiohttp.ClientTimeout(connect=_CONNECT_TIMEOUT_S)
        logger.info("Connecting to OKX WS: %s", self._ws_url)

        async with self._session.ws_connect(
            self._ws_url,
            timeout=timeout,
            heartbeat=None,  # we manage ping manually
        ) as ws:
            await self._subscribe(ws)
            logger.info("Subscribed to tickers: %s", self._symbols)

            ping_task = asyncio.create_task(self._ping_loop(ws))
            yielded = already_yielded

            try:
                async for msg in ws:
                    # --- deadline / count check -----------------------
                    if deadline and time.monotonic() >= deadline:
                        return
                    if max_count is not None and yielded >= max_count:
                        return

                    if msg.type == aiohttp.WSMsgType.TEXT:
                        if msg.data == "pong":
                            continue
                        try:
                            payload = json.loads(msg.data)
                        except json.JSONDecodeError:
                            logger.debug("Unparseable WS message: %.120s", msg.data)
                            continue

                        snapshot = parse_ticker_message(payload)
                        if snapshot is not None:
                            logger.debug(
                                "Snapshot %s last=%s bid=%s ask=%s",
                                snapshot.symbol,
                                snapshot.last,
                                snapshot.bid,
                                snapshot.ask,
                            )
                            yield snapshot
                            yielded += 1

                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.warning("WS error frame received; closing connection")
                        return

                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                    ):
                        logger.info("WS connection closed by server")
                        return
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass

    async def _subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        args = [{"channel": "tickers", "instId": sym} for sym in self._symbols]
        payload = json.dumps({"op": "subscribe", "args": args})
        await ws.send_str(payload)

    @staticmethod
    async def _ping_loop(ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            while True:
                await asyncio.sleep(_PING_INTERVAL_S)
                if ws.closed:
                    return
                await ws.send_str("ping")
                logger.debug("ping sent")
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("ping loop ended: %s", exc)
