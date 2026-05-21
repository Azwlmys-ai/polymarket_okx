"""
tests/test_bybit_client.py

Unit tests for src/bybit_client.py pure functions.
No network, no asyncio.
"""
from __future__ import annotations

import pytest

from src.bybit_client import (
    BYBIT_ASSET_TOPICS,
    PricePoint,
    parse_bybit_trade,
    topic_for_asset,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _trade_msg(
    topic: str = "publicTrade.BTCUSDT",
    trades: list[dict] | None = None,
    ts: int = 1_700_000_000_000,
) -> dict:
    if trades is None:
        trades = [{"T": ts, "s": "BTCUSDT", "p": "30000.00", "v": "0.001", "S": "Buy", "BT": False}]
    return {"topic": topic, "type": "snapshot", "ts": ts, "data": trades}


def _single_trade(price: str, symbol: str = "BTCUSDT", ts_ms: int = 1_700_000_000_000) -> dict:
    return {"T": ts_ms, "s": symbol, "p": price, "v": "0.001", "S": "Buy", "BT": False}


# ─────────────────────────────────────────────────────────────────────────────
# parse_bybit_trade — single trade
# ─────────────────────────────────────────────────────────────────────────────

class TestParseBybittradeSingle:
    def test_btcusdt_parsed(self):
        msg = _trade_msg("publicTrade.BTCUSDT", [_single_trade("30000.00")])
        results = parse_bybit_trade(msg)
        assert len(results) == 1
        asset, pt = results[0]
        assert asset == "BTC"
        assert pt.price == pytest.approx(30000.0)

    def test_ethusdt_parsed(self):
        msg = _trade_msg("publicTrade.ETHUSDT", [_single_trade("2000.50", "ETHUSDT")])
        results = parse_bybit_trade(msg)
        assert len(results) == 1
        asset, pt = results[0]
        assert asset == "ETH"
        assert pt.price == pytest.approx(2000.50)

    def test_solusdt_parsed(self):
        msg = _trade_msg("publicTrade.SOLUSDT", [_single_trade("85.00", "SOLUSDT")])
        results = parse_bybit_trade(msg)
        assert len(results) == 1
        asset, pt = results[0]
        assert asset == "SOL"

    def test_timestamp_converted_to_seconds(self):
        ts_ms = 1_700_000_100_000
        msg = _trade_msg(trades=[_single_trade("30000", ts_ms=ts_ms)])
        results = parse_bybit_trade(msg)
        assert len(results) == 1
        _, pt = results[0]
        assert pt.ts == pytest.approx(ts_ms / 1000.0)

    def test_zero_price_excluded(self):
        msg = _trade_msg(trades=[_single_trade("0.0")])
        assert parse_bybit_trade(msg) == []

    def test_negative_price_excluded(self):
        msg = _trade_msg(trades=[_single_trade("-1.0")])
        assert parse_bybit_trade(msg) == []

    def test_invalid_price_excluded(self):
        msg = _trade_msg(trades=[_single_trade("not_a_number")])
        assert parse_bybit_trade(msg) == []


# ─────────────────────────────────────────────────────────────────────────────
# parse_bybit_trade — multiple trades in one message
# ─────────────────────────────────────────────────────────────────────────────

class TestParseBybittradeMultiple:
    def test_multiple_trades_all_returned(self):
        trades = [
            _single_trade("30000.00", ts_ms=1_000_000),
            _single_trade("30001.00", ts_ms=1_000_001),
            _single_trade("29999.50", ts_ms=1_000_002),
        ]
        msg = _trade_msg(trades=trades)
        results = parse_bybit_trade(msg)
        assert len(results) == 3
        prices = [pt.price for _, pt in results]
        assert prices == pytest.approx([30000.0, 30001.0, 29999.50])

    def test_mixed_valid_invalid_trades(self):
        trades = [
            _single_trade("30000.00"),
            {"T": 1_000_000, "s": "BTCUSDT", "p": "bad", "v": "0.1"},   # bad price
            _single_trade("30001.00"),
        ]
        msg = _trade_msg(trades=trades)
        results = parse_bybit_trade(msg)
        assert len(results) == 2   # bad trade excluded

    def test_empty_data_list_returns_empty(self):
        msg = {"topic": "publicTrade.BTCUSDT", "type": "snapshot", "ts": 1000, "data": []}
        assert parse_bybit_trade(msg) == []

    def test_ten_trades_all_processed(self):
        trades = [_single_trade(str(30000 + i)) for i in range(10)]
        msg = _trade_msg(trades=trades)
        results = parse_bybit_trade(msg)
        assert len(results) == 10


# ─────────────────────────────────────────────────────────────────────────────
# parse_bybit_trade — malformed messages (must not crash)
# ─────────────────────────────────────────────────────────────────────────────

class TestParseBybittrademalformed:
    def test_empty_dict(self):
        assert parse_bybit_trade({}) == []

    def test_no_topic(self):
        msg = {"type": "snapshot", "ts": 1000, "data": [_single_trade("30000")]}
        assert parse_bybit_trade(msg) == []

    def test_unknown_topic(self):
        msg = _trade_msg("publicTrade.BNBUSDT")
        assert parse_bybit_trade(msg) == []

    def test_data_not_list(self):
        msg = {"topic": "publicTrade.BTCUSDT", "data": "not_a_list"}
        assert parse_bybit_trade(msg) == []

    def test_data_none(self):
        msg = {"topic": "publicTrade.BTCUSDT", "data": None}
        assert parse_bybit_trade(msg) == []

    def test_trade_missing_price_field(self):
        trade = {"T": 1_000_000, "s": "BTCUSDT", "v": "0.001"}  # no "p"
        msg = _trade_msg(trades=[trade])
        assert parse_bybit_trade(msg) == []

    def test_trade_missing_timestamp(self):
        trade = {"s": "BTCUSDT", "p": "30000.00"}  # no "T"
        msg = _trade_msg(trades=[trade])
        assert parse_bybit_trade(msg) == []

    def test_op_response_ignored(self):
        # Bybit sends op responses (subscribe ack, pong) — should return empty
        msg = {"op": "subscribe", "success": True, "ret_msg": "subscribe"}
        assert parse_bybit_trade(msg) == []

    def test_trade_item_not_dict(self):
        msg = {"topic": "publicTrade.BTCUSDT", "data": ["not_a_dict", 42]}
        assert parse_bybit_trade(msg) == []


# ─────────────────────────────────────────────────────────────────────────────
# topic_for_asset helper
# ─────────────────────────────────────────────────────────────────────────────

class TestTopicForAsset:
    def test_btc(self):
        assert topic_for_asset("BTC") == "publicTrade.BTCUSDT"

    def test_eth(self):
        assert topic_for_asset("ETH") == "publicTrade.ETHUSDT"

    def test_sol(self):
        assert topic_for_asset("SOL") == "publicTrade.SOLUSDT"

    def test_unknown_returns_none(self):
        assert topic_for_asset("XRP") is None
        assert topic_for_asset("DOGE") is None
