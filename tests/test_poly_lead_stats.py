"""
tests/test_poly_lead_stats.py

Unit tests for src/poly_lead_stats.py and src/binance_client.py pure functions.
All tests are deterministic, use no network, no asyncio.
"""
from __future__ import annotations

import pytest

from src.binance_client import (
    BINANCE_ASSET_STREAMS,
    PricePoint as BnbPricePoint,
    asset_to_stream,
    parse_binance_trade,
    stream_url,
)
from src.poly_lead_stats import (
    EXCHANGES,
    FORWARD_HORIZONS,
    STATS_ONLY,
    REAL_ORDER,
    LeadSignal,
    PricePoint,
    _direction_stats,
    _exchange_horizon_stats,
    _percentile,
    _rank_venues,
    build_stats_report,
    compute_forward_return,
    detect_price_event,
    format_markdown_report,
)


# ─────────────────────────────────────────────────────────────────────────────
# Safety constants
# ─────────────────────────────────────────────────────────────────────────────

class TestSafetyConstants:
    def test_stats_only_is_true(self):
        assert STATS_ONLY is True

    def test_real_order_is_false(self):
        assert REAL_ORDER is False


# ─────────────────────────────────────────────────────────────────────────────
# detect_price_event
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectPriceEvent:
    def _series(self, prices_with_offsets: list[tuple[float, float]], base_ts: float = 1000.0):
        """Build PricePoint list from (time_offset, price) pairs."""
        return [PricePoint(ts=base_ts + dt, price=p) for dt, p in prices_with_offsets]

    def test_empty_series_returns_none(self):
        assert detect_price_event([], 10.0, 0.03, 1010.0) is None

    def test_single_point_returns_none(self):
        series = [PricePoint(ts=1000.0, price=0.50)]
        assert detect_price_event(series, 10.0, 0.03, 1010.0) is None

    def test_jump_detected_above_threshold(self):
        series = self._series([(0, 0.50), (5, 0.54)])  # +0.04 in 5s
        event = detect_price_event(series, 10.0, 0.03, 1010.0)
        assert event is not None
        assert event["direction"] == "jump"
        assert event["magnitude"] == pytest.approx(0.04)
        assert event["yes_before"] == pytest.approx(0.50)
        assert event["yes_after"] == pytest.approx(0.54)

    def test_drop_detected_above_threshold(self):
        series = self._series([(0, 0.60), (8, 0.54)])  # -0.06 in 8s
        event = detect_price_event(series, 10.0, 0.03, 1010.0)
        assert event is not None
        assert event["direction"] == "drop"
        assert event["magnitude"] == pytest.approx(0.06)

    def test_below_threshold_returns_none(self):
        series = self._series([(0, 0.50), (5, 0.52)])  # +0.02 < 0.03
        assert detect_price_event(series, 10.0, 0.03, 1010.0) is None

    def test_exactly_at_threshold_triggers(self):
        series = self._series([(0, 0.50), (5, 0.53)])  # exactly 0.03
        event = detect_price_event(series, 10.0, 0.03, 1010.0)
        assert event is not None
        assert event["direction"] == "jump"

    def test_point_outside_window_ignored(self):
        # Price moved +0.05 but older point is outside the 10s window
        series = self._series([(0, 0.45), (15, 0.50)])  # offset 0 → outside window
        # now_ts = 1025, window = 10s → cutoff = 1015 → pt at 1000 is excluded
        event = detect_price_event(series, 10.0, 0.03, 1025.0)
        # Only 1 point in window → None
        assert event is None

    def test_multiple_points_uses_oldest_in_window(self):
        # now_ts=1020, window=10s → cutoff=1010
        # Oldest in window: ts=1012 (price=0.48), latest: ts=1020 (price=0.52)
        series = self._series([
            (0,  0.45),   # ts=1000, outside window (1000 < 1010)
            (12, 0.48),   # ts=1012, oldest inside window
            (16, 0.50),   # ts=1016
            (20, 0.52),   # ts=1020, latest
        ])
        event = detect_price_event(series, 10.0, 0.03, 1020.0)
        assert event is not None
        assert event["yes_before"] == pytest.approx(0.48)
        assert event["yes_after"] == pytest.approx(0.52)
        assert event["magnitude"] == pytest.approx(0.04)

    def test_30s_window_larger_threshold(self):
        series = self._series([(0, 0.50), (25, 0.56)])  # +0.06 in 25s
        event = detect_price_event(series, 30.0, 0.05, 1030.0)
        assert event is not None
        assert event["direction"] == "jump"

    def test_30s_window_below_threshold(self):
        series = self._series([(0, 0.50), (25, 0.54)])  # +0.04 < 0.05
        assert detect_price_event(series, 30.0, 0.05, 1030.0) is None

    def test_60s_window(self):
        series = self._series([(0, 0.50), (55, 0.59)])  # +0.09 in 55s
        event = detect_price_event(series, 60.0, 0.08, 1060.0)
        assert event is not None
        assert event["direction"] == "jump"

    def test_no_data_in_window_returns_none(self):
        # Series exists but all points are before the window
        series = self._series([(0, 0.50), (1, 0.55)])
        # now_ts = 2000, window = 10 → cutoff = 1990, all points at 1000/1001
        assert detect_price_event(series, 10.0, 0.03, 2000.0) is None


# ─────────────────────────────────────────────────────────────────────────────
# compute_forward_return
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeForwardReturn:
    def _series(self, pairs: list[tuple[float, float]]) -> list[PricePoint]:
        return [PricePoint(ts=ts, price=p) for ts, p in pairs]

    def test_positive_return(self):
        series = self._series([(1000, 100.0), (1070, 102.0)])
        ret = compute_forward_return(series, trigger_ts=1000.0, horizon_s=60.0)
        assert ret == pytest.approx(0.02)

    def test_negative_return(self):
        series = self._series([(1000, 100.0), (1070, 99.0)])
        ret = compute_forward_return(series, trigger_ts=1000.0, horizon_s=60.0)
        assert ret == pytest.approx(-0.01)

    def test_no_data_after_trigger_returns_none(self):
        series = self._series([(900, 100.0), (950, 101.0)])
        # trigger_ts = 1000, no data ≥ 1000
        ret = compute_forward_return(series, trigger_ts=1000.0, horizon_s=60.0)
        assert ret is None

    def test_horizon_not_yet_elapsed_returns_none(self):
        series = self._series([(1000, 100.0), (1050, 101.0)])
        # trigger=1000, horizon=60 → need data at ≥1060; only 1050 available
        ret = compute_forward_return(series, trigger_ts=1000.0, horizon_s=60.0)
        assert ret is None

    def test_empty_series_returns_none(self):
        assert compute_forward_return([], trigger_ts=1000.0, horizon_s=60.0) is None

    def test_zero_trigger_price_returns_none(self):
        series = self._series([(1000, 0.0), (1070, 100.0)])
        ret = compute_forward_return(series, trigger_ts=1000.0, horizon_s=60.0)
        assert ret is None

    def test_180s_horizon(self):
        series = self._series([(1000, 50.0), (1200, 52.0)])
        ret = compute_forward_return(series, trigger_ts=1000.0, horizon_s=180.0)
        assert ret == pytest.approx(0.04)

    def test_300s_horizon(self):
        series = self._series([(1000, 200.0), (1310, 210.0)])
        ret = compute_forward_return(series, trigger_ts=1000.0, horizon_s=300.0)
        assert ret == pytest.approx(0.05)


# ─────────────────────────────────────────────────────────────────────────────
# build_stats_report
# ─────────────────────────────────────────────────────────────────────────────

def _make_signal(
    asset: str = "BTC",
    direction: str = "jump",
    returns: dict | None = None,
) -> LeadSignal:
    s = LeadSignal(
        ts=1000.0, asset=asset, market_id="m1",
        market_title="Bitcoin Up or Down", direction=direction,
        window_s=10.0, threshold=0.03, magnitude=0.04,
        yes_before=0.50, yes_after=0.54,
        okx_symbol="BTC-USDT", okx_price_at_trigger=50000.0,
    )
    if returns:
        s.forward_returns = returns
    return s


class TestBuildStatsReport:
    def test_empty_signals_no_crash(self):
        report = build_stats_report([], elapsed_s=100.0)
        assert report["total_signals"] == 0
        assert report["has_positive_expectation"] is False
        assert report["by_asset"] == {}

    def test_single_jump_signal(self):
        s = _make_signal("ETH", "jump", {60: 0.005, 180: 0.008, 300: 0.01})
        report = build_stats_report([s], elapsed_s=500.0)
        assert report["total_signals"] == 1
        assert report["by_asset"]["ETH"] == 1
        assert report["by_direction"]["jump"] == 1

    def test_win_rate_all_positive(self):
        signals = [
            _make_signal("BTC", "jump", {60: 0.01, 180: 0.02, 300: 0.03}),
            _make_signal("BTC", "jump", {60: 0.005, 180: 0.01, 300: 0.015}),
        ]
        report = build_stats_report(signals, elapsed_s=1000.0)
        assert report["by_horizon"][60]["jump"]["win_rate"] == pytest.approx(1.0)
        assert report["by_horizon"][60]["jump"]["mean"] == pytest.approx(0.0075)

    def test_win_rate_mixed(self):
        signals = [
            _make_signal("SOL", "jump", {60: 0.01}),
            _make_signal("SOL", "jump", {60: -0.005}),
        ]
        report = build_stats_report(signals, elapsed_s=1000.0)
        assert report["by_horizon"][60]["jump"]["win_rate"] == pytest.approx(0.5)

    def test_drop_direction_counted(self):
        signals = [
            _make_signal("BTC", "drop", {60: -0.005, 180: -0.01, 300: -0.02}),
        ]
        report = build_stats_report(signals, elapsed_s=1000.0)
        assert report["by_direction"]["drop"] == 1

    def test_positive_expectation_detected(self):
        # Aligned win_rate > 0.5 and mean > 0 for at least one horizon
        signals = [
            _make_signal("BTC", "jump", {60: 0.01, 180: 0.02, 300: 0.03}),
            _make_signal("BTC", "jump", {60: 0.005, 180: 0.01, 300: 0.015}),
            _make_signal("BTC", "jump", {60: 0.008, 180: 0.012, 300: 0.018}),
        ]
        report = build_stats_report(signals, elapsed_s=1000.0)
        assert report["has_positive_expectation"] is True

    def test_no_positive_expectation_when_negative(self):
        signals = [
            _make_signal("BTC", "jump", {60: -0.01, 180: -0.02, 300: -0.03}),
            _make_signal("BTC", "jump", {60: -0.005, 180: -0.01, 300: -0.015}),
        ]
        report = build_stats_report(signals, elapsed_s=1000.0)
        assert report["has_positive_expectation"] is False

    def test_missing_returns_excluded(self):
        # Signal with no forward returns yet → should not crash
        s = _make_signal("BTC", "jump", {})
        report = build_stats_report([s], elapsed_s=100.0)
        assert report["total_signals"] == 1
        assert report["by_horizon"][60]["jump"]["n"] == 0

    def test_multi_asset(self):
        signals = [
            _make_signal("BTC", "jump", {60: 0.01}),
            _make_signal("ETH", "drop", {60: -0.008}),
            _make_signal("SOL", "jump", {60: 0.003}),
            _make_signal("BTC", "jump", {60: 0.005}),
        ]
        report = build_stats_report(signals, elapsed_s=2000.0)
        assert report["by_asset"]["BTC"] == 2
        assert report["by_asset"]["ETH"] == 1
        assert report["by_asset"]["SOL"] == 1

    def test_elapsed_s_preserved(self):
        report = build_stats_report([], elapsed_s=7200.0)
        assert report["elapsed_s"] == pytest.approx(7200.0)


# ─────────────────────────────────────────────────────────────────────────────
# _percentile helper
# ─────────────────────────────────────────────────────────────────────────────

class TestPercentile:
    def test_single_value(self):
        assert _percentile([5.0], 50) == pytest.approx(5.0)

    def test_median_of_two(self):
        assert _percentile([0.0, 10.0], 50) == pytest.approx(5.0)

    def test_p25_of_four(self):
        result = _percentile([1.0, 2.0, 3.0, 4.0], 25)
        assert 1.0 <= result <= 2.0

    def test_p75_of_four(self):
        result = _percentile([1.0, 2.0, 3.0, 4.0], 75)
        assert 3.0 <= result <= 4.0

    def test_empty_returns_zero(self):
        assert _percentile([], 50) == pytest.approx(0.0)


# ─────────────────────────────────────────────────────────────────────────────
# format_markdown_report
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatMarkdownReport:
    def test_no_signals_no_crash(self):
        report = build_stats_report([], 0.0)
        md = format_markdown_report(report, [])
        assert "POLY_LEAD_STATS_REPORT" in md
        assert "STATS_ONLY" in md
        assert "No signals detected" in md

    def test_contains_safety_disclaimer(self):
        report = build_stats_report([], 0.0)
        md = format_markdown_report(report, [])
        assert "DRY_RUN" in md or "STATS_ONLY" in md
        assert "No real orders" in md

    def test_contains_forward_horizon_sections(self):
        report = build_stats_report([], 0.0)
        md = format_markdown_report(report, [])
        for h in FORWARD_HORIZONS:
            assert f"+{h}s" in md

    def test_contains_threshold_params(self):
        report = build_stats_report([], 0.0)
        md = format_markdown_report(report, [])
        assert "JUMP_10S" in md
        assert "JUMP_30S" in md
        assert "JUMP_60S" in md

    def test_signal_appears_in_recent_table(self):
        s = _make_signal("ETH", "jump", {60: 0.005, 180: 0.008, 300: 0.01})
        s.ts = 1_700_000_000.0  # fixed epoch for predictable output
        report = build_stats_report([s], 300.0)
        md = format_markdown_report(report, [s])
        assert "ETH" in md
        assert "jump" in md

    def test_has_positive_expectation_shown(self):
        signals = [
            _make_signal("BTC", "jump", {60: 0.01, 180: 0.02, 300: 0.03}),
            _make_signal("BTC", "jump", {60: 0.008, 180: 0.015, 300: 0.02}),
            _make_signal("BTC", "jump", {60: 0.005, 180: 0.01, 300: 0.015}),
        ]
        report = build_stats_report(signals, 3600.0)
        md = format_markdown_report(report, signals)
        assert "✅" in md or "positive" in md.lower()

    def test_pending_returns_shown_as_clock(self):
        s = _make_signal("SOL", "jump", {})  # no returns yet
        report = build_stats_report([s], 10.0)
        md = format_markdown_report(report, [s])
        assert "⏳" in md

    def test_exchange_comparison_section_present(self):
        report = build_stats_report([], 0.0)
        md = format_markdown_report(report, [])
        assert "Exchange Comparison" in md

    def test_real_order_disabled_in_report(self):
        report = build_stats_report([], 0.0)
        md = format_markdown_report(report, [])
        assert "REAL_ORDER_DISABLED" in md or "no real orders" in md.lower()

    def test_report_contains_both_exchanges(self):
        report = build_stats_report([], 0.0)
        md = format_markdown_report(report, [])
        assert "OKX" in md
        assert "BINANCE" in md

    def test_binance_returns_column_in_signal_table(self):
        s = _make_signal("BTC", "jump", {60: 0.01})
        s.binance_returns = {60: 0.012}
        report = build_stats_report([s], 100.0)
        md = format_markdown_report(report, [s])
        assert "BNB+60s" in md


# ─────────────────────────────────────────────────────────────────────────────
# Binance client — parse_binance_trade
# ─────────────────────────────────────────────────────────────────────────────

class TestParseBinanceTrade:
    def _msg(self, sym="BTCUSDT", price="30000.00", ts_ms=1_700_000_000_000) -> dict:
        return {
            "e": "trade",
            "E": ts_ms,
            "s": sym,
            "p": price,
            "q": "0.001",
            "T": ts_ms,
            "m": True,
        }

    def test_btcusdt_parsed(self):
        result = parse_binance_trade(self._msg("BTCUSDT", "30000.00"))
        assert result is not None
        asset, pt = result
        assert asset == "BTC"
        assert pt.price == pytest.approx(30000.0)

    def test_ethusdt_parsed(self):
        result = parse_binance_trade(self._msg("ETHUSDT", "2000.50"))
        assert result is not None
        asset, pt = result
        assert asset == "ETH"
        assert pt.price == pytest.approx(2000.50)

    def test_solusdt_parsed(self):
        result = parse_binance_trade(self._msg("SOLUSDT", "85.00"))
        assert result is not None
        asset, pt = result
        assert asset == "SOL"
        assert pt.price == pytest.approx(85.0)

    def test_unknown_symbol_returns_none(self):
        assert parse_binance_trade(self._msg("BNBUSDT", "300.0")) is None

    def test_wrong_event_type_returns_none(self):
        msg = self._msg()
        msg["e"] = "kline"
        assert parse_binance_trade(msg) is None

    def test_missing_price_returns_none(self):
        msg = self._msg()
        del msg["p"]
        assert parse_binance_trade(msg) is None

    def test_zero_price_returns_none(self):
        result = parse_binance_trade(self._msg("BTCUSDT", "0.0"))
        assert result is None

    def test_invalid_price_returns_none(self):
        result = parse_binance_trade(self._msg("BTCUSDT", "not_a_number"))
        assert result is None

    def test_timestamp_converted_to_seconds(self):
        ts_ms = 1_700_000_100_000
        result = parse_binance_trade(self._msg(ts_ms=ts_ms))
        assert result is not None
        _, pt = result
        assert pt.ts == pytest.approx(ts_ms / 1000.0)

    def test_missing_event_type_returns_none(self):
        msg = {"s": "BTCUSDT", "p": "30000", "T": 1_700_000_000_000}
        assert parse_binance_trade(msg) is None


class TestBinanceClientHelpers:
    def test_asset_to_stream(self):
        assert asset_to_stream("BTC") == "btcusdt@trade"
        assert asset_to_stream("ETH") == "ethusdt@trade"
        assert asset_to_stream("SOL") == "solusdt@trade"

    def test_unknown_asset_to_stream(self):
        assert asset_to_stream("XRP") is None

    def test_stream_url_single(self):
        url = stream_url(["BTC"])
        assert "btcusdt@trade" in url
        assert url.startswith("wss://")

    def test_stream_url_combined(self):
        url = stream_url(["BTC", "ETH", "SOL"])
        assert "btcusdt@trade" in url
        assert "ethusdt@trade" in url
        assert "solusdt@trade" in url


# ─────────────────────────────────────────────────────────────────────────────
# Dual-exchange forward return
# ─────────────────────────────────────────────────────────────────────────────

def _make_signal_dual(
    asset="BTC",
    direction="jump",
    okx_returns=None,
    bnb_returns=None,
) -> LeadSignal:
    s = _make_signal(asset, direction, okx_returns or {})
    if bnb_returns:
        s.binance_returns = bnb_returns
    return s


class TestDualExchangeReport:
    def test_exchanges_constant(self):
        assert "okx" in EXCHANGES
        assert "binance" in EXCHANGES

    def test_build_report_has_by_exchange_key(self):
        report = build_stats_report([], 0.0)
        assert "by_exchange" in report

    def test_by_exchange_contains_both_keys(self):
        report = build_stats_report([], 0.0)
        assert "okx" in report["by_exchange"]
        assert "binance" in report["by_exchange"]

    def test_exchange_comparison_key_present(self):
        report = build_stats_report([], 0.0)
        assert "exchange_comparison" in report

    def test_binance_returns_fill(self):
        s = _make_signal_dual(
            "ETH", "jump",
            okx_returns={60: 0.005, 180: 0.01, 300: 0.015},
            bnb_returns={60: 0.007, 180: 0.012, 300: 0.018},
        )
        assert s.binance_returns[60] == pytest.approx(0.007)

    def test_exchange_horizon_stats_okx(self):
        signals = [
            _make_signal_dual("BTC", "jump",
                              okx_returns={60: 0.01, 180: 0.02, 300: 0.03},
                              bnb_returns={60: 0.005}),
        ]
        h_stats = _exchange_horizon_stats(signals, "okx")
        assert h_stats[60]["jump"]["n"] == 1
        assert h_stats[60]["jump"]["mean"] == pytest.approx(0.01)

    def test_exchange_horizon_stats_binance(self):
        signals = [
            _make_signal_dual("ETH", "jump",
                              okx_returns={60: 0.003},
                              bnb_returns={60: 0.009, 180: 0.015, 300: 0.02}),
        ]
        h_stats = _exchange_horizon_stats(signals, "binance")
        assert h_stats[60]["jump"]["n"] == 1
        assert h_stats[60]["jump"]["mean"] == pytest.approx(0.009)

    def test_exchange_comparison_leader(self):
        # Binance has higher mean → should lead
        signals = [
            _make_signal_dual("BTC", "jump",
                              okx_returns={60: 0.003, 180: 0.005, 300: 0.007},
                              bnb_returns={60: 0.008, 180: 0.012, 300: 0.015}),
            _make_signal_dual("ETH", "jump",
                              okx_returns={60: 0.002, 180: 0.004, 300: 0.006},
                              bnb_returns={60: 0.006, 180: 0.010, 300: 0.013}),
        ]
        report = build_stats_report(signals, 1000.0)
        cmp = report["exchange_comparison"]
        # Binance mean > OKX mean for every horizon → binance leads
        assert cmp[60]["leader"] == "binance"

    def test_empty_binance_data_does_not_crash(self):
        signals = [
            _make_signal("BTC", "jump", {60: 0.01}),  # no binance_returns
        ]
        report = build_stats_report(signals, 100.0)
        assert report["by_exchange"]["binance"][60]["aligned"]["n"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Bybit returns integration (three-exchange)
# ─────────────────────────────────────────────────────────────────────────────

def _make_signal_triple(
    asset="BTC",
    direction="jump",
    okx_rets=None,
    bnb_rets=None,
    byb_rets=None,
) -> LeadSignal:
    s = _make_signal(asset, direction, okx_rets or {})
    if bnb_rets:
        s.binance_returns = bnb_rets
    if byb_rets:
        s.bybit_returns = byb_rets
    return s


class TestThreeExchangeStats:
    def test_exchanges_constant_has_bybit(self):
        assert "bybit" in EXCHANGES
        assert len(EXCHANGES) == 3

    def test_bybit_returns_field_on_signal(self):
        s = _make_signal_triple("BTC", "jump", byb_rets={60: 0.009})
        assert s.bybit_returns[60] == pytest.approx(0.009)

    def test_build_report_by_exchange_has_bybit(self):
        report = build_stats_report([], 0.0)
        assert "bybit" in report["by_exchange"]

    def test_bybit_stats_populated(self):
        signals = [
            _make_signal_triple(
                "ETH", "jump",
                okx_rets={60: 0.003, 180: 0.005, 300: 0.007},
                bnb_rets={60: 0.005, 180: 0.008, 300: 0.011},
                byb_rets={60: 0.007, 180: 0.011, 300: 0.015},
            ),
        ]
        report = build_stats_report(signals, 500.0)
        byb = report["by_exchange"]["bybit"]
        assert byb[60]["jump"]["n"] == 1
        assert byb[60]["jump"]["mean"] == pytest.approx(0.007)

    def test_missing_bybit_data_does_not_crash(self):
        signals = [_make_signal("BTC", "jump", {60: 0.01})]  # no bybit_returns
        report = build_stats_report(signals, 100.0)
        assert report["by_exchange"]["bybit"][60]["aligned"]["n"] == 0

    def test_exchange_horizon_stats_bybit(self):
        signals = [
            _make_signal_triple(
                "SOL", "jump",
                byb_rets={60: 0.012, 180: 0.018, 300: 0.025},
            ),
        ]
        h_stats = _exchange_horizon_stats(signals, "bybit")
        assert h_stats[60]["jump"]["n"] == 1
        assert h_stats[60]["jump"]["mean"] == pytest.approx(0.012)

    def test_report_contains_bybit_column(self):
        report = build_stats_report([], 0.0)
        md = format_markdown_report(report, [])
        assert "bybit" in md.lower()

    def test_report_contains_median_180_column(self):
        report = build_stats_report([], 0.0)
        md = format_markdown_report(report, [])
        assert "median_180s" in md

    def test_report_contains_median_300_column(self):
        report = build_stats_report([], 0.0)
        md = format_markdown_report(report, [])
        assert "median_300s" in md

    def test_bybit_column_in_signal_table(self):
        s = _make_signal_triple("BTC", "jump",
                                okx_rets={60: 0.01},
                                byb_rets={60: 0.009})
        report = build_stats_report([s], 100.0)
        md = format_markdown_report(report, [s])
        assert "BYB+60s" in md


# ─────────────────────────────────────────────────────────────────────────────
# Venue ranking
# ─────────────────────────────────────────────────────────────────────────────

class TestVenueRanking:
    def _ex_data(self, mean60=0.0, wr60=0.5, n60=10) -> dict:
        """Build a minimal by_exchange entry for one exchange."""
        return {
            h: {
                "aligned": {
                    "n": n60,
                    "mean": mean60,
                    "win_rate": wr60,
                    "median": mean60,
                    "p25": mean60 * 0.5,
                    "p75": mean60 * 1.5,
                }
            }
            for h in FORWARD_HORIZONS
        }

    def test_all_venues_present_in_ranking(self):
        by_ex = {ex: {} for ex in EXCHANGES}
        vr = _rank_venues(by_ex)
        assert set(vr["ranked"]) == set(EXCHANGES)

    def test_best_venue_has_highest_score(self):
        by_ex = {
            "okx":     self._ex_data(mean60=0.01, wr60=0.6),
            "binance": self._ex_data(mean60=0.02, wr60=0.7),  # best
            "bybit":   self._ex_data(mean60=0.005, wr60=0.45),
        }
        vr = _rank_venues(by_ex)
        assert vr["best_venue"] == "binance"

    def test_weakest_venue_has_lowest_score(self):
        by_ex = {
            "okx":     self._ex_data(mean60=0.01, wr60=0.6),
            "binance": self._ex_data(mean60=0.02, wr60=0.7),
            "bybit":   self._ex_data(mean60=-0.01, wr60=0.3),  # negative → weakest
        }
        vr = _rank_venues(by_ex)
        assert vr["weakest_venue"] == "bybit"

    def test_recommend_paper_trade_when_any_positive(self):
        by_ex = {
            "okx":     self._ex_data(mean60=0.01, wr60=0.6),  # positive expectation
            "binance": self._ex_data(mean60=-0.005, wr60=0.4),
            "bybit":   self._ex_data(mean60=0.0, wr60=0.5),
        }
        vr = _rank_venues(by_ex)
        assert vr["recommend_paper_trade"] is True

    def test_no_recommendation_when_all_negative(self):
        by_ex = {
            "okx":     self._ex_data(mean60=-0.01, wr60=0.3),
            "binance": self._ex_data(mean60=-0.02, wr60=0.2),
            "bybit":   self._ex_data(mean60=-0.005, wr60=0.4),
        }
        vr = _rank_venues(by_ex)
        assert vr["recommend_paper_trade"] is False

    def test_empty_data_no_crash(self):
        vr = _rank_venues({ex: {} for ex in EXCHANGES})
        assert "ranked" in vr
        assert "best_venue" in vr

    def test_venue_ranking_section_in_report(self):
        report = build_stats_report([], 0.0)
        md = format_markdown_report(report, [])
        assert "Venue Ranking" in md

    def test_venue_ranking_keys_in_report(self):
        report = build_stats_report([], 0.0)
        assert "venue_ranking" in report
        vr = report["venue_ranking"]
        assert "best_venue" in vr
        assert "second_venue" in vr
        assert "weakest_venue" in vr
        assert "recommend_paper_trade" in vr
