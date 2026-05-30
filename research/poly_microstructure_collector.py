"""
poly_microstructure_collector.py — read-only orderbook sampler.

Discovers active short-cycle Polymarket Up/Down markets, subscribes to their
YES/NO CLOB WebSocket feeds, and emits microstructure metrics to a JSONL file.

NO WALLET. NO ORDERS. NO AUTHENTICATION. READ-ONLY.

Usage:
    python3 research/poly_microstructure_collector.py --duration 300
    python3 research/poly_microstructure_collector.py --duration 3600 \\
        --out research/poly_microstructure_samples_1h.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import ssl
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

# ---------------------------------------------------------------------------
# Project root on path (for running as `python3 research/...`)
# ---------------------------------------------------------------------------
import pathlib as _pathlib
sys.path.insert(0, str(_pathlib.Path(__file__).parent.parent))

from src.polymarket_ws import (
    PolyWSEvent,
    _parse_levels,
    parse_ws_message,
    polymarket_ws_task,
)
from research.poly_microstructure_metrics import (
    depth_near_mid,
    depth_usd,
    order_imbalance,
    spread_bps as _spread_bps,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GAMMA_BASE   = "https://gamma-api.polymarket.com"
HEARTBEAT_S  = 60
KEYWORDS     = ["up or down", "up-or-down", "updown"]
DEFAULT_OUT  = Path("research/poly_microstructure_samples.jsonl")
DEPTH_PCT    = 0.05

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode    = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MarketMeta:
    """Static metadata fetched once during discovery."""
    market_id:    str     # condition_id hex
    slug:         str
    yes_token_id: str
    no_token_id:  str
    end_ts_ms:    int     # market expiry epoch-ms (0 if unknown)
    question:     str


@dataclass
class BookState:
    """Per-token mutable orderbook state, updated on 'book' events."""
    token_id: str
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class MicrostructureSample:
    """One JSONL output record."""
    ts_ms:              int
    market_id:          str
    token_id:           str
    event_type:         str
    best_bid:           float | None
    best_ask:           float | None
    mid:                float | None
    spread_bps:         float | None
    bid_depth_near_mid: float
    ask_depth_near_mid: float
    imbalance:          float | None
    depth_near_mid:     float
    seconds_to_expiry:  float | None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Pure transformation functions (testable offline)
# ---------------------------------------------------------------------------

def seconds_to_expiry(ts_ms: int, end_ts_ms: int) -> float | None:
    """Seconds remaining until market expiry. None if end_ts_ms is zero."""
    if end_ts_ms <= 0:
        return None
    return round((end_ts_ms - ts_ms) / 1000.0, 3)


def event_to_sample(
    ev: PolyWSEvent,
    book: BookState,
    end_ts_ms: int,
) -> MicrostructureSample:
    """
    Convert a PolyWSEvent + current BookState → MicrostructureSample.

    Uses stored book levels for depth/imbalance when available.
    Falls back gracefully when book is empty or prices are missing.
    """
    mid = ev.mid
    sp  = _spread_bps(ev.best_bid, ev.best_ask)

    # Use stored book for depth/imbalance; only meaningful after a 'book' event.
    bids = book.bids
    asks = book.asks

    bid_d = depth_near_mid(bids, [], mid, DEPTH_PCT)
    ask_d = depth_near_mid([], asks, mid, DEPTH_PCT)
    total_d = round(bid_d + ask_d, 6)

    imb = order_imbalance(bids, asks) if bids or asks else None
    sto = seconds_to_expiry(ev.ts_ms, end_ts_ms)

    return MicrostructureSample(
        ts_ms              = ev.ts_ms,
        market_id          = ev.market_id,
        token_id           = ev.token_id,
        event_type         = ev.source,
        best_bid           = ev.best_bid,
        best_ask           = ev.best_ask,
        mid                = mid,
        spread_bps         = sp,
        bid_depth_near_mid = bid_d,
        ask_depth_near_mid = ask_d,
        imbalance          = imb,
        depth_near_mid     = total_d,
        seconds_to_expiry  = sto,
    )


def update_book(book: BookState, ev: PolyWSEvent, raw_event: dict) -> None:
    """
    Update BookState in-place from a 'book' WS event.

    Only 'book' events carry full level data; other types are ignored.
    """
    if ev.source != "book":
        return
    raw_bids = raw_event.get("bids") or []
    raw_asks = raw_event.get("asks") or []
    book.bids = sorted(_parse_levels(raw_bids), key=lambda x: x[0], reverse=True)
    book.asks = sorted(_parse_levels(raw_asks), key=lambda x: x[0])


# ---------------------------------------------------------------------------
# Market discovery (HTTP)
# ---------------------------------------------------------------------------

async def _fetch_json(session: aiohttp.ClientSession, url: str, params: dict | None = None) -> Any:
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
            r.raise_for_status()
            return await r.json(content_type=None)
    except Exception as exc:
        log.warning("[discovery] HTTP error %s: %s", url, exc)
        return None


def _parse_end_ts(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        s = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _extract_tokens(market: dict) -> tuple[str, str]:
    raw = market.get("clobTokenIds") or "[]"
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        yes = str(ids[0]) if len(ids) > 0 else ""
        no  = str(ids[1]) if len(ids) > 1 else ""
        return yes, no
    except Exception:
        return "", ""


def _5m_slugs(n_ahead: int = 3, n_back: int = 1) -> list[str]:
    """
    Return deterministic BTC 5m slugs for the current and nearby windows.

    Slug format: btc-updown-5m-{window_start_unix}
    The /markets endpoint doesn't return restricted markets, so we build
    slugs from current UTC time and fetch them via /events/slug/ directly.
    """
    now = int(time.time())
    boundary = (now // 300) * 300
    offsets = list(range(-n_back, n_ahead + 1))
    return [f"btc-updown-5m-{boundary + i * 300}" for i in offsets]


def _market_meta_from_event(event_data: dict) -> MarketMeta | None:
    """Build MarketMeta from a Gamma /events/slug/{slug} response."""
    if not isinstance(event_data, dict):
        return None
    if event_data.get("type") == "not found error":
        return None
    markets = event_data.get("markets") or []
    if not markets:
        return None
    m = markets[0]
    if m.get("closed") or not m.get("acceptingOrders", True):
        return None   # skip already-closed or not-yet-open markets
    yes_tok, no_tok = _extract_tokens(m)
    if not yes_tok:
        return None
    return MarketMeta(
        market_id    = m.get("conditionId") or "",
        slug         = m.get("slug") or event_data.get("slug") or "",
        yes_token_id = yes_tok,
        no_token_id  = no_tok,
        end_ts_ms    = _parse_end_ts(m.get("endDate")),
        question     = (m.get("question") or event_data.get("title") or "")[:80],
    )


async def discover_markets(limit: int = 100) -> list[MarketMeta]:
    """
    Discover active Up/Down markets.

    Strategy 1 (primary): deterministic BTC 5m slug construction.
      The /markets endpoint omits restricted markets; the /events/slug/ endpoint
      returns them directly.

    Strategy 2 (fallback): keyword search via /markets endpoint.
      Catches non-BTC Up/Down markets if any are found.
    """
    connector = aiohttp.TCPConnector(ssl=_SSL_CTX)
    headers   = {"User-Agent": "polymarket-okx-research/2.0 (microstructure-collector)"}
    found: dict[str, MarketMeta] = {}   # slug → MarketMeta (dedup)

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        # --- Strategy 1: deterministic BTC 5m slugs ---
        slugs = _5m_slugs(n_ahead=3, n_back=1)
        log.debug("[discovery] trying slugs: %s", slugs)
        for slug in slugs:
            data = await _fetch_json(session, f"{GAMMA_BASE}/events/slug/{slug}")
            meta = _market_meta_from_event(data or {})
            if meta and meta.slug not in found:
                found[meta.slug] = meta
                log.debug("[discovery] found via slug: %s", meta.question)

        # --- Strategy 2: keyword search (fallback / extra markets) ---
        data = await _fetch_json(
            session, f"{GAMMA_BASE}/markets",
            {"limit": limit, "active": "true", "closed": "false"},
        )
        if isinstance(data, dict):
            data = data.get("data") or data.get("markets") or []
        for m in (data or []):
            q = (m.get("question") or "").lower()
            if not any(kw in q for kw in KEYWORDS):
                continue
            yes_tok, no_tok = _extract_tokens(m)
            if not yes_tok:
                continue
            slug = m.get("slug") or ""
            if slug in found:
                continue
            found[slug] = MarketMeta(
                market_id    = m.get("conditionId") or m.get("id") or "",
                slug         = slug,
                yes_token_id = yes_tok,
                no_token_id  = no_tok,
                end_ts_ms    = _parse_end_ts(m.get("endDate")),
                question     = (m.get("question") or "")[:80],
            )

    return list(found.values())


# ---------------------------------------------------------------------------
# Collector runtime
# ---------------------------------------------------------------------------

class Collector:
    def __init__(self, out_path: Path, duration_s: int):
        self._out_path   = out_path
        self._duration_s = duration_s
        self._shutdown   = asyncio.Event()
        self._books:    dict[str, BookState]   = {}   # token_id → BookState
        self._meta_by_token: dict[str, MarketMeta] = {}  # token_id → MarketMeta
        self._raw_by_token: dict[str, dict]    = {}   # token_id → latest raw dict (for update_book)
        self._n_samples  = 0
        self._n_events   = 0
        self._last_hb_s  = time.monotonic()
        self._fh         = None

    def _open(self) -> None:
        self._out_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._out_path, "a", encoding="utf-8")

    def _close(self) -> None:
        if self._fh:
            self._fh.flush()
            self._fh.close()
            self._fh = None

    def _write(self, sample: MicrostructureSample) -> None:
        if self._fh:
            self._fh.write(json.dumps(sample.to_dict()) + "\n")
            self._fh.flush()

    async def _handle_raw(self, raw_evt: dict, ev: PolyWSEvent) -> None:
        """
        Process one parsed PolyWSEvent together with its original raw dict.

        Having access to the raw dict lets us update the book's bid/ask level
        lists from 'book' events (the PolyWSEvent scalar fields don't carry them).
        """
        self._n_events += 1
        meta = self._meta_by_token.get(ev.token_id)
        if meta is None:
            return

        book = self._books.setdefault(ev.token_id, BookState(token_id=ev.token_id))
        update_book(book, ev, raw_evt)   # populates bids/asks on 'book' events

        sample = event_to_sample(ev, book, meta.end_ts_ms)
        self._write(sample)
        self._n_samples += 1

        now = time.monotonic()
        if now - self._last_hb_s >= HEARTBEAT_S:
            self._last_hb_s = now
            print(
                f"[{_utc()}] heartbeat — events={self._n_events}"
                f" samples={self._n_samples}",
                flush=True,
            )

    async def _ws_loop(self, token_ids: list[str]) -> None:
        """
        Own WS loop that processes raw messages, giving access to level data.

        Mirrors polymarket_ws_task logic but keeps the raw event dict so that
        book events can populate bid/ask level arrays for depth/imbalance.
        Reconnects on errors until self._shutdown is set.
        """
        from src.polymarket_ws import POLY_MARKET_WS_URL

        sub = json.dumps({
            "assets_ids": token_ids,
            "type": "market",
            "custom_feature_enabled": True,
        })

        connector = aiohttp.TCPConnector(ssl=_SSL_CTX)
        reconnects = 0

        async with aiohttp.ClientSession(connector=connector) as session:
            while not self._shutdown.is_set():
                try:
                    async with session.ws_connect(
                        POLY_MARKET_WS_URL,
                        heartbeat=25,
                        timeout=aiohttp.ClientTimeout(connect=15, total=None),
                    ) as ws:
                        await ws.send_str(sub)
                        log.info("[collector-ws] connected (#%d), %d tokens",
                                 reconnects, len(token_ids))

                        async for msg in ws:
                            if self._shutdown.is_set():
                                return
                            if msg.type not in (
                                aiohttp.WSMsgType.TEXT,
                                aiohttp.WSMsgType.BINARY,
                            ):
                                if msg.type in (
                                    aiohttp.WSMsgType.CLOSE,
                                    aiohttp.WSMsgType.ERROR,
                                ):
                                    break
                                continue
                            try:
                                payload = json.loads(
                                    msg.data if msg.type == aiohttp.WSMsgType.TEXT
                                    else msg.data.decode()
                                )
                            except Exception:
                                continue

                            # payload may be a list or a single dict
                            raw_evts = payload if isinstance(payload, list) else [payload]
                            for raw_evt in raw_evts:
                                if not isinstance(raw_evt, dict):
                                    continue
                                for ev in parse_ws_message(raw_evt):
                                    try:
                                        await self._handle_raw(raw_evt, ev)
                                    except Exception as exc:
                                        log.debug("[collector-ws] handle error: %s", exc)

                except Exception as exc:
                    if not self._shutdown.is_set():
                        reconnects += 1
                        log.warning("[collector-ws] error (%s) — retry #%d in 3s",
                                    exc, reconnects)
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(self._shutdown.wait()), timeout=3.0
                            )
                        except asyncio.TimeoutError:
                            pass

    async def run(self) -> None:
        print(f"[{_utc()}] Discovering markets…", flush=True)
        markets = await discover_markets()

        if not markets:
            print(
                "[warn] No active Up/Down markets found. "
                "Check network or try again later.",
                flush=True,
            )
            return

        token_ids: list[str] = []
        for m in markets:
            for tid in (m.yes_token_id, m.no_token_id):
                if tid:
                    self._meta_by_token[tid] = m
                    self._books[tid]         = BookState(token_id=tid)
                    token_ids.append(tid)

        print(
            f"[{_utc()}] Subscribed to {len(markets)} markets "
            f"({len(token_ids)} tokens). "
            f"Duration={self._duration_s}s. "
            f"Out={self._out_path}",
            flush=True,
        )

        self._open()
        try:
            async with asyncio.timeout(self._duration_s):
                await self._ws_loop(token_ids)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        finally:
            self._shutdown.set()
            self._close()

        print(
            f"[{_utc()}] Done. events={self._n_events} "
            f"samples={self._n_samples} → {self._out_path}",
            flush=True,
        )


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    _setup_logging()

    parser = argparse.ArgumentParser(
        description="Polymarket microstructure JSONL collector (read-only)"
    )
    parser.add_argument("--duration", type=int, default=300,
                        help="Collection duration in seconds (default 300)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="Output JSONL file")
    args = parser.parse_args()

    collector = Collector(out_path=args.out, duration_s=args.duration)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _handle_signal(sig: int, _frame: Any) -> None:
        print(f"\n[{_utc()}] Shutting down (signal {sig})…", flush=True)
        collector._shutdown.set()

    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        loop.run_until_complete(collector.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
