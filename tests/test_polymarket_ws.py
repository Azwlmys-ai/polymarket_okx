"""
tests/test_polymarket_ws.py — offline unit tests for Polymarket WS parser.

No network calls. Covers snapshot (book), best_bid_ask, last_trade_price,
price_change, malformed events, list envelopes, and edge cases.
"""

from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from src.polymarket_ws import (
    PolyWSEvent,
    _parse_levels,
    _safe_float,
    parse_best_bid_ask_event,
    parse_book_event,
    parse_last_trade_event,
    parse_price_change_event,
    parse_ws_message,
)

# ---------------------------------------------------------------------------
# Representative raw messages (captured from live WS probe)
# ---------------------------------------------------------------------------

TOKEN_YES = "106332386932214606086654990895646557174437993889157944446791384332713444688552"
TOKEN_NO  = "23951857091991851918788516639567315877867196671329359790975959300"
MARKET_HX = "0xce83a3662ac672cff31493d5ad0d1e0a03be95bda74138f67fc8f271f66e8325"

BOOK_EVT = {
    "event_type":       "book",
    "asset_id":         TOKEN_YES,
    "market":           MARKET_HX,
    "timestamp":        1779404910751,
    "bids":             [
        {"price": "0.97", "size": "975.53"},
        {"price": "0.96", "size": "6631.64"},
        {"price": "0.01", "size": "11201.14"},
    ],
    "asks":             [
        {"price": "0.99", "size": "4752.83"},
        {"price": "0.98", "size": "1255.36"},
    ],
    "last_trade_price": "0.020",
    "tick_size":        "0.01",
    "hash":             "abc123",
}

BBA_EVT = {
    "event_type": "best_bid_ask",
    "asset_id":   TOKEN_YES,
    "market":     MARKET_HX,
    "timestamp":  1779404910775,
    "best_bid":   "0.97",
    "best_ask":   "0.98",
    "spread":     "0.01",
}

TRADE_EVT = {
    "event_type":       "last_trade_price",
    "asset_id":         TOKEN_YES,
    "market":           MARKET_HX,
    "timestamp":        1779404910794,
    "price":            "0.98",
    "side":             "BUY",
    "size":             20,
    "fee_rate_bps":     0,
    "transaction_hash": "0xabc",
}

PRICE_CHANGE_EVT = {
    "event_type": "price_change",
    "market":     MARKET_HX,
    "timestamp":  1779404910773,
    "price_changes": [
        {"asset_id": TOKEN_YES, "price": "0.97", "best_bid": "0.96", "best_ask": "0.98"},
        {"asset_id": TOKEN_NO,  "price": "0.03", "best_bid": "0.02", "best_ask": "0.04"},
    ],
}

NEW_MARKET_EVT = {
    "event_type": "new_market",
    "market":     "0xdeadbeef",
    "timestamp":  1779404912301,
    "active":     False,
    "slug":       "eth-updown-15m-1779490800",
}


# ---------------------------------------------------------------------------
# _safe_float
# ---------------------------------------------------------------------------

class TestSafeFloat:
    def test_valid_string(self):
        assert _safe_float("0.97") == pytest.approx(0.97)

    def test_valid_float(self):
        assert _safe_float(0.5) == pytest.approx(0.5)

    def test_none(self):
        assert _safe_float(None) is None

    def test_out_of_range_above(self):
        assert _safe_float(1.1) is None

    def test_out_of_range_below(self):
        assert _safe_float(-0.1) is None

    def test_exact_boundaries(self):
        assert _safe_float(0.0) == pytest.approx(0.0)
        assert _safe_float(1.0) == pytest.approx(1.0)

    def test_malformed_string(self):
        assert _safe_float("not_a_number") is None

    def test_empty_string(self):
        assert _safe_float("") is None


# ---------------------------------------------------------------------------
# _parse_levels
# ---------------------------------------------------------------------------

class TestParseLevels:
    def test_valid_list(self):
        raw = [{"price": "0.97", "size": "100"}, {"price": "0.96", "size": "200"}]
        levels = _parse_levels(raw)
        assert len(levels) == 2
        assert levels[0] == (pytest.approx(0.97), pytest.approx(100.0))

    def test_empty(self):
        assert _parse_levels([]) == []

    def test_skips_invalid_price(self):
        raw = [{"price": "bad", "size": "100"}, {"price": "0.50", "size": "200"}]
        levels = _parse_levels(raw)
        assert len(levels) == 1

    def test_skips_missing_keys(self):
        raw = [{"price": "0.50"}, {"price": "0.60", "size": "100"}]
        levels = _parse_levels(raw)
        assert len(levels) == 1   # only the complete entry

    def test_skips_zero_price(self):
        raw = [{"price": "0.0", "size": "100"}]
        assert _parse_levels(raw) == []

    def test_skips_above_1(self):
        raw = [{"price": "1.5", "size": "100"}]
        assert _parse_levels(raw) == []


# ---------------------------------------------------------------------------
# parse_book_event
# ---------------------------------------------------------------------------

class TestParseBookEvent:
    def test_full_event(self):
        ev = parse_book_event(BOOK_EVT)
        assert ev is not None
        assert ev.source == "book"
        assert ev.token_id == TOKEN_YES
        assert ev.market_id == MARKET_HX
        assert ev.ts_ms == 1779404910751
        assert ev.best_bid == pytest.approx(0.97)   # highest bid
        assert ev.best_ask == pytest.approx(0.98)   # lowest ask
        assert ev.mid == pytest.approx((0.97 + 0.98) / 2)
        assert ev.last_trade == pytest.approx(0.020)
        assert ev.yes_price == ev.mid

    def test_empty_books(self):
        evt = {**BOOK_EVT, "bids": [], "asks": []}
        ev = parse_book_event(evt)
        assert ev is not None
        assert ev.best_bid is None
        assert ev.best_ask is None
        assert ev.mid is None
        assert ev.yes_price == ev.last_trade   # falls back to last_trade

    def test_missing_token_id(self):
        evt = {**BOOK_EVT, "asset_id": ""}
        assert parse_book_event(evt) is None

    def test_missing_market_id(self):
        evt = {**BOOK_EVT, "market": ""}
        assert parse_book_event(evt) is None

    def test_zero_timestamp(self):
        evt = {**BOOK_EVT, "timestamp": 0}
        assert parse_book_event(evt) is None

    def test_bids_sorted_descending(self):
        # Best bid should be the HIGHEST price
        evt = {**BOOK_EVT, "bids": [
            {"price": "0.50", "size": "100"},
            {"price": "0.97", "size": "200"},
            {"price": "0.30", "size": "50"},
        ]}
        ev = parse_book_event(evt)
        assert ev.best_bid == pytest.approx(0.97)

    def test_asks_sorted_ascending(self):
        # Best ask should be the LOWEST price
        evt = {**BOOK_EVT, "asks": [
            {"price": "0.99", "size": "100"},
            {"price": "0.98", "size": "200"},
        ]}
        ev = parse_book_event(evt)
        assert ev.best_ask == pytest.approx(0.98)

    def test_malformed_dict(self):
        assert parse_book_event({"event_type": "book"}) is None

    def test_none_input(self):
        assert parse_book_event(None) is None  # type: ignore


# ---------------------------------------------------------------------------
# parse_best_bid_ask_event
# ---------------------------------------------------------------------------

class TestParseBestBidAskEvent:
    def test_standard(self):
        ev = parse_best_bid_ask_event(BBA_EVT)
        assert ev is not None
        assert ev.source == "best_bid_ask"
        assert ev.best_bid == pytest.approx(0.97)
        assert ev.best_ask == pytest.approx(0.98)
        assert ev.mid == pytest.approx((0.97 + 0.98) / 2)
        assert ev.yes_price == ev.mid
        assert ev.last_trade is None

    def test_missing_fields(self):
        assert parse_best_bid_ask_event({"event_type": "best_bid_ask"}) is None

    def test_only_bid(self):
        evt = {**BBA_EVT, "best_ask": None}
        ev = parse_best_bid_ask_event(evt)
        assert ev is not None
        assert ev.best_ask is None
        assert ev.mid == pytest.approx(0.97)   # mid falls back to bid alone

    def test_only_ask(self):
        evt = {**BBA_EVT, "best_bid": None}
        ev = parse_best_bid_ask_event(evt)
        assert ev is not None
        assert ev.best_bid is None
        assert ev.mid == pytest.approx(0.98)


# ---------------------------------------------------------------------------
# parse_last_trade_event
# ---------------------------------------------------------------------------

class TestParseLastTradeEvent:
    def test_standard(self):
        ev = parse_last_trade_event(TRADE_EVT)
        assert ev is not None
        assert ev.source == "last_trade_price"
        assert ev.last_trade == pytest.approx(0.98)
        assert ev.yes_price == pytest.approx(0.98)
        assert ev.best_bid is None
        assert ev.best_ask is None

    def test_missing_price(self):
        evt = {**TRADE_EVT, "price": None}
        ev = parse_last_trade_event(evt)
        assert ev is not None
        assert ev.last_trade is None
        assert ev.yes_price is None

    def test_missing_ids(self):
        assert parse_last_trade_event({"event_type": "last_trade_price", "timestamp": 0}) is None


# ---------------------------------------------------------------------------
# parse_price_change_event
# ---------------------------------------------------------------------------

class TestParsePriceChangeEvent:
    def test_two_changes(self):
        evts = parse_price_change_event(PRICE_CHANGE_EVT)
        assert len(evts) == 2
        yes_evt = next(e for e in evts if e.token_id == TOKEN_YES)
        no_evt  = next(e for e in evts if e.token_id == TOKEN_NO)
        assert yes_evt.best_bid == pytest.approx(0.96)
        assert yes_evt.best_ask == pytest.approx(0.98)
        assert yes_evt.mid == pytest.approx((0.96 + 0.98) / 2)
        assert no_evt.best_bid == pytest.approx(0.02)

    def test_empty_changes(self):
        evt = {**PRICE_CHANGE_EVT, "price_changes": []}
        assert parse_price_change_event(evt) == []

    def test_missing_market(self):
        evt = {**PRICE_CHANGE_EVT, "market": ""}
        assert parse_price_change_event(evt) == []

    def test_malformed_change_skipped(self):
        evt = {
            **PRICE_CHANGE_EVT,
            "price_changes": [
                {"asset_id": TOKEN_YES, "price": "bad"},
                {"asset_id": TOKEN_NO,  "price": "0.03"},
            ],
        }
        evts = parse_price_change_event(evt)
        assert len(evts) == 1


# ---------------------------------------------------------------------------
# parse_ws_message  (top-level dispatcher)
# ---------------------------------------------------------------------------

class TestParseWsMessage:
    def test_single_book_dict(self):
        evts = parse_ws_message(BOOK_EVT)
        assert len(evts) == 1
        assert evts[0].source == "book"

    def test_list_envelope_mixed(self):
        raw = [BOOK_EVT, BBA_EVT, TRADE_EVT]
        evts = parse_ws_message(raw)
        assert len(evts) == 3
        sources = {e.source for e in evts}
        assert sources == {"book", "best_bid_ask", "last_trade_price"}

    def test_price_change_expands(self):
        evts = parse_ws_message(PRICE_CHANGE_EVT)
        assert len(evts) == 2   # one per price_change entry

    def test_new_market_ignored(self):
        assert parse_ws_message(NEW_MARKET_EVT) == []

    def test_unknown_type_ignored(self):
        assert parse_ws_message({"event_type": "some_future_type", "market": MARKET_HX}) == []

    def test_none_input(self):
        assert parse_ws_message(None) == []

    def test_empty_list(self):
        assert parse_ws_message([]) == []

    def test_malformed_item_in_list(self):
        raw = [BOOK_EVT, "not_a_dict", None, BBA_EVT]
        evts = parse_ws_message(raw)
        assert len(evts) == 2   # only the two valid dicts

    def test_all_event_types_in_one_batch(self):
        raw = [BOOK_EVT, BBA_EVT, TRADE_EVT, PRICE_CHANGE_EVT, NEW_MARKET_EVT]
        evts = parse_ws_message(raw)
        # book(1) + bba(1) + trade(1) + price_change(2) + new_market(ignored) = 5
        assert len(evts) == 5
