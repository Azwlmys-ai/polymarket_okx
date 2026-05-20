"""
research/stats.py — Statistical analysis for microstructure experiments.

STATS_ONLY — No real orders. No capital at risk.

Computes:
  - Per-experiment summaries
  - Forward return statistics
  - Regime breakdowns
  - Fee-adjusted PnL
  - Top/worst condition ranking
  - Daily research reports
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from research.models import (
    ExperimentSummary,
    ExperimentType,
    MarketPhase,
    ResearchSignal,
    SignalDirection,
    VolatilityRegime,
)


# ─────────────────────────────────────────────────────────────────────────────
# Core statistic helpers
# ─────────────────────────────────────────────────────────────────────────────


def _percentile(sv: list[float], p: float) -> float:
    n = len(sv)
    if n == 0:
        return 0.0
    if n == 1:
        return sv[0]
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return sv[lo] + (idx - lo) * (sv[hi] - sv[lo])


def _direction_stats(returns: list[float]) -> dict:
    """Descriptive stats for a list of returns."""
    if not returns:
        return {"n": 0, "win_rate": None, "mean": None,
                "median": None, "p25": None, "p75": None,
                "max_drawdown": None}
    sv = sorted(returns)
    n = len(sv)
    mean = sum(sv) / n
    win_rate = sum(1 for r in sv if r > 0) / n

    # Max drawdown (running peak-to-trough on cumulative sum)
    cumsum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in returns:
        cumsum += r
        if cumsum > peak:
            peak = cumsum
        dd = peak - cumsum
        if dd > max_dd:
            max_dd = dd

    return {
        "n": n,
        "win_rate": win_rate,
        "mean": mean,
        "median": _percentile(sv, 50),
        "p25": _percentile(sv, 25),
        "p75": _percentile(sv, 75),
        "max_drawdown": max_dd,
    }


def compute_expectancy(returns: list[float]) -> Optional[float]:
    """
    Expectancy = win_rate * avg_win - loss_rate * avg_loss
    """
    if not returns:
        return None
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    n = len(returns)
    if n == 0:
        return None
    win_rate = len(wins) / n
    loss_rate = len(losses) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    return win_rate * avg_win - loss_rate * avg_loss


# ─────────────────────────────────────────────────────────────────────────────
# Per-experiment summary computation
# ─────────────────────────────────────────────────────────────────────────────


def compute_experiment_summaries(
    signals: list[ResearchSignal],
) -> list[ExperimentSummary]:
    """Compute ExperimentSummary for each experiment type present in signals."""
    by_exp: dict[ExperimentType, list[ResearchSignal]] = defaultdict(list)
    for s in signals:
        by_exp[s.experiment].append(s)

    summaries = []
    for exp_type, exp_signals in by_exp.items():
        summary = _summarize_one(exp_type, exp_signals)
        summaries.append(summary)
    return summaries


def _summarize_one(
    exp_type: ExperimentType,
    signals: list[ResearchSignal],
) -> ExperimentSummary:
    """Build ExperimentSummary for one experiment type."""
    # Regime breakdown
    regime_bd: dict[str, int] = defaultdict(int)
    vol_bd: dict[str, int] = defaultdict(int)
    for s in signals:
        regime_bd[s.market_phase.value] += 1
        vol_bd[s.volatility_regime.value] += 1

    # Pick the right forward return for expectancy
    # Settlement reversion: use btc_return_60s (aligned to signal direction)
    # Poly price lag: use btc_return_60s (aligned)
    # Spread distortion: observational only, use btc_return_60s
    aligned_rets = _aligned_returns(signals, horizon="btc_return_60s")
    poly_rets = _aligned_returns(signals, horizon="poly_return_60s")

    stats = _direction_stats(aligned_rets)
    expectancy = compute_expectancy(aligned_rets)

    # Fee-adjusted: assume 2% Poly round-trip + 0.1% OKX = 0.021
    FEE = 0.021
    fee_adj_rets = [r - FEE for r in aligned_rets]
    fee_stats = _direction_stats(fee_adj_rets)
    fee_adj_pnl = sum(fee_adj_rets) if fee_adj_rets else None

    # Top / worst conditions by market_phase × volatility_regime
    top, worst = _rank_conditions(signals, aligned_rets)

    return ExperimentSummary(
        experiment=exp_type,
        signal_count=len(signals),
        regime_breakdown=dict(regime_bd),
        vol_breakdown=dict(vol_bd),
        win_rate=stats["win_rate"],
        mean_return=stats["mean"],
        median_return=stats["median"],
        max_drawdown=stats["max_drawdown"],
        expectancy=expectancy,
        fee_adjusted_pnl=fee_adj_pnl,
        top_conditions=top,
        worst_conditions=worst,
    )


def _aligned_returns(
    signals: list[ResearchSignal],
    horizon: str = "btc_return_60s",
) -> list[float]:
    """
    Align returns to signal direction:
      - jump → expect positive return → keep as-is
      - drop → expect negative return → flip sign
      - neutral → keep as-is
    """
    aligned = []
    for s in signals:
        ret = getattr(s, horizon, None)
        if ret is None:
            continue
        if s.signal_direction == SignalDirection.DROP:
            aligned.append(-ret)
        elif s.signal_direction == SignalDirection.JUMP:
            aligned.append(ret)
        else:
            aligned.append(ret)
    return aligned


def _rank_conditions(
    signals: list[ResearchSignal],
    aligned_rets: list[float],
) -> tuple[list[dict], list[dict]]:
    """
    Rank (market_phase, volatility_regime) combos by aligned mean return.
    Returns (top_3, worst_3).
    """
    # Build per-condition groups
    groups: dict[tuple, list[float]] = defaultdict(list)
    for s, ret in zip(signals, aligned_rets):
        key = (s.market_phase.value, s.volatility_regime.value)
        groups[key].append(ret)

    scored = []
    for key, rets in groups.items():
        if len(rets) < 3:
            continue
        stats = _direction_stats(rets)
        scored.append({
            "phase": key[0],
            "regime": key[1],
            "count": len(rets),
            "win_rate": stats["win_rate"],
            "mean_return": stats["mean"],
            "expectancy": compute_expectancy(rets),
        })

    scored.sort(key=lambda x: x.get("expectancy") or -999, reverse=True)
    top = scored[:3]
    worst = scored[-3:] if len(scored) >= 3 else []
    worst.reverse()
    return top, worst


# ─────────────────────────────────────────────────────────────────────────────
# Lead-lag aggregation (Experiment B specific)
# ─────────────────────────────────────────────────────────────────────────────


def compute_lead_lag_stats(signals: list[ResearchSignal]) -> dict:
    """Aggregate lead-lag statistics for POLY_PRICE_LAG signals."""
    lag_signals = [s for s in signals
                   if s.experiment == ExperimentType.POLY_PRICE_LAG
                   and s.lead_lag_ms is not None]
    if not lag_signals:
        return {
            "n": 0,
            "poly_leads_count": 0,
            "okx_leads_count": 0,
            "mean_lead_ms": None,
            "median_lead_ms": None,
            "p25_lead_ms": None,
            "p75_lead_ms": None,
        }

    lags = [s.lead_lag_ms for s in lag_signals]
    poly_leads = sum(1 for l in lags if l > 0)
    okx_leads = sum(1 for l in lags if l < 0)
    sorted_lags = sorted(lags)

    return {
        "n": len(lags),
        "poly_leads_count": poly_leads,
        "okx_leads_count": okx_leads,
        "poly_leads_pct": poly_leads / len(lags) if lags else None,
        "mean_lead_ms": sum(lags) / len(lags) if lags else None,
        "median_lead_ms": _percentile(sorted_lags, 50),
        "p25_lead_ms": _percentile(sorted_lags, 25),
        "p75_lead_ms": _percentile(sorted_lags, 75),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Settlement reversion horizon analysis (Experiment A specific)
# ─────────────────────────────────────────────────────────────────────────────


def compute_settlement_reversion_horizons(
    signals: list[ResearchSignal],
) -> dict:
    """Compute BTC mean reversion stats at 15s, 30s, 60s for settlement signals."""
    rev_signals = [s for s in signals
                   if s.experiment == ExperimentType.SETTLEMENT_REVERSION]

    result = {}
    for horizon_label, horizon_attr in [
        ("15s", "btc_return_15s"),
        ("30s", "btc_return_30s"),
        ("60s", "btc_return_60s"),
    ]:
        rets = _aligned_returns(rev_signals, horizon=horizon_attr)
        stats = _direction_stats(rets)
        result[horizon_label] = {
            "n": stats["n"],
            "win_rate": stats["win_rate"],
            "mean_return": stats["mean"],
            "median_return": stats["median"],
            "expectancy": compute_expectancy(rets),
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Spread distortion aggregation (Experiment C specific)
# ─────────────────────────────────────────────────────────────────────────────


def compute_spread_distortion_stats(signals: list[ResearchSignal]) -> dict:
    """Aggregate spread distortion observations."""
    dist_signals = [s for s in signals
                    if s.experiment == ExperimentType.SPREAD_DISTORTION]

    if not dist_signals:
        return {"n": 0}

    spread_changes = []
    liq_changes = []
    for s in dist_signals:
        if s.spread_before and s.spread_after and s.spread_before > 0:
            spread_changes.append((s.spread_after - s.spread_before) / s.spread_before)
        if s.liquidity_before and s.liquidity_after and s.liquidity_before > 0:
            liq_changes.append((s.liquidity_after - s.liquidity_before) / s.liquidity_before)

    # Check BTC subsequent moves
    btc_rets = _aligned_returns(dist_signals, horizon="btc_return_60s")

    return {
        "n": len(dist_signals),
        "phase_breakdown": dict(_count_by_phase(dist_signals)),
        "mean_spread_change_pct": sum(spread_changes) / len(spread_changes) * 100 if spread_changes else None,
        "mean_liquidity_change_pct": sum(liq_changes) / len(liq_changes) * 100 if liq_changes else None,
        "spread_stats": _direction_stats(spread_changes),
        "liquidity_stats": _direction_stats(liq_changes),
        "btc_subsequent_stats": _direction_stats(btc_rets),
    }


def _count_by_phase(signals: list[ResearchSignal]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for s in signals:
        counts[s.market_phase.value] += 1
    return dict(counts)


# ─────────────────────────────────────────────────────────────────────────────
# News event analysis (Phase 2)
# ─────────────────────────────────────────────────────────────────────────────


def compute_news_event_stats(news_events: list) -> dict:
    """Aggregate news event outcomes."""
    from research.models import NewsEvent
    if not news_events:
        return {"n": 0}

    by_type: dict[str, list] = defaultdict(list)
    by_sentiment: dict[str, list] = defaultdict(list)
    btc_moves_60s = []
    btc_moves_300s = []
    poly_moves = []
    poly_leads = []

    for ne in news_events:
        if ne.btc_move_after_60s is not None:
            btc_moves_60s.append(ne.btc_move_after_60s)
        if ne.btc_move_after_300s is not None:
            btc_moves_300s.append(ne.btc_move_after_300s)
        if ne.poly_price_change is not None:
            poly_moves.append(ne.poly_price_change)
        if ne.poly_first_move_ms is not None:
            poly_leads.append(ne.poly_first_move_ms)
        by_type[ne.event_type].append(ne)
        by_sentiment[ne.event_sentiment].append(ne)

    type_stats = {}
    for etype, events in by_type.items():
        moves = [e.btc_move_after_60s for e in events if e.btc_move_after_60s is not None]
        type_stats[etype] = {
            "count": len(events),
            "btc_60s_stats": _direction_stats(moves),
        }

    sentiment_stats = {}
    for sent, events in by_sentiment.items():
        moves = [e.btc_move_after_60s for e in events if e.btc_move_after_60s is not None]
        sentiment_stats[sent] = {
            "count": len(events),
            "btc_60s_stats": _direction_stats(moves),
        }

    return {
        "n": len(news_events),
        "by_type": type_stats,
        "by_sentiment": sentiment_stats,
        "btc_60s": _direction_stats(btc_moves_60s),
        "btc_300s": _direction_stats(btc_moves_300s),
        "poly_leads_count": sum(1 for l in poly_leads if l > 0),
        "poly_leads_total": len(poly_leads),
        "mean_poly_lead_ms": sum(poly_leads) / len(poly_leads) if poly_leads else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Randomized baseline — bootstraps a null distribution
# ─────────────────────────────────────────────────────────────────────────────


def compute_randomized_baseline(
    signals: list[ResearchSignal],
    n_boot: int = 500,
    seed: int = 42,
) -> dict:
    """
    Simulate a naive random-entry strategy on the same underlying data.

    Methodology:
      - Collect all available btc_return_60s from signals as the "returns pool".
      - Bootstrap random draws (same count as signals) to estimate null expectancy.
      - Return the 95th percentile as a conservative baseline.
    """
    import random as _rnd
    _rnd.seed(seed)

    pool = [s.btc_return_60s for s in signals if s.btc_return_60s is not None]
    if len(pool) < 10:
        return {
            "n_pool": len(pool),
            "baseline_win_rate": None,
            "baseline_expectancy": None,
            "baseline_mean": None,
            "baseline_p95_win_rate": None,
            "baseline_p95_expectancy": None,
            "method": "bootstrap (insufficient data)",
        }

    n_signals = len(signals)

    win_rates: list[float] = []
    expectancies: list[float] = []
    for _ in range(n_boot):
        sample = [_rnd.choice(pool) * _rnd.choice([1.0, -1.0]) for _ in range(n_signals)]
        wins = sum(1 for s in sample if s > 0)
        win_rates.append(wins / n_signals)
        expectancies.append(compute_expectancy(sample) or 0.0)

    win_rates.sort()
    expectancies.sort()

    return {
        "n_pool": len(pool),
        "baseline_win_rate": sum(win_rates) / n_boot,
        "baseline_expectancy": sum(expectancies) / n_boot,
        "baseline_mean": sum(expectancies) / n_boot,
        "baseline_p95_win_rate": _percentile(win_rates, 95),
        "baseline_p95_expectancy": _percentile(expectancies, 95),
        "method": f"bootstrap (n={n_signals}, pool={len(pool)}, iter={n_boot})",
    }


# ─────────────────────────────────────────────────────────────────────────────
# GO / NO-GO Gate
# ─────────────────────────────────────────────────────────────────────────────


def go_nogo_check(
    summaries: list[ExperimentSummary],
    signals: list[ResearchSignal],
    n_sessions_completed: int = 1,
) -> dict:
    """
    Formal go/no-go decision for paper-execution readiness.

    All criteria must be satisfied → GO. Otherwise → NO-GO.
    """
    baseline = compute_randomized_baseline(signals)

    primary = None
    for summ in summaries:
        if primary is None or summ.signal_count > primary.signal_count:
            primary = summ

    if primary is None:
        return {
            "verdict": "NO-GO",
            "reason": "No experiment summaries available.",
            "criteria": {},
            "baseline": baseline,
            "n_sessions": n_sessions_completed,
        }

    FEE = 0.021

    net_exp_after_fee = (
        primary.expectancy - FEE if primary.expectancy is not None else None
    )
    median_after_fee = (
        primary.median_return - FEE if primary.median_return is not None else None
    )

    strategy_wr = primary.win_rate or 0.0
    baseline_wr = baseline.get("baseline_p95_win_rate") or 0.0

    # Concentration check: >70% of signals in any 600s window
    concentrated = False
    max_in_window = 0
    if signals:
        timestamps = sorted([s.timestamp for s in signals])
        j = 0
        for i in range(len(timestamps)):
            while j < len(timestamps) and timestamps[j] - timestamps[i] <= 600.0:
                j += 1
            count = j - i
            if count > max_in_window:
                max_in_window = count
        concentrated = len(timestamps) > 0 and (max_in_window / len(timestamps) > 0.70)

    criteria = {
        "c1_signal_count_ge_50": {
            "met": primary.signal_count >= 50,
            "value": primary.signal_count,
            "threshold": 50,
        },
        "c2_expectancy_gt_0_after_fee": {
            "met": net_exp_after_fee is not None and net_exp_after_fee > 0,
            "value": net_exp_after_fee,
            "threshold": 0,
        },
        "c3_median_gt_0_after_fee": {
            "met": median_after_fee is not None and median_after_fee > 0,
            "value": median_after_fee,
            "threshold": 0,
        },
        "c4_win_rate_vs_baseline": {
            "met": baseline_wr > 0 and strategy_wr >= baseline_wr + 0.05,
            "value": strategy_wr,
            "threshold": baseline_wr + 0.05,
            "baseline": baseline_wr,
        },
        "c5_min_2_sessions": {
            "met": n_sessions_completed >= 2,
            "value": n_sessions_completed,
            "threshold": 2,
            "note": "Must run ≥2 independent sessions with consistent GO results.",
        },
        "c6_max_drawdown_ok": {
            "met": primary.max_drawdown is not None and primary.max_drawdown < 0.10,
            "value": primary.max_drawdown,
            "threshold": "< 0.10 (10%)",
        },
        "c7_not_concentrated": {
            "met": not concentrated,
            "value": f"{max_in_window}/{len(timestamps)}" if signals else "0/0",
            "threshold": "≤ 70%",
        },
    }

    all_met = all(c["met"] for c in criteria.values())
    unmet = [k for k, c in criteria.items() if not c["met"]]

    return {
        "verdict": "GO" if all_met else "NO-GO",
        "reason": (
            "All criteria satisfied — ready for paper execution."
            if all_met
            else f"Failed criteria: {', '.join(unmet)}"
        ),
        "criteria": criteria,
        "baseline": baseline,
        "n_sessions": n_sessions_completed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Daily research report generator
# ─────────────────────────────────────────────────────────────────────────────


def generate_daily_report(
    signals: list[ResearchSignal],
    news_events: list,
    output_path: Path,
    elapsed_s: float = 0.0,
) -> str:
    """
    Generate daily_research_report.md.

    Required content:
      - signal count
      - regime breakdown
      - expectancy
      - median vs mean
      - fee-adjusted pnl
      - top-performing conditions
      - worst-performing conditions
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    h_str = f"{elapsed_s / 3600:.1f}h" if elapsed_s >= 3600 else f"{elapsed_s:.0f}s"

    summaries = compute_experiment_summaries(signals)
    lag_stats = compute_lead_lag_stats(signals)
    settlement_horizons = compute_settlement_reversion_horizons(signals)
    spread_stats = compute_spread_distortion_stats(signals)
    news_stats = compute_news_event_stats(news_events)

    def _fmt_pct(v: Optional[float]) -> str:
        return f"{v:.4%}" if v is not None else "N/A"

    def _fmt_ms(v: Optional[float]) -> str:
        return f"{v:.1f}ms" if v is not None else "N/A"

    lines = [
        "# Daily Research Report — Polymarket Microstructure Alpha",
        "",
        "> **STATS_ONLY / PAPER_SIM — No real orders. No capital at risk.**",
        "",
        f"**Generated:** {now}  ",
        f"**Session Duration:** {h_str}  ",
        f"**Total Signals:** {len(signals)}  ",
        f"**News Events:** {len(news_events)}  ",
        "",
        "---",
        "",
        "## 1. Executive Summary",
        "",
    ]

    if not signals:
        lines += [
            "_No signals collected yet. Run the research engine to gather data._",
            "",
        ]
    else:
        # Per-experiment summary
        lines.append("| Experiment | N | Win Rate | Mean | Median | Expectancy | Fee-Adj PnL |")
        lines.append("|---|---|---|---|---|---|---|")
        for summ in summaries:
            lines.append(
                f"| **{summ.experiment.value}** | {summ.signal_count} "
                f"| {_fmt_pct(summ.win_rate)} "
                f"| {_fmt_pct(summ.mean_return)} "
                f"| {_fmt_pct(summ.median_return)} "
                f"| {_fmt_pct(summ.expectancy)} "
                f"| {_fmt_pct(summ.fee_adjusted_pnl)} |"
            )
        lines.append("")

        # Overall verdict
        any_positive = any(
            (s.expectancy or 0) > 0 and (s.win_rate or 0) > 0.5
            for s in summaries
        )
        if any_positive:
            lines.append("### ✅ Positive Edge Detected")
            lines.append("")
            for s in summaries:
                if (s.expectancy or 0) > 0 and (s.win_rate or 0) > 0.5:
                    lines.append(
                        f"- **{s.experiment.value}**: expectancy={_fmt_pct(s.expectancy)}, "
                        f"win_rate={_fmt_pct(s.win_rate)}, n={s.signal_count}"
                    )
            lines.append("")
        else:
            lines.append("### ❌ No Consistent Edge Detected")
            lines.append("")
            lines.append(
                "Continue data collection. Increase sample size. "
                "Adjust detection thresholds if needed."
            )
            lines.append("")

    # ── Regime Breakdown ──────────────────────────────────────────────────────
    lines += ["---", "", "## 2. Regime Breakdown", ""]
    all_phases: dict[str, int] = defaultdict(int)
    all_vols: dict[str, int] = defaultdict(int)
    for s in signals:
        all_phases[s.market_phase.value] += 1
        all_vols[s.volatility_regime.value] += 1

    lines.append("### Market Phase Distribution")
    lines.append("| Phase | Count | Pct |")
    lines.append("|---|---|")
    total = len(signals) or 1
    for phase in ["early", "mid", "late", "settlement"]:
        c = all_phases.get(phase, 0)
        lines.append(f"| {phase} | {c} | {c/total:.1%} |")
    lines.append("")

    lines.append("### Volatility Regime Distribution")
    lines.append("| Regime | Count | Pct |")
    lines.append("|---|---|")
    for regime in ["low", "medium", "high"]:
        c = all_vols.get(regime, 0)
        lines.append(f"| {regime} | {c} | {c/total:.1%} |")
    lines.append("")

    # ── Experiment A: Settlement Reversion ────────────────────────────────────
    if settlement_horizons:
        lines += ["---", "", "## 3. Experiment A: Settlement Reversion", ""]
        lines.append(
            "Analysis of BTC mean reversion following Polymarket settlement "
            "when YES price remains near 50±5 while BTC has moved directionally."
        )
        lines.append("")
        lines.append("| Horizon | N | Win Rate | Mean Return | Median | Expectancy |")
        lines.append("|---|---|---|---|---|---|")
        for h_label in ["15s", "30s", "60s"]:
            h_data = settlement_horizons.get(h_label, {})
            lines.append(
                f"| {h_label} | {h_data.get('n', 0)} "
                f"| {_fmt_pct(h_data.get('win_rate'))} "
                f"| {_fmt_pct(h_data.get('mean_return'))} "
                f"| {_fmt_pct(h_data.get('median_return'))} "
                f"| {_fmt_pct(h_data.get('expectancy'))} |"
            )
        lines.append("")

    # ── Experiment B: Poly Price Lag ──────────────────────────────────────────
    if lag_stats.get("n", 0) > 0:
        lines += ["---", "", "## 4. Experiment B: Poly Price Lag", ""]
        lines.append("Measures whether Polymarket YES price changes lead OKX BTC moves.")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.append(f"| Observations | {lag_stats['n']} |")
        lines.append(f"| Poly leads OKX | {lag_stats.get('poly_leads_count', 0)} ({lag_stats.get('poly_leads_pct', 0):.1%}) |")
        lines.append(f"| OKX leads Poly | {lag_stats.get('okx_leads_count', 0)} |")
        lines.append(f"| Mean lead (ms) | {_fmt_ms(lag_stats.get('mean_lead_ms'))} |")
        lines.append(f"| Median lead (ms) | {_fmt_ms(lag_stats.get('median_lead_ms'))} |")
        lines.append(f"| P25 lead (ms) | {_fmt_ms(lag_stats.get('p25_lead_ms'))} |")
        lines.append(f"| P75 lead (ms) | {_fmt_ms(lag_stats.get('p75_lead_ms'))} |")
        lines.append("")

    # ── Experiment C: Spread Distortion ───────────────────────────────────────
    if spread_stats.get("n", 0) > 0:
        lines += ["---", "", "## 5. Experiment C: Spread Distortion", ""]
        lines.append("Observations of abnormal spread widening or liquidity collapse near settlement.")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.append(f"| Observations | {spread_stats['n']} |")
        lines.append(f"| Mean spread change | {spread_stats.get('mean_spread_change_pct', 0):.1f}% |")
        lines.append(f"| Mean liquidity change | {spread_stats.get('mean_liquidity_change_pct', 0):.1f}% |")
        btc_sub = spread_stats.get("btc_subsequent_stats", {})
        lines.append(f"| BTC subsequent win_rate | {_fmt_pct(btc_sub.get('win_rate'))} |")
        lines.append(f"| BTC subsequent mean | {_fmt_pct(btc_sub.get('mean'))} |")
        lines.append("")

    # ── News Events ───────────────────────────────────────────────────────────
    if news_stats.get("n", 0) > 0:
        lines += ["---", "", "## 6. News/Event Analysis (Phase 2)", ""]
        lines.append(f"Total news events captured: **{news_stats['n']}**")
        lines.append("")
        lines.append("| Event Type | Count | BTC 60s Win Rate | BTC 60s Mean |")
        lines.append("|---|---|---|---|")
        for etype, estats in news_stats.get("by_type", {}).items():
            btc = estats.get("btc_60s_stats", {})
            lines.append(
                f"| {etype} | {estats.get('count', 0)} "
                f"| {_fmt_pct(btc.get('win_rate'))} "
                f"| {_fmt_pct(btc.get('mean'))} |"
            )
        lines.append("")
        lines.append("| Sentiment | Count | BTC 60s Win Rate | BTC 60s Mean |")
        lines.append("|---|---|---|---|")
        for sent, sstats in news_stats.get("by_sentiment", {}).items():
            btc = sstats.get("btc_60s_stats", {})
            lines.append(
                f"| {sent} | {sstats.get('count', 0)} "
                f"| {_fmt_pct(btc.get('win_rate'))} "
                f"| {_fmt_pct(btc.get('mean'))} |"
            )
        lines.append("")

    # ── Top / Worst Conditions ────────────────────────────────────────────────
    lines += ["---", "", "## 7. Condition Ranking", ""]
    for summ in summaries:
        if summ.top_conditions or summ.worst_conditions:
            lines.append(f"### {summ.experiment.value}")
            lines.append("")
            if summ.top_conditions:
                lines.append("**Top Conditions:**")
                lines.append("")
                lines.append("| Phase | Regime | N | Win Rate | Mean | Expectancy |")
                lines.append("|---|---|---|---|---|---|")
                for c in summ.top_conditions:
                    lines.append(
                        f"| {c['phase']} | {c['regime']} | {c['count']} "
                        f"| {_fmt_pct(c.get('win_rate'))} "
                        f"| {_fmt_pct(c.get('mean_return'))} "
                        f"| {_fmt_pct(c.get('expectancy'))} |"
                    )
                lines.append("")
            if summ.worst_conditions:
                lines.append("**Worst Conditions:**")
                lines.append("")
                lines.append("| Phase | Regime | N | Win Rate | Mean | Expectancy |")
                lines.append("|---|---|---|---|---|---|")
                for c in summ.worst_conditions:
                    lines.append(
                        f"| {c['phase']} | {c['regime']} | {c['count']} "
                        f"| {_fmt_pct(c.get('win_rate'))} "
                        f"| {_fmt_pct(c.get('mean_return'))} "
                        f"| {_fmt_pct(c.get('expectancy'))} |"
                    )
                lines.append("")

    # ── GO / NO-GO Gate ───────────────────────────────────────────────────────
    lines += ["---", "", "## 8. GO / NO-GO Gate", ""]
    gate = go_nogo_check(summaries, signals)
    lines.append(f"### Verdict: **{gate['verdict']}**")
    lines.append("")
    lines.append(f"**Reason:** {gate['reason']}")
    lines.append("")

    lines.append("| Criterion | Result | Value | Threshold |")
    lines.append("|---|---|---|---|")
    for cname, cdata in gate.get("criteria", {}).items():
        if cname == "c5_min_2_sessions" and gate.get("n_sessions", 1) < 2:
            continue
        met_icon = "✅" if cdata["met"] else "❌"
        val = cdata.get("value", "N/A")
        if isinstance(val, float) and val is not None:
            val = f"{val:.6f}"
        elif val is None:
            val = "N/A"
        lines.append(f"| {cname} | {met_icon} | {val} | {cdata['threshold']} |")
    lines.append("")

    baseline = gate.get("baseline", {})
    if baseline.get("baseline_p95_win_rate") is not None:
        lines.append("### Randomized Baseline (bootstrap)")
        lines.append("")
        lines.append(f"- Mean win rate: {baseline['baseline_win_rate']:.4%}" if baseline.get("baseline_win_rate") else "- Mean win rate: N/A")
        lines.append(f"- P95 win rate: **{baseline['baseline_p95_win_rate']:.4%}**" if baseline.get("baseline_p95_win_rate") else "- P95 win rate: N/A")
        lines.append(f"- Mean expectancy: {baseline.get('baseline_expectancy', 0):.6f}" if baseline.get("baseline_expectancy") is not None else "- Mean expectancy: N/A")
        lines.append(f"- Pool: {baseline.get('n_pool', 0)} returns | {baseline.get('method', '')}")
        lines.append("")

    if gate.get("n_sessions", 1) < 2:
        lines.append("> ⚠️ **Multi-session**: ≥2 independent sessions required before paper execution.")
        lines.append("")

    # ── Conclusion ────────────────────────────────────────────────────────────
    lines += ["---", "", "## 9. Conclusion", ""]
    lines.append(
        "_This report is auto-generated for alpha research purposes only. "
        "No real orders were placed. All statistics are computed from "
        "paper-simulated signal tracking._"
    )
    lines.append("")

    md = "\n".join(lines)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(md, encoding="utf-8")
    return md