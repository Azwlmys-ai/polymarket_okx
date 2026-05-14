"""
tests/test_polymarket_client.py — unit tests for Polymarket parsing logic.

All tests use representative fixture dicts; no network calls are made.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.polymarket_client import (
    _fetch_gamma_markets,
    _market_matches_keywords,
    _safe_float,
    _yes_token_id,
    apply_clob_book,
    parse_gamma_market,
)
from src.models import MarketSource


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

KEYWORDS = ["BTC", "ETH", "SOL", "bitcoin", "ethereum", "solana", "crypto"]


def _gamma_market(
    question: str = "Will Bitcoin exceed $100,000 by end of 2025?",
    market_id: str = "market-uuid-btc",
    active: bool = True,
    closed: bool = False,
    outcome_prices: list | None = None,
    tokens: list | None = None,
    liquidity: str = "50000.00",
    volume: str = "123456.78",
) -> dict:
    if outcome_prices is None:
        outcome_prices = ["0.65", "0.35"]
    if tokens is None:
        tokens = [
            {"token_id": "yes-token-001", "outcome": "Yes", "price": 0.65},
            {"token_id": "no-token-002", "outcome": "No", "price": 0.35},
        ]
    return {
        "id": market_id,
        "conditionId": f"0x{market_id}",
        "question": question,
        "active": active,
        "closed": closed,
        "outcomePrices": json.dumps(outcome_prices),
        "tokens": tokens,
        "liquidity": liquidity,
        "volume": volume,
    }


def _clob_book(
    bids: list | None = None,
    asks: list | None = None,
) -> dict:
    if bids is None:
        bids = [{"price": "0.64", "size": "500"}, {"price": "0.63", "size": "1000"}]
    if asks is None:
        asks = [{"price": "0.66", "size": "300"}, {"price": "0.67", "size": "800"}]
    return {
        "market": "market-uuid-btc",
        "asset_id": "yes-token-001",
        "bids": bids,
        "asks": asks,
        "hash": "abc123",
    }


# ---------------------------------------------------------------------------
# _safe_float
# ---------------------------------------------------------------------------

class TestSafeFloat:
    def test_converts_string_float(self):
        from src.polymarket_client import _safe_float
        assert _safe_float("0.65") == pytest.approx(0.65)

    def test_converts_int(self):
        from src.polymarket_client import _safe_float
        assert _safe_float(1) == pytest.approx(1.0)

    def test_returns_none_for_zero(self):
        from src.polymarket_client import _safe_float
        assert _safe_float("0") is None
        assert _safe_float(0) is None

    def test_returns_none_for_none(self):
        from src.polymarket_client import _safe_float
        assert _safe_float(None) is None

    def test_returns_none_for_empty_string(self):
        from src.polymarket_client import _safe_float
        assert _safe_float("") is None


# ---------------------------------------------------------------------------
# _market_matches_keywords
# ---------------------------------------------------------------------------

class TestMarketMatchesKeywords:
    def test_matches_btc_keyword(self):
        assert _market_matches_keywords("Will Bitcoin hit $100k?", KEYWORDS) is True

    def test_matches_eth_keyword(self):
        assert _market_matches_keywords("Will ETH reach $5000?", KEYWORDS) is True

    def test_case_insensitive_match(self):
        assert _market_matches_keywords("will BITCOIN rise?", KEYWORDS) is True

    def test_no_match_for_unrelated_market(self):
        assert _market_matches_keywords("Will the US win the World Cup?", KEYWORDS) is False

    def test_empty_question_returns_false(self):
        assert _market_matches_keywords("", KEYWORDS) is False

    def test_matches_partial_keyword(self):
        assert _market_matches_keywords("Ethereum 2.0 upgrade by June?", KEYWORDS) is True


# ---------------------------------------------------------------------------
# _yes_token_id
# ---------------------------------------------------------------------------

class TestYesTokenId:
    def test_extracts_yes_token_id(self):
        market = _gamma_market()
        assert _yes_token_id(market) == "yes-token-001"

    def test_returns_none_when_tokens_missing(self):
        market = _gamma_market(tokens=[])
        assert _yes_token_id(market) is None

    def test_case_insensitive_yes_match(self):
        market = _gamma_market(tokens=[{"token_id": "t1", "outcome": "YES"}])
        assert _yes_token_id(market) == "t1"

    def test_falls_back_to_first_token_when_no_yes_label(self):
        market = _gamma_market(tokens=[{"token_id": "t-first"}, {"token_id": "t-second"}])
        assert _yes_token_id(market) == "t-first"

    # ------------------------------------------------------------------
    # clobTokenIds support (Gamma v2 format)
    # ------------------------------------------------------------------

    def test_clob_token_ids_as_json_string(self):
        """clobTokenIds arrives as a JSON-encoded string; first entry is YES."""
        market = _gamma_market(tokens=[])
        market["clobTokenIds"] = json.dumps(["clob-yes-001", "clob-no-002"])
        assert _yes_token_id(market) == "clob-yes-001"

    def test_clob_token_ids_as_python_list(self):
        """clobTokenIds arrives already deserialized as a Python list."""
        market = _gamma_market(tokens=[])
        market["clobTokenIds"] = ["clob-yes-001", "clob-no-002"]
        assert _yes_token_id(market) == "clob-yes-001"

    def test_clob_token_ids_takes_priority_over_tokens(self):
        """clobTokenIds is checked before the tokens[] fallback."""
        market = _gamma_market()  # has tokens with yes-token-001
        market["clobTokenIds"] = ["clob-priority-id"]
        assert _yes_token_id(market) == "clob-priority-id"

    def test_clob_token_ids_single_element_json_string(self):
        """Single-element clobTokenIds JSON string is handled correctly."""
        market = _gamma_market(tokens=[])
        market["clobTokenIds"] = json.dumps(["only-token-id"])
        assert _yes_token_id(market) == "only-token-id"

    def test_clob_token_ids_empty_list_falls_through_to_tokens(self):
        """Empty clobTokenIds list falls through to tokens[] lookup."""
        market = _gamma_market()  # has tokens with yes-token-001
        market["clobTokenIds"] = []
        assert _yes_token_id(market) == "yes-token-001"

    def test_clob_token_ids_empty_json_string_falls_through_to_tokens(self):
        """clobTokenIds of '[]' (empty JSON list) falls through to tokens[]."""
        market = _gamma_market()  # has tokens with yes-token-001
        market["clobTokenIds"] = "[]"
        assert _yes_token_id(market) == "yes-token-001"

    def test_clob_token_ids_absent_tokens_absent_returns_none(self):
        """No clobTokenIds and no tokens[] → returns None."""
        market = _gamma_market(tokens=[])
        assert "clobTokenIds" not in market
        assert _yes_token_id(market) is None


# ---------------------------------------------------------------------------
# parse_gamma_market
# ---------------------------------------------------------------------------

class TestParseGammaMarket:
    def test_returns_snapshot_for_valid_active_market(self):
        snap = parse_gamma_market(_gamma_market())
        assert snap is not None

    def test_source_is_polymarket(self):
        snap = parse_gamma_market(_gamma_market())
        assert snap.source == MarketSource.POLYMARKET

    def test_market_id_taken_from_id_field(self):
        snap = parse_gamma_market(_gamma_market(market_id="my-market-123"))
        assert snap.market_id == "my-market-123"

    def test_symbol_is_truncated_question(self):
        long_q = "A" * 200
        snap = parse_gamma_market(_gamma_market(question=long_q))
        assert snap is not None
        assert len(snap.symbol) <= 120

    def test_last_price_parsed_from_outcome_prices(self):
        snap = parse_gamma_market(_gamma_market(outcome_prices=["0.72", "0.28"]))
        assert snap is not None
        assert snap.last == pytest.approx(0.72)

    def test_last_price_falls_back_to_token_price(self):
        market = _gamma_market(
            tokens=[{"token_id": "t1", "outcome": "Yes", "price": 0.55}]
        )
        # Remove outcomePrices to force fallback
        market.pop("outcomePrices", None)
        snap = parse_gamma_market(market)
        assert snap is not None
        assert snap.last == pytest.approx(0.55)

    def test_volume_and_liquidity_stored(self):
        snap = parse_gamma_market(_gamma_market(volume="9999.99", liquidity="12345.00"))
        assert snap is not None
        assert snap.volume_24h == pytest.approx(9999.99)
        assert snap.liquidity == pytest.approx(12345.0)

    def test_returns_none_for_closed_market(self):
        snap = parse_gamma_market(_gamma_market(closed=True))
        assert snap is None

    def test_returns_none_for_inactive_market(self):
        snap = parse_gamma_market(_gamma_market(active=False))
        assert snap is None

    def test_returns_none_for_missing_market_id(self):
        market = _gamma_market()
        market.pop("id")
        market.pop("conditionId")
        snap = parse_gamma_market(market)
        assert snap is None

    def test_bid_and_ask_are_none_without_clob(self):
        snap = parse_gamma_market(_gamma_market())
        assert snap is not None
        assert snap.bid is None
        assert snap.ask is None

    def test_raw_payload_preserved(self):
        snap = parse_gamma_market(_gamma_market(market_id="raw-check"))
        assert snap is not None
        assert snap.raw.get("id") == "raw-check"

    def test_list_outcome_prices_accepted(self):
        """outcomePrices may arrive as a Python list (not JSON string)."""
        market = _gamma_market()
        market["outcomePrices"] = [0.80, 0.20]
        snap = parse_gamma_market(market)
        assert snap is not None
        assert snap.last == pytest.approx(0.80)


# ---------------------------------------------------------------------------
# apply_clob_book
# ---------------------------------------------------------------------------

class TestApplyClobBook:
    def _base_snapshot(self):
        return parse_gamma_market(_gamma_market())

    def test_bid_and_ask_populated_from_book(self):
        snap = apply_clob_book(self._base_snapshot(), _clob_book())
        assert snap.bid == pytest.approx(0.64)
        assert snap.ask == pytest.approx(0.66)

    def test_mid_computed_as_average(self):
        snap = apply_clob_book(self._base_snapshot(), _clob_book())
        assert snap.mid == pytest.approx((0.64 + 0.66) / 2)

    def test_liquidity_updated_to_best_bid_size(self):
        snap = apply_clob_book(self._base_snapshot(), _clob_book())
        assert snap.liquidity == pytest.approx(500.0)

    def test_original_snapshot_not_mutated(self):
        base = self._base_snapshot()
        _ = apply_clob_book(base, _clob_book())
        assert base.bid is None  # original unchanged

    def test_mid_set_to_bid_when_asks_empty(self):
        book = _clob_book(asks=[])
        snap = apply_clob_book(self._base_snapshot(), book)
        assert snap.mid == pytest.approx(snap.bid)

    def test_mid_set_to_ask_when_bids_empty(self):
        book = _clob_book(bids=[])
        snap = apply_clob_book(self._base_snapshot(), book)
        assert snap.mid == pytest.approx(snap.ask)

    def test_empty_book_preserves_last_as_mid(self):
        book = _clob_book(bids=[], asks=[])
        base = self._base_snapshot()
        snap = apply_clob_book(base, book)
        # mid falls back to gamma last price when no CLOB data
        assert snap.mid == pytest.approx(base.last)


# ---------------------------------------------------------------------------
# _fetch_gamma_markets — error logging (F-3 fix)
# ---------------------------------------------------------------------------

class TestFetchGammaMarketsErrorLogging:
    """
    Verify that Gamma market fetch failures log the exception type name and
    repr() — not just str() — so silent exceptions (e.g. SSL errors whose
    str() is empty) are diagnosable from logs.

    These tests patch aiohttp at the session level; no network calls are made.
    """

    @pytest.mark.asyncio
    async def test_warning_includes_exception_type_name(self, caplog):
        """[ExcClassName] must appear in the warning message."""

        class _SilentError(Exception):
            """An exception whose str() is empty — mimics some aiohttp SSL errors."""
            def __str__(self) -> str:
                return ""

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=_SilentError("inner detail"))

        with caplog.at_level(logging.WARNING, logger="src.polymarket_client"):
            result = await _fetch_gamma_markets(mock_session, "https://gamma.test", ["BTC"])

        assert result == [], "should return empty list on fetch failure"
        combined = " ".join(caplog.messages)
        assert "_SilentError" in combined, (
            f"expected exception type name '_SilentError' in warning, got: {combined!r}"
        )

    @pytest.mark.asyncio
    async def test_warning_includes_repr_not_empty_str(self, caplog):
        """repr(exc) must appear so that exceptions with empty str() are still logged."""

        class _EmptyStrError(Exception):
            def __str__(self) -> str:
                return ""

        exc = _EmptyStrError("diagnosable-detail")
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=exc)

        with caplog.at_level(logging.WARNING, logger="src.polymarket_client"):
            await _fetch_gamma_markets(mock_session, "https://gamma.test", ["BTC"])

        combined = " ".join(caplog.messages)
        # repr() of the exception should contain "diagnosable-detail"
        assert "diagnosable-detail" in combined, (
            f"expected repr detail 'diagnosable-detail' in warning, got: {combined!r}"
        )

    @pytest.mark.asyncio
    async def test_warning_emitted_as_warning_level(self, caplog):
        """Fetch failure must be logged at WARNING level (not DEBUG or ERROR)."""
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=RuntimeError("boom"))

        with caplog.at_level(logging.DEBUG, logger="src.polymarket_client"):
            await _fetch_gamma_markets(mock_session, "https://gamma.test", ["BTC"])

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records, "expected at least one WARNING log record on fetch failure"
        assert any("Gamma market fetch failed" in r.message for r in warning_records)
