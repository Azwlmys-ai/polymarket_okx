"""
tests/test_latency_threshold_scan.py — pure-function tests.

All offline. Covers:
  price_at_time, detect_cex_move, fill_poly_forward_samples,
  compute_threshold_stats, classify_threshold, replay_threshold.
"""

from __future__ import annotations

import math, sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from research.latency_threshold_scan import (
    RawStore,
    ThresholdStats,
    classify_threshold,
    compute_threshold_stats,
    detect_cex_move,
    fill_poly_forward_samples,
    price_at_time,
    replay_threshold,
)
from research.latency_edge_verifier import LagEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ticks(*pairs: tuple[int, float]) -> list[tuple[int, float]]:
    return sorted(pairs)


def _event(
    cex_move_ts_ms: int = 1_000_000,
    poly_yes: float = 0.50,
    market_id: str = "m1",
    asset: str = "BTC-USDT",
    direction: str = "up",
) -> LagEvent:
    return LagEvent(
        event_id=1, ts_utc="2026-01-01T00:00:00Z",
        asset=asset, market_id=market_id, market_title="test",
        ttl_s=300.0, cex_sources_agreed=["okx","binance"],
        cex_move_ts_ms=cex_move_ts_ms, cex_direction=direction,
        cex_move_pct=0.003, cex_price_before=78000.0, cex_price_after=78234.0,
        poly_yes_at_trigger=poly_yes,
    )


def _stats(
    threshold=0.001, n=0, s_hr=0.0, fr5=0.0, ne_p50=float("nan"),
) -> ThresholdStats:
    return ThresholdStats(
        threshold_pct=threshold, n_signals=n, signals_per_hour=s_hr,
        follow_rate_3s=fr5, follow_rate_5s=fr5, false_signal_rate=0.0,
        gross_edge_p50=None, net_edge_p50=ne_p50 if not math.isnan(ne_p50) else None,
        positive_net_edge_count=0, total_net_edges=0, verdict="",
    )


# ---------------------------------------------------------------------------
# price_at_time
# ---------------------------------------------------------------------------

class TestPriceAtTime:
    def test_exact_match(self):
        ticks = _ticks((1000, 100.0), (2000, 101.0), (3000, 102.0))
        assert price_at_time(ticks, 2000) == pytest.approx(101.0)

    def test_between_ticks(self):
        ticks = _ticks((1000, 100.0), (3000, 102.0))
        assert price_at_time(ticks, 2000) == pytest.approx(100.0)  # most recent before

    def test_before_all_ticks(self):
        ticks = _ticks((5000, 100.0))
        assert price_at_time(ticks, 1000) is None

    def test_after_all_ticks(self):
        ticks = _ticks((1000, 100.0), (2000, 101.0))
        assert price_at_time(ticks, 9999) == pytest.approx(101.0)

    def test_empty(self):
        assert price_at_time([], 5000) is None

    def test_single_tick_at_time(self):
        assert price_at_time([(1000, 42.0)], 1000) == pytest.approx(42.0)


# ---------------------------------------------------------------------------
# detect_cex_move
# ---------------------------------------------------------------------------

class TestDetectCexMove:
    def _sources(self, okx_pct=0.0, bnb_pct=0.0, byb_pct=0.0) -> dict:
        base_ts, base_p = 0, 100.0
        now_ts = 10_000  # 10s later
        sources = {}
        for name, pct in [("okx", okx_pct), ("binance", bnb_pct), ("bybit", byb_pct)]:
            now_p = base_p * (1 + pct)
            sources[name] = _ticks((base_ts, base_p), (now_ts, now_p))
        return sources

    def test_consensus_up_two_sources(self):
        src = self._sources(okx_pct=0.003, bnb_pct=0.003, byb_pct=0.001)
        result = detect_cex_move(src, 10_000, 0.002, 10_000)
        assert result is not None
        direction, sources = result
        assert direction == "up"
        assert len(sources) >= 2

    def test_consensus_down(self):
        src = self._sources(okx_pct=-0.003, bnb_pct=-0.003, byb_pct=0.001)
        result = detect_cex_move(src, 10_000, 0.002, 10_000)
        assert result is not None
        assert result[0] == "down"

    def test_no_consensus_only_one_source(self):
        src = self._sources(okx_pct=0.003, bnb_pct=0.0, byb_pct=0.0)
        result = detect_cex_move(src, 10_000, 0.002, 10_000)
        assert result is None

    def test_threshold_not_met(self):
        src = self._sources(okx_pct=0.001, bnb_pct=0.001, byb_pct=0.001)
        result = detect_cex_move(src, 10_000, 0.002, 10_000)
        assert result is None

    def test_mixed_directions_no_consensus(self):
        # One up, one down → no direction reaches consensus
        src = self._sources(okx_pct=0.003, bnb_pct=-0.003, byb_pct=0.0)
        result = detect_cex_move(src, 10_000, 0.002, 10_000)
        assert result is None

    def test_empty_sources(self):
        assert detect_cex_move({}, 10_000, 0.002, 10_000) is None


# ---------------------------------------------------------------------------
# fill_poly_forward_samples
# ---------------------------------------------------------------------------

class TestFillPolyForwardSamples:
    def test_fills_horizons(self):
        store = RawStore()
        t0 = 1_000_000
        store.poly_polls["m1"] = [
            (t0 + 1_000,   0.51),   # 1s
            (t0 + 3_000,   0.53),   # 3s
            (t0 + 60_000,  0.56),   # 60s
            (t0 + 300_000, 0.60),   # 300s
        ]
        ev = _event(cex_move_ts_ms=t0, market_id="m1")
        fill_poly_forward_samples(ev, store, horizons_s=[1, 3, 60, 300])
        assert ev.poly_samples[1_000]   == pytest.approx(0.51)
        assert ev.poly_samples[3_000]   == pytest.approx(0.53)
        assert ev.poly_samples[60_000]  == pytest.approx(0.56)
        assert ev.poly_samples[300_000] == pytest.approx(0.60)

    def test_missing_market_no_crash(self):
        store = RawStore()
        ev = _event(market_id="nonexistent")
        fill_poly_forward_samples(ev, store)
        assert ev.poly_samples == {}

    def test_horizon_before_any_poll(self):
        # Poll exists at t0+5s but not at t0+1s.
        # fill_poly_forward_samples finds the FIRST poll >= target time,
        # so for the 1s horizon (target=t0+1000), it correctly uses the
        # t0+5000 poll (the next available after the horizon).
        store = RawStore()
        t0 = 1_000_000
        store.poly_polls["m1"] = [(t0 + 5_000, 0.55)]
        ev = _event(cex_move_ts_ms=t0, market_id="m1")
        fill_poly_forward_samples(ev, store, horizons_s=[1])
        assert ev.poly_samples.get(1_000) == pytest.approx(0.55)  # next available poll


# ---------------------------------------------------------------------------
# classify_threshold
# ---------------------------------------------------------------------------

class TestClassifyThreshold:
    def test_too_strict(self):
        assert classify_threshold(2, 0.5, 0.8, 0.05) == "THRESHOLD TOO STRICT"

    def test_too_strict_low_rate(self):
        assert classify_threshold(5, 2.0, 0.8, 0.05) == "THRESHOLD TOO STRICT"

    def test_no_edge_negative(self):
        assert classify_threshold(50, 8.0, 0.60, -0.01) == "NO MEASURABLE EDGE"

    def test_no_edge_low_follow(self):
        assert classify_threshold(50, 8.0, 0.30, 0.02) == "NO MEASURABLE EDGE"

    def test_weak_paper_edge(self):
        # above minimum signals and follow, but net edge below 3.5 cent hurdle
        assert classify_threshold(50, 8.0, 0.60, 0.005) == "WEAK PAPER EDGE"

    def test_candidate_found(self):
        assert classify_threshold(50, 8.0, 0.65, 0.04) == "CANDIDATE THRESHOLD FOUND"

    def test_nan_edge_is_no_edge(self):
        assert classify_threshold(50, 8.0, 0.60, float("nan")) == "NO MEASURABLE EDGE"

    def test_exact_boundary_signals(self):
        # Exactly at minimum signals/hr
        result = classify_threshold(30, 5.0, 0.60, 0.04)
        assert result == "CANDIDATE THRESHOLD FOUND"

    def test_zero_edge_is_no_edge(self):
        assert classify_threshold(50, 8.0, 0.60, 0.0) == "NO MEASURABLE EDGE"


# ---------------------------------------------------------------------------
# compute_threshold_stats
# ---------------------------------------------------------------------------

class TestComputeThresholdStats:
    def _make_events(self, n: int, lag_ms=2000, yes_move=0.06) -> list[LagEvent]:
        """Create n synthetic resolved events."""
        events = []
        t0 = int(time.time() * 1000) - 400_000
        for i in range(n):
            ev = _event(cex_move_ts_ms=t0 + i * 10_000, poly_yes=0.50)
            ev.poly_samples = {
                3_000:   0.50 + yes_move * 0.5,
                5_000:   0.50 + yes_move * 0.8,
                60_000:  0.50 + yes_move,
                300_000: 0.50 + yes_move,
            }
            from research.latency_edge_verifier import derive_event_stats
            derive_event_stats(ev)
            events.append(ev)
        return events

    def test_no_events(self):
        stats = compute_threshold_stats([], 3600, 0.001)
        assert stats.n_signals == 0
        assert stats.verdict == "THRESHOLD TOO STRICT"

    def test_signals_per_hour(self):
        events = self._make_events(30)
        stats = compute_threshold_stats(events, 3600, 0.001)
        assert stats.signals_per_hour == pytest.approx(30.0, rel=0.1)

    def test_positive_edge_counted(self):
        events = self._make_events(30, yes_move=0.10)  # 10 cent move > 3.5 cent fee
        stats = compute_threshold_stats(events, 3600, 0.001)
        assert stats.positive_net_edge_count > 0

    def test_verdict_too_strict_when_few(self):
        stats = compute_threshold_stats(self._make_events(5), 3600, 0.001)
        assert stats.verdict == "THRESHOLD TOO STRICT"


# ---------------------------------------------------------------------------
# replay_threshold  (minimal smoke test with synthetic data)
# ---------------------------------------------------------------------------

class TestReplayThreshold:
    def _make_store(
        self,
        n_ticks: int = 100,
        jump_ts_ms: int = 50_000,
        jump_pct: float = 0.005,
    ) -> RawStore:
        """Synthetic RawStore with a single known price jump."""
        store = RawStore()
        # Build price series: flat then jump at jump_ts_ms
        for src, asset_key in [("okx","BTC-USDT"), ("binance","BTC"), ("bybit","BTC")]:
            ticks = []
            base_price = 78000.0
            for i in range(n_ticks):
                ts = i * 1_000   # 1 tick per second
                price = base_price * (1 + jump_pct) if ts >= jump_ts_ms else base_price
                ticks.append((ts, price))
            store.ticks[f"{src}:{asset_key}"] = sorted(ticks)
        # Poly market
        store.poly_markets["m1"] = ("BTC-USDT", "BTC Up or Down test")
        for i in range(n_ticks):
            ts = i * 1_000
            store.poly_polls["m1"].append((ts, 0.50 + (0.03 if ts >= jump_ts_ms else 0.0)))
        store.elapsed_s = n_ticks
        return store

    def test_detects_jump_above_threshold(self):
        # Jump at 70s so the 60s cooldown from t=0 is cleared before detection.
        # (At ts=50s: 50000 - 0 = 50000 < 60000 cooldown → blocked.
        #  At ts=70s: 70000 - 0 = 70000 >= 60000 cooldown → fires.)
        store = self._make_store(n_ticks=150, jump_ts_ms=70_000, jump_pct=0.005)
        events = replay_threshold(store, threshold_pct=0.004, cooldown_s=60.0)
        assert len(events) >= 1

    def test_no_events_below_threshold(self):
        store = self._make_store(jump_pct=0.002)
        events = replay_threshold(store, threshold_pct=0.004)
        assert len(events) == 0

    def test_cooldown_prevents_duplicates(self):
        store = self._make_store(n_ticks=200, jump_pct=0.006)
        events = replay_threshold(store, threshold_pct=0.004, cooldown_s=30.0)
        # With 60s cooldown and 100s window, should get at most a few signals
        assert len(events) <= 5

    def test_empty_store_returns_empty(self):
        store = RawStore()
        assert replay_threshold(store, 0.002) == []
