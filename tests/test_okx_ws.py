"""
tests/test_okx_ws.py — unit tests for OKX WebSocket message parsing.

These tests are pure (no network, no DB) and cover parse_ticker_message.
"""
from __future__ import annotations

import pytest

from src.okx_ws import parse_ticker_message
from src.models import MarketSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ticker_push(inst_id: str = "BTC-USDT", **overrides) -> dict:
    """Return a minimal well-formed OKX ticker push message."""
    item = {
        "instType": "SPOT",
        "instId": inst_id,
        "last": "43000.5",
        "lastSz": "0.001",
        "askPx": "43001.0",
        "askSz": "0.5",
        "bidPx": "43000.0",
        "bidSz": "0.3",
        "open24h": "42000.0",
        "high24h": "44000.0",
        "low24h": "41500.0",
        "volCcy24h": "9999.0",
        "vol24h": "8888.0",
        "ts": "1710000000000",
        "sodUtc0": "42500.0",
        "sodUtc8": "42600.0",
    }
    item.update(overrides)
    return {
        "arg": {"channel": "tickers", "instId": inst_id},
        "data": [item],
    }


# ---------------------------------------------------------------------------
# parse_ticker_message — happy-path tests
# ---------------------------------------------------------------------------

class TestParseTickerMessage:
    def test_returns_market_snapshot_for_valid_push(self):
        msg = _ticker_push("BTC-USDT")
        snap = parse_ticker_message(msg)
        assert snap is not None

    def test_source_is_okx(self):
        snap = parse_ticker_message(_ticker_push("ETH-USDT"))
        assert snap.source == MarketSource.OKX

    def test_market_id_matches_inst_id(self):
        snap = parse_ticker_message(_ticker_push("SOL-USDT"))
        assert snap.market_id == "SOL-USDT"
        assert snap.symbol == "SOL-USDT"

    def test_numeric_fields_parsed_correctly(self):
        snap = parse_ticker_message(_ticker_push("BTC-USDT"))
        assert snap is not None
        assert snap.last == pytest.approx(43000.5)
        assert snap.bid == pytest.approx(43000.0)
        assert snap.ask == pytest.approx(43001.0)
        assert snap.volume_24h == pytest.approx(8888.0)

    def test_mid_computed_as_average_of_bid_ask(self):
        snap = parse_ticker_message(_ticker_push("BTC-USDT"))
        assert snap is not None
        expected_mid = (43000.0 + 43001.0) / 2
        assert snap.mid == pytest.approx(expected_mid)

    def test_ts_ms_taken_from_message_ts(self):
        snap = parse_ticker_message(_ticker_push("BTC-USDT", ts="1710000000000"))
        assert snap is not None
        assert snap.ts_ms == 1710000000000

    def test_ts_ms_falls_back_to_current_time_when_missing(self):
        import time
        before = int(time.time() * 1000)
        msg = _ticker_push("BTC-USDT")
        msg["data"][0].pop("ts", None)
        snap = parse_ticker_message(msg)
        after = int(time.time() * 1000)
        assert snap is not None
        assert before <= snap.ts_ms <= after + 10  # +10 ms tolerance

    def test_raw_payload_preserved(self):
        msg = _ticker_push("BTC-USDT")
        snap = parse_ticker_message(msg)
        assert snap is not None
        assert snap.raw.get("instId") == "BTC-USDT"

    def test_mid_is_none_when_bid_is_zero_or_missing(self):
        snap = parse_ticker_message(_ticker_push("BTC-USDT", bidPx="0", askPx="0"))
        assert snap is not None
        # Both zero values are treated as None → mid is None
        assert snap.mid is None

    def test_liquidity_is_bid_size(self):
        snap = parse_ticker_message(_ticker_push("BTC-USDT", bidSz="1.23"))
        assert snap is not None
        assert snap.liquidity == pytest.approx(1.23)


# ---------------------------------------------------------------------------
# parse_ticker_message — skip / None cases
# ---------------------------------------------------------------------------

class TestParseTickerMessageSkips:
    def test_returns_none_for_subscription_event(self):
        event_msg = {
            "event": "subscribe",
            "arg": {"channel": "tickers", "instId": "BTC-USDT"},
        }
        assert parse_ticker_message(event_msg) is None

    def test_returns_none_for_error_event(self):
        error_msg = {"event": "error", "code": "60018", "msg": "Invalid token"}
        assert parse_ticker_message(error_msg) is None

    def test_returns_none_for_non_ticker_channel(self):
        msg = {
            "arg": {"channel": "books", "instId": "BTC-USDT"},
            "data": [{"asks": [], "bids": [], "ts": "1710000000000"}],
        }
        assert parse_ticker_message(msg) is None

    def test_returns_none_for_empty_data_list(self):
        msg = {"arg": {"channel": "tickers", "instId": "BTC-USDT"}, "data": []}
        assert parse_ticker_message(msg) is None

    def test_returns_none_for_empty_dict(self):
        assert parse_ticker_message({}) is None
