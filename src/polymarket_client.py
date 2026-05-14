"""
polymarket_client.py — Polymarket read-only public data client.

Uses only public, unauthenticated Polymarket endpoints:
  - Gamma API  (market discovery, prices, volume)
  - CLOB API   (best bid/ask order book per YES token)

No API keys, no wallet signing, no order placement.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator

import aiohttp

from src.models import MarketSnapshot, MarketSource

logger = logging.getLogger(__name__)

# Gamma API query params
_GAMMA_MARKETS_PATH = "/markets"
_GAMMA_PAGE_LIMIT = 100          # max markets fetched per discovery sweep
_CLOB_BOOK_PATH = "/book"        # ?token_id=<id>

# Polling cadence for the bounded collector
_POLL_INTERVAL_S: float = 5.0
_HTTP_TIMEOUT_S: float = 15.0


# ---------------------------------------------------------------------------
# Pure parsing helpers (no I/O — unit-testable)
# ---------------------------------------------------------------------------

def _safe_float(value: object) -> float | None:
    """Convert str / int / float to float, return None on failure or zero."""
    try:
        f = float(value)  # type: ignore[arg-type]
        return f if f != 0.0 else None
    except (TypeError, ValueError):
        return None


def _market_matches_keywords(question: str, keywords: list[str]) -> bool:
    """Return True if *question* contains at least one keyword (case-insensitive)."""
    q_lower = question.lower()
    return any(kw.lower() in q_lower for kw in keywords)


def _yes_token_id(market: dict) -> str | None:
    """Extract the token_id for the YES outcome from a Gamma market object.

    Resolution order:
    1. ``clobTokenIds`` (Gamma v2 format) — may be a JSON-encoded string or a
       Python list.  The first entry is treated as the YES token.
    2. ``tokens[].token_id`` where ``outcome == "YES"`` (case-insensitive).
    3. First token in ``tokens[]`` as a last resort.

    Returns None if no usable token ID can be found.
    """
    # --- clobTokenIds (Gamma v2): JSON string or Python list ---
    clob_ids_raw = market.get("clobTokenIds")
    if clob_ids_raw is not None:
        try:
            clob_ids = (
                clob_ids_raw
                if isinstance(clob_ids_raw, list)
                else __import__("json").loads(clob_ids_raw)
            )
            if clob_ids:
                return str(clob_ids[0])
        except Exception:  # noqa: BLE001
            pass

    # --- tokens[] fallback (Gamma v1) ---
    tokens = market.get("tokens") or []
    for token in tokens:
        if str(token.get("outcome", "")).upper() == "YES":
            return str(token["token_id"])
    # Fallback: first token if outcomes not labelled
    if tokens:
        return str(tokens[0].get("token_id", ""))
    return None


def parse_gamma_market(market: dict) -> MarketSnapshot | None:
    """
    Parse a single Gamma API market object into a MarketSnapshot.

    Uses `outcomePrices[0]` (YES price) as `last`.
    Bid/ask are None here — they are filled in by `apply_clob_book` if available.

    Returns None for closed markets or markets with no usable price data.
    """
    if market.get("closed") or not market.get("active"):
        return None

    market_id: str = market.get("id") or market.get("conditionId") or ""
    if not market_id:
        return None

    question: str = market.get("question") or market.get("title") or market_id

    # YES price from outcomePrices list (index 0)
    outcome_prices_raw = market.get("outcomePrices")
    last: float | None = None
    if outcome_prices_raw:
        try:
            prices = (
                outcome_prices_raw
                if isinstance(outcome_prices_raw, list)
                else __import__("json").loads(outcome_prices_raw)
            )
            last = _safe_float(prices[0]) if prices else None
        except Exception:  # noqa: BLE001
            last = None

    # Prefer YES token price field if outcomePrices unavailable
    if last is None:
        yes_token = next(
            (t for t in (market.get("tokens") or []) if str(t.get("outcome", "")).upper() == "YES"),
            None,
        )
        if yes_token:
            last = _safe_float(yes_token.get("price"))

    ts_ms = int(time.time() * 1000)

    return MarketSnapshot(
        ts_ms=ts_ms,
        source=MarketSource.POLYMARKET,
        market_id=market_id,
        symbol=question[:120],  # truncate very long questions
        bid=None,
        ask=None,
        mid=last,   # best we have without CLOB; overwritten by apply_clob_book
        last=last,
        liquidity=_safe_float(market.get("liquidity")),
        volume_24h=_safe_float(market.get("volume")),
        raw=market,
    )


def apply_clob_book(snapshot: MarketSnapshot, book: dict) -> MarketSnapshot:
    """
    Return a new MarketSnapshot enriched with CLOB best bid/ask data.

    `book` must be the response dict from GET /book?token_id=<yes_token_id>.
    The original snapshot is not mutated; a new instance is returned via
    ``model_copy()``.  The Pydantic model is not configured frozen.
    """
    bids: list[dict] = book.get("bids") or []
    asks: list[dict] = book.get("asks") or []

    best_bid = _safe_float(bids[0]["price"]) if bids else None
    best_ask = _safe_float(asks[0]["price"]) if asks else None

    mid: float | None = snapshot.mid
    if best_bid is not None and best_ask is not None:
        mid = round((best_bid + best_ask) / 2, 8)
    elif best_bid is not None:
        mid = best_bid
    elif best_ask is not None:
        mid = best_ask

    liquidity: float | None = snapshot.liquidity
    if bids:
        liquidity = _safe_float(bids[0].get("size")) or liquidity

    return snapshot.model_copy(
        update={
            "bid": best_bid,
            "ask": best_ask,
            "mid": mid,
            "liquidity": liquidity,
        }
    )


# ---------------------------------------------------------------------------
# Async HTTP helpers
# ---------------------------------------------------------------------------

async def _fetch_gamma_markets(
    session: aiohttp.ClientSession,
    gamma_url: str,
    keywords: list[str],
    limit: int = _GAMMA_PAGE_LIMIT,
) -> list[dict]:
    """
    Fetch active Gamma markets and return only those matching *keywords*.

    Network errors are logged and an empty list is returned so the caller
    can continue polling.
    """
    url = f"{gamma_url.rstrip('/')}{_GAMMA_MARKETS_PATH}"
    params = {"limit": limit, "active": "true", "closed": "false"}
    try:
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
        async with session.get(url, params=params, timeout=timeout) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Gamma market fetch failed: [%s] %s",
            type(exc).__name__,
            repr(exc),
        )
        return []

    if not isinstance(data, list):
        # Some API versions wrap in {"data": [...]}
        data = data.get("data") or data.get("markets") or []

    matched = [m for m in data if _market_matches_keywords(m.get("question") or "", keywords)]
    logger.debug("Gamma: %d markets fetched, %d match keywords", len(data), len(matched))
    return matched


async def _fetch_clob_book(
    session: aiohttp.ClientSession,
    clob_url: str,
    token_id: str,
) -> dict | None:
    """
    Fetch CLOB order book for *token_id*.  Returns None on any error.
    """
    url = f"{clob_url.rstrip('/')}{_CLOB_BOOK_PATH}"
    params = {"token_id": token_id}
    try:
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
        async with session.get(url, params=params, timeout=timeout) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("CLOB book fetch failed for token %s: %s", token_id, exc)
        return None


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class PolymarketCollector:
    """
    Polls Polymarket public APIs for crypto-related market snapshots.

    Discovery: Gamma API `/markets` filtered by keyword list.
    Pricing:   CLOB API `/book` for the YES token of each matched market.

    Usage::

        async with PolymarketCollector(gamma_url, clob_url, keywords) as c:
            async for snapshot in c.stream(duration_s=60, max_count=20):
                print(snapshot)
    """

    def __init__(
        self,
        gamma_url: str,
        clob_url: str,
        keywords: list[str],
    ) -> None:
        self._gamma_url = gamma_url
        self._clob_url = clob_url
        self._keywords = keywords
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "PolymarketCollector":
        self._session = aiohttp.ClientSession(
            headers={"User-Agent": "polymarket-okx-research/1.0 (phase1-data-collection)"}
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def stream(
        self,
        duration_s: float | None = None,
        max_count: int | None = None,
    ) -> AsyncIterator[MarketSnapshot]:
        """
        Yield MarketSnapshot objects from Polymarket public endpoints.

        Polls in sweeps: each sweep fetches all matched markets, then
        enriches with CLOB book data.  Sleeps _POLL_INTERVAL_S between sweeps.

        Stops when duration_s elapses or max_count snapshots are yielded.
        """
        assert self._session is not None, "Use PolymarketCollector as async context manager"

        deadline = time.monotonic() + duration_s if duration_s else None
        yielded = 0

        while True:
            if deadline and time.monotonic() >= deadline:
                return
            if max_count is not None and yielded >= max_count:
                return

            sweep_start = time.monotonic()
            markets = await _fetch_gamma_markets(
                self._session, self._gamma_url, self._keywords
            )
            logger.info("Polymarket sweep: %d crypto markets found", len(markets))

            for market in markets:
                if deadline and time.monotonic() >= deadline:
                    return
                if max_count is not None and yielded >= max_count:
                    return

                snapshot = parse_gamma_market(market)
                if snapshot is None:
                    continue

                # Enrich with CLOB book if YES token is available
                token_id = _yes_token_id(market)
                if token_id:
                    book = await _fetch_clob_book(self._session, self._clob_url, token_id)
                    if book:
                        snapshot = apply_clob_book(snapshot, book)

                logger.debug(
                    "Polymarket snapshot %s  last=%.4f  bid=%s  ask=%s",
                    snapshot.symbol[:60],
                    snapshot.last or 0,
                    snapshot.bid,
                    snapshot.ask,
                )
                yield snapshot
                yielded += 1

            # Wait before next sweep (respect deadline)
            elapsed = time.monotonic() - sweep_start
            sleep_s = max(0.0, _POLL_INTERVAL_S - elapsed)
            if deadline:
                remaining = deadline - time.monotonic()
                sleep_s = min(sleep_s, remaining)
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)
