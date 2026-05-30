"""
tests/test_poly_microstructure_collector.py — offline unit tests.

Covers pure functions only: no network, no WebSocket, no file I/O.
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from src.polymarket_ws import PolyWSEvent
from research.poly_microstructure_collector import (
    BookState,
    MarketMeta,
    MicrostructureSample,
    _extract_tokens,
    _parse_end_ts,
    event_to_sample,
    seconds_to_expiry,
    update_book,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _event(
    ts_ms: int = 1_000_000,
    market_id: str = "0xtest",
    token_id: str = "tok_yes",
    best_bid: float | None = 0.48,
    best_ask: float | None = 0.52,
    source: str = "best_bid_ask",
) -> PolyWSEvent:
    mid = (best_bid + best_ask) / 2 if best_bid and best_ask else None
    return PolyWSEvent(
        ts_ms=ts_ms,
        market_id=market_id,
        token_id=token_id,
        yes_price=mid,
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        last_trade=None,
        source=source,
    )


def _book(
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
    token_id: str = "tok_yes",
) -> BookState:
    b = BookState(token_id=token_id)
    b.bids = bids or []
    b.asks = asks or []
    return b


def _end_ts_ms(seconds_ahead: float = 600.0, base_ts_ms: int = 1_000_000) -> int:
    return base_ts_ms + int(seconds_ahead * 1000)


# ---------------------------------------------------------------------------
# seconds_to_expiry
# ---------------------------------------------------------------------------

class TestSecondsToExpiry:
    def test_basic(self):
        result = seconds_to_expiry(ts_ms=1_000_000, end_ts_ms=1_600_000)
        assert result == pytest.approx(600.0)

    def test_expired(self):
        result = seconds_to_expiry(ts_ms=2_000_000, end_ts_ms=1_000_000)
        assert result == pytest.approx(-1000.0)

    def test_zero_end_ts(self):
        assert seconds_to_expiry(ts_ms=1_000_000, end_ts_ms=0) is None

    def test_exact_expiry(self):
        assert seconds_to_expiry(ts_ms=1_000_000, end_ts_ms=1_000_000) == pytest.approx(0.0)

    def test_fractional_seconds(self):
        result = seconds_to_expiry(ts_ms=1_000_000, end_ts_ms=1_001_500)
        assert result == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# event_to_sample — basic conversion
# ---------------------------------------------------------------------------

class TestEventToSample:
    def test_returns_sample(self):
        ev   = _event()
        book = _book()
        s    = event_to_sample(ev, book, end_ts_ms=0)
        assert isinstance(s, MicrostructureSample)

    def test_ts_ms_preserved(self):
        ev = _event(ts_ms=999_000)
        s  = event_to_sample(ev, _book(), end_ts_ms=0)
        assert s.ts_ms == 999_000

    def test_event_type_preserved(self):
        ev = _event(source="best_bid_ask")
        s  = event_to_sample(ev, _book(), end_ts_ms=0)
        assert s.event_type == "best_bid_ask"

    def test_bid_ask_preserved(self):
        ev = _event(best_bid=0.48, best_ask=0.52)
        s  = event_to_sample(ev, _book(), end_ts_ms=0)
        assert s.best_bid == pytest.approx(0.48)
        assert s.best_ask == pytest.approx(0.52)

    def test_mid_computed(self):
        ev = _event(best_bid=0.48, best_ask=0.52)
        s  = event_to_sample(ev, _book(), end_ts_ms=0)
        assert s.mid == pytest.approx(0.50)

    def test_spread_bps_computed(self):
        # spread=0.04, mid=0.50 → 800 bps
        ev = _event(best_bid=0.48, best_ask=0.52)
        s  = event_to_sample(ev, _book(), end_ts_ms=0)
        assert s.spread_bps == pytest.approx(800.0, rel=1e-3)

    def test_seconds_to_expiry_set(self):
        ev  = _event(ts_ms=1_000_000)
        s   = event_to_sample(ev, _book(), end_ts_ms=1_300_000)
        assert s.seconds_to_expiry == pytest.approx(300.0)

    def test_seconds_to_expiry_none_when_end_zero(self):
        ev = _event(ts_ms=1_000_000)
        s  = event_to_sample(ev, _book(), end_ts_ms=0)
        assert s.seconds_to_expiry is None

    def test_serialisable(self):
        import json
        ev = _event()
        s  = event_to_sample(ev, _book(), end_ts_ms=0)
        json.dumps(s.to_dict())   # must not raise


# ---------------------------------------------------------------------------
# event_to_sample — depth and imbalance
# ---------------------------------------------------------------------------

class TestEventToSampleDepth:
    def test_empty_book_zero_depth(self):
        ev = _event()
        s  = event_to_sample(ev, _book(), end_ts_ms=0)
        assert s.depth_near_mid == pytest.approx(0.0)

    def test_depth_from_book_state(self):
        ev = _event(best_bid=0.49, best_ask=0.51)
        book = _book(
            bids=[(0.49, 200)],
            asks=[(0.51, 300)],
        )
        s = event_to_sample(ev, book, end_ts_ms=0)
        # within ±5% of mid=0.50: both levels qualify
        assert s.depth_near_mid > 0

    def test_bid_ask_depth_split(self):
        ev = _event(best_bid=0.49, best_ask=0.51)
        book = _book(
            bids=[(0.49, 100)],
            asks=[(0.51, 200)],
        )
        s = event_to_sample(ev, book, end_ts_ms=0)
        assert s.bid_depth_near_mid > 0
        assert s.ask_depth_near_mid > 0
        assert s.depth_near_mid == pytest.approx(s.bid_depth_near_mid + s.ask_depth_near_mid)

    def test_imbalance_none_when_empty_book(self):
        ev = _event()
        s  = event_to_sample(ev, _book(), end_ts_ms=0)
        assert s.imbalance is None

    def test_imbalance_bid_heavy(self):
        ev = _event(best_bid=0.49, best_ask=0.51)
        book = _book(bids=[(0.49, 1000)], asks=[(0.51, 10)])
        s = event_to_sample(ev, book, end_ts_ms=0)
        assert s.imbalance is not None
        assert s.imbalance > 0   # bid dominated

    def test_imbalance_ask_heavy(self):
        ev = _event(best_bid=0.49, best_ask=0.51)
        book = _book(bids=[(0.49, 10)], asks=[(0.51, 1000)])
        s = event_to_sample(ev, book, end_ts_ms=0)
        assert s.imbalance is not None
        assert s.imbalance < 0   # ask dominated


# ---------------------------------------------------------------------------
# event_to_sample — missing / None fields
# ---------------------------------------------------------------------------

class TestEventToSampleMissing:
    def test_none_bid(self):
        ev = _event(best_bid=None, best_ask=0.52)
        s  = event_to_sample(ev, _book(), end_ts_ms=0)
        assert s.best_bid is None
        assert s.spread_bps is None   # can't compute without both sides

    def test_none_ask(self):
        ev = _event(best_bid=0.48, best_ask=None)
        s  = event_to_sample(ev, _book(), end_ts_ms=0)
        assert s.best_ask is None

    def test_none_bid_and_ask(self):
        ev = _event(best_bid=None, best_ask=None)
        s  = event_to_sample(ev, _book(), end_ts_ms=0)
        assert s.mid is None
        assert s.spread_bps is None
        assert s.depth_near_mid == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# update_book
# ---------------------------------------------------------------------------

class TestUpdateBook:
    def _book_event_raw(
        self,
        bids: list[dict] | None = None,
        asks: list[dict] | None = None,
    ) -> dict:
        return {
            "bids": bids or [{"price": "0.48", "size": "100"}],
            "asks": asks or [{"price": "0.52", "size": "200"}],
        }

    def test_book_event_updates_bids_and_asks(self):
        book = _book()
        ev   = _event(source="book")
        raw  = self._book_event_raw()
        update_book(book, ev, raw)
        assert len(book.bids) == 1
        assert len(book.asks) == 1
        assert book.bids[0][0] == pytest.approx(0.48)
        assert book.asks[0][0] == pytest.approx(0.52)

    def test_non_book_event_does_not_update(self):
        book = _book(bids=[(0.48, 100)], asks=[(0.52, 200)])
        ev   = _event(source="best_bid_ask")
        update_book(book, ev, {"bids": [], "asks": []})
        # levels unchanged
        assert len(book.bids) == 1

    def test_book_event_sorts_bids_descending(self):
        book = _book()
        ev   = _event(source="book")
        raw  = {"bids": [{"price": "0.45", "size": "50"}, {"price": "0.48", "size": "100"}], "asks": []}
        update_book(book, ev, raw)
        prices = [b[0] for b in book.bids]
        assert prices == sorted(prices, reverse=True)

    def test_book_event_sorts_asks_ascending(self):
        book = _book()
        ev   = _event(source="book")
        raw  = {"bids": [], "asks": [{"price": "0.55", "size": "50"}, {"price": "0.52", "size": "100"}]}
        update_book(book, ev, raw)
        prices = [a[0] for a in book.asks]
        assert prices == sorted(prices)

    def test_malformed_levels_skipped(self):
        book = _book()
        ev   = _event(source="book")
        raw  = {"bids": [{"price": "bad", "size": "100"}, {"price": "0.48", "size": "100"}], "asks": []}
        update_book(book, ev, raw)
        assert len(book.bids) == 1   # only valid entry kept


# ---------------------------------------------------------------------------
# _extract_tokens / _parse_end_ts (discovery helpers)
# ---------------------------------------------------------------------------

class TestDiscoveryHelpers:
    def test_extract_tokens_json_string(self):
        import json
        m = {"clobTokenIds": json.dumps(["YES_TOK", "NO_TOK"])}
        yes, no = _extract_tokens(m)
        assert yes == "YES_TOK"
        assert no  == "NO_TOK"

    def test_extract_tokens_list(self):
        m = {"clobTokenIds": ["YES_TOK", "NO_TOK"]}
        yes, no = _extract_tokens(m)
        assert yes == "YES_TOK"
        assert no  == "NO_TOK"

    def test_extract_tokens_missing(self):
        yes, no = _extract_tokens({})
        assert yes == "" and no == ""

    def test_extract_tokens_single_entry(self):
        import json
        m = {"clobTokenIds": json.dumps(["ONLY_YES"])}
        yes, no = _extract_tokens(m)
        assert yes == "ONLY_YES"
        assert no  == ""

    def test_parse_end_ts_iso(self):
        ts = _parse_end_ts("2026-06-01T00:00:00Z")
        assert ts > 0

    def test_parse_end_ts_none(self):
        assert _parse_end_ts(None) == 0

    def test_parse_end_ts_empty(self):
        assert _parse_end_ts("") == 0

    def test_parse_end_ts_bad_format(self):
        assert _parse_end_ts("not-a-date") == 0
