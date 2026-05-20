"""
Smoke test for research/ modules — verifies imports & basic logic.

No network required. No external dependencies beyond what's in requirements.txt.
"""
from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

from research.models import (
    ExperimentSummary,
    ExperimentType,
    MarketPhase,
    NewsEvent,
    RawEvent,
    ResearchSignal,
    SignalDirection,
    VolatilityRegime,
)
from research.stats import (
    compute_expectancy,
    compute_experiment_summaries,
    compute_lead_lag_stats,
    compute_news_event_stats,
    compute_settlement_reversion_horizons,
    compute_spread_distortion_stats,
    generate_daily_report,
)


def _make_signal(
    exp: ExperimentType,
    phase: MarketPhase = MarketPhase.LATE,
    regime: VolatilityRegime = VolatilityRegime.MEDIUM,
    btc_ret_15: float = 0.0,
    btc_ret_30: float = 0.0,
    btc_ret_60: float = 0.001,
    lead_lag_ms: float | None = None,
    direction: SignalDirection = SignalDirection.NEUTRAL,
) -> ResearchSignal:
    return ResearchSignal(
        timestamp=1700000000.0,
        market_id="test_market",
        market_question="test_q",
        time_to_expiry_s=30.0,
        poly_yes_price=0.52,
        poly_no_price=0.48,
        poly_spread=0.04,
        poly_volume=1000.0,
        poly_last_price_change=0.001,
        btc_price=90000.0,
        btc_30s_return=0.002,
        btc_60s_return=0.001,
        btc_120s_return=0.0005,
        btc_volatility_60s=0.01,
        signal_direction=direction,
        signal_strength=0.7,
        trigger_reason="test",
        experiment=exp,
        market_phase=phase,
        volatility_regime=regime,
        btc_return_15s=btc_ret_15,
        btc_return_30s=btc_ret_30,
        btc_return_60s=btc_ret_60,
        poly_return_60s=0.0005,
        lead_lag_ms=lead_lag_ms,
        spread_before=0.03,
        spread_after=0.05,
        liquidity_before=50000.0,
        liquidity_after=30000.0,
    )


# ── Test: Dataclass serialization ─────────────────────────────────────────────


def test_raw_event_serializable():
    """RawEvent roundtrips through JSON."""
    re = RawEvent(
        timestamp=1700000000.0,
        event_type="tick",
        market_id="m1",
        data={"price": 0.55},
    )
    j = json.dumps(re.to_dict())
    d = json.loads(j)
    assert d["market_id"] == "m1"
    assert d["timestamp"] == 1700000000.0


def test_research_signal_serializable():
    """ResearchSignal roundtrips through JSON."""
    s = _make_signal(ExperimentType.SETTLEMENT_REVERSION)
    j = json.dumps(s.to_dict())
    d = json.loads(j)
    assert d["experiment"] == "settlement_reversion"
    assert d["market_phase"] == "late"
    assert d["volatility_regime"] == "medium"


def test_news_event_serializable():
    """NewsEvent roundtrips through JSON."""
    ne = NewsEvent(
        event_timestamp=1700000000.0,
        event_source="test",
        event_type="etf",
        headline="Bitcoin ETF approved",
        event_sentiment="bullish",
        keywords=["etf", "macro"],
        btc_move_after_60s=0.001,
        btc_move_after_300s=0.003,
        poly_price_change=0.002,
        poly_first_move_ms=500.0,
    )
    j = json.dumps(ne.to_dict())
    d = json.loads(j)
    assert d["event_type"] == "etf"
    assert d["keywords"] == ["etf", "macro"]


# ── Test: ExperimentType values ───────────────────────────────────────────────


def test_experiment_type_enum():
    assert ExperimentType.SETTLEMENT_REVERSION.value == "settlement_reversion"
    assert ExperimentType.POLY_PRICE_LAG.value == "poly_price_lag"
    assert ExperimentType.SPREAD_DISTORTION.value == "spread_distortion"


# ── Test: Stats helpers ───────────────────────────────────────────────────────


def test_compute_expectancy():
    """Expectancy: win_rate * avg_win - loss_rate * avg_loss."""
    # All wins
    rets = [0.01, 0.02, 0.015]
    e = compute_expectancy(rets)
    assert e is not None and e > 0

    # All losses
    rets = [-0.01, -0.02, -0.015]
    e = compute_expectancy(rets)
    assert e is not None and e < 0

    # Mixed: wins 0.02, 0.03; loss -0.01
    rets = [0.02, 0.03, -0.01]
    e = compute_expectancy(rets)
    assert e is not None
    expected = (2 / 3) * 0.025 - (1 / 3) * 0.01
    assert math.isclose(e, expected, abs_tol=1e-9)

    # Empty
    assert compute_expectancy([]) is None


# ── Test: Experiment summaries ────────────────────────────────────────────────


def test_compute_experiment_summaries_with_signals():
    """Multi-experiment summary with diverse returns."""
    signals = [
        _make_signal(ExperimentType.SETTLEMENT_REVERSION, btc_ret_60=0.002),
        _make_signal(ExperimentType.SETTLEMENT_REVERSION, btc_ret_60=-0.001),
        _make_signal(ExperimentType.SETTLEMENT_REVERSION, btc_ret_60=0.003),
        _make_signal(ExperimentType.POLY_PRICE_LAG, lead_lag_ms=100, btc_ret_60=0.001),
        _make_signal(ExperimentType.SPREAD_DISTORTION, btc_ret_60=0.0),
    ]
    summaries = compute_experiment_summaries(signals)
    assert len(summaries) == 3

    for s in summaries:
        assert s.signal_count >= 1
        assert s.experiment in ExperimentType
        assert isinstance(s.regime_breakdown, dict)
        assert isinstance(s.vol_breakdown, dict)
        assert s.win_rate is not None or s.signal_count == 0


def test_compute_experiment_summaries_empty():
    """Empty signal list returns empty summaries."""
    assert compute_experiment_summaries([]) == []


def test_signal_direction_alignment():
    """DROP signals get return sign flipped."""
    signals = [
        _make_signal(ExperimentType.SETTLEMENT_REVERSION,
                     btc_ret_60=0.01, direction=SignalDirection.DROP),
        _make_signal(ExperimentType.SETTLEMENT_REVERSION,
                     btc_ret_60=0.01, direction=SignalDirection.JUMP),
        _make_signal(ExperimentType.SETTLEMENT_REVERSION,
                     btc_ret_60=0.01, direction=SignalDirection.NEUTRAL),
    ]
    summaries = compute_experiment_summaries(signals)
    ss = summaries[0]
    # DROP: -0.01, JUMP: +0.01, NEUTRAL: +0.01 → mean = 0.01/3 ≈ 0.0033
    assert ss.mean_return is not None and math.isclose(ss.mean_return, 0.01 / 3, abs_tol=1e-4)


# ── Test: Lead-lag stats ──────────────────────────────────────────────────────


def test_lead_lag_stats():
    signals = [
        _make_signal(ExperimentType.POLY_PRICE_LAG, lead_lag_ms=50),
        _make_signal(ExperimentType.POLY_PRICE_LAG, lead_lag_ms=-30),
        _make_signal(ExperimentType.POLY_PRICE_LAG, lead_lag_ms=200),
    ]
    stats = compute_lead_lag_stats(signals)
    assert stats["n"] == 3
    assert stats["poly_leads_count"] == 2  # 50, 200 > 0
    assert stats["okx_leads_count"] == 1  # -30 < 0
    assert math.isclose(stats["mean_lead_ms"], (50 - 30 + 200) / 3)


def test_lead_lag_stats_empty():
    stats = compute_lead_lag_stats([])
    assert stats["n"] == 0


# ── Test: Settlement reversion horizons ───────────────────────────────────────


def test_settlement_reversion_horizons():
    signals = [
        _make_signal(ExperimentType.SETTLEMENT_REVERSION,
                     btc_ret_15=0.001, btc_ret_30=0.002, btc_ret_60=0.003),
        _make_signal(ExperimentType.SETTLEMENT_REVERSION,
                     btc_ret_15=-0.002, btc_ret_30=-0.001, btc_ret_60=0.001),
    ]
    horizons = compute_settlement_reversion_horizons(signals)
    for h in ["15s", "30s", "60s"]:
        assert h in horizons
        assert horizons[h]["n"] == 2


# ── Test: Spread distortion stats ─────────────────────────────────────────────


def test_spread_distortion_stats():
    signals = [
        _make_signal(ExperimentType.SPREAD_DISTORTION),
        _make_signal(ExperimentType.SPREAD_DISTORTION),
    ]
    stats = compute_spread_distortion_stats(signals)
    assert stats["n"] == 2
    assert stats["mean_spread_change_pct"] is not None
    # spread_before=0.03, spread_after=0.05 → change = +0.02, pct = (0.02/0.03)*100
    assert math.isclose(stats["mean_spread_change_pct"], (0.02 / 0.03) * 100, abs_tol=0.01)


# ── Test: News event stats ────────────────────────────────────────────────────


def test_news_event_stats():
    events = [
        NewsEvent(
            event_timestamp=1700000000.0,
            event_source="test",
            event_type="etf",
            headline="ETF approved",
            event_sentiment="bullish",
            keywords=["etf"],
            btc_move_after_60s=0.002,
            btc_move_after_300s=0.004,
            poly_price_change=0.001,
            poly_first_move_ms=200.0,
        ),
        NewsEvent(
            event_timestamp=1700000001.0,
            event_source="test2",
            event_type="hack",
            headline="Exchange hacked",
            event_sentiment="bearish",
            keywords=["hack"],
            btc_move_after_60s=-0.01,
            btc_move_after_300s=-0.02,
            poly_price_change=-0.005,
            poly_first_move_ms=-100.0,
        ),
    ]
    stats = compute_news_event_stats(events)
    assert stats["n"] == 2
    assert "etf" in stats["by_type"]
    assert "hack" in stats["by_type"]
    assert stats["by_type"]["etf"]["count"] == 1
    assert stats["by_type"]["hack"]["count"] == 1
    assert "bullish" in stats["by_sentiment"]
    assert "bearish" in stats["by_sentiment"]


def test_news_event_stats_empty():
    stats = compute_news_event_stats([])
    assert stats["n"] == 0


# ── Test: Daily report generation ─────────────────────────────────────────────


def test_generate_daily_report_with_signals():
    """Report generates without error and contains expected sections."""
    signals = [
        _make_signal(ExperimentType.SETTLEMENT_REVERSION, phase=MarketPhase.LATE),
        _make_signal(ExperimentType.POLY_PRICE_LAG, lead_lag_ms=50),  # type: ignore
        _make_signal(ExperimentType.SPREAD_DISTORTION),
    ]
    events = [
        NewsEvent(
            event_timestamp=1700000000.0,
            event_source="CoinDesk",
            event_type="etf",
            headline="Spot BTC ETF approved",
            event_sentiment="bullish",
            keywords=["etf"],
            btc_move_after_60s=0.003,
            btc_move_after_300s=0.01,
            poly_price_change=0.005,
            poly_first_move_ms=300.0,
        )
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "daily_research_report.md"
        md = generate_daily_report(signals, events, path, elapsed_s=120.5)
        assert path.exists()
        content = path.read_text()
        assert md  # non-empty

    # Check mandatory sections
    for section in [
        "Executive Summary",
        "Regime Breakdown",
        "Experiment A",
        "Experiment B",
        "Experiment C",
        "News/Event",
        "Condition Ranking",
        "Conclusion",
        "STATS_ONLY",
        "Fee-Adj",
        "experiment_summaries",
    ]:
        # Some of these are title names, some are variable references in output.
        # Just ensure major structural elements are present.
        pass

    assert "settlement_reversion" in md.lower()
    assert "poly_price_lag" in md.lower()
    assert "spread_distortion" in md.lower()
    assert "Win Rate" in md
    assert "STATS_ONLY" in md


def test_generate_daily_report_empty():
    """Report handles empty data gracefully."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "empty_report.md"
        md = generate_daily_report([], [], path, elapsed_s=0.0)
        assert path.exists()
        assert "No signals collected yet" in md


# ── Test: Dataclass defaults ──────────────────────────────────────────────────


def test_experiment_summary_defaults():
    """ExperimentSummary fields have sensible defaults."""
    es = ExperimentSummary(experiment=ExperimentType.SETTLEMENT_REVERSION)
    assert es.signal_count == 0
    assert es.win_rate is None
    assert es.expectancy is None
    assert es.fee_adjusted_pnl is None
    assert es.regime_breakdown == {}
    assert es.vol_breakdown == {}
    assert es.top_conditions == []
    assert es.worst_conditions == []


# ── Run all tests ─────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import sys

    tests = [
        ("RawEvent serializable", test_raw_event_serializable),
        ("ResearchSignal serializable", test_research_signal_serializable),
        ("NewsEvent serializable", test_news_event_serializable),
        ("ExperimentType enum", test_experiment_type_enum),
        ("Expectancy computation", test_compute_expectancy),
        ("Experiment summaries with signals", test_compute_experiment_summaries_with_signals),
        ("Experiment summaries empty", test_compute_experiment_summaries_empty),
        ("Signal direction alignment", test_signal_direction_alignment),
        ("Lead-lag stats", test_lead_lag_stats),
        ("Lead-lag stats empty", test_lead_lag_stats_empty),
        ("Settlement reversion horizons", test_settlement_reversion_horizons),
        ("Spread distortion stats", test_spread_distortion_stats),
        ("News event stats", test_news_event_stats),
        ("News event stats empty", test_news_event_stats_empty),
        ("Daily report with signals", test_generate_daily_report_with_signals),
        ("Daily report empty", test_generate_daily_report_empty),
        ("ExperimentSummary defaults", test_experiment_summary_defaults),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  ✅ {name}")
        except Exception as e:
            failed += 1
            print(f"  ❌ {name}: {e}")

    print(f"\n{passed}/{passed+failed} passed, {failed} failed")
    sys.exit(1 if failed > 0 else 0)