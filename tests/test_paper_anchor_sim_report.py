"""Offline tests for paper_anchor_sim report generation."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from research import paper_anchor_sim as sim


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_report_adds_threshold_stats_and_handles_missing_fields(tmp_path, monkeypatch):
    signals_path = tmp_path / "research" / "paper_anchor_signals.jsonl"
    report_path = tmp_path / "research" / "paper_anchor_report.md"
    monkeypatch.setattr(sim, "SIGNALS_PATH", signals_path)
    monkeypatch.setattr(sim, "REPORT_PATH", report_path)
    sim._anchor_deltas.clear()

    _write_jsonl(
        signals_path,
        [
            {
                "slug": "btc-updown-5m-1",
                "event_start_ts": 1767225600,
                "end_ts": 1767225900,
                "binance_t_open": 101.0,
                "anchor_est": 1.0,
                "resolved": True,
                "outcome": "UP",
                "price_to_beat": 5.0,
                "checkpoints": [
                    {
                        "offset_s": 90,
                        "ts_utc": "2026-01-01T00:01:30Z",
                        "btc_live": 130.0,
                        "distance": 129.0,
                        "direction": "UP",
                        "triggered": True,
                        "poly_ask": 0.50,
                        "tradeable": True,
                    },
                    {
                        "offset_s": 120,
                        "ts_utc": "2026-01-01T00:02:00Z",
                        "btc_live": 70.0,
                        "distance": 69.0,
                        "direction": "DOWN",
                        "triggered": True,
                        "poly_bid": 0.50,
                    },
                ],
            },
            {
                "slug": "btc-updown-5m-2",
                "event_start_ts": 1767225900,
                "end_ts": 1767226200,
                "resolved": True,
                "outcome": "DOWN",
                "checkpoints": [
                    {
                        # Deliberately old/partial shape: missing optional CLOB fields.
                        "offset_s": 180,
                        "ts_utc": "2026-01-01T00:08:00Z",
                        "btc_live": 180.0,
                        "distance": 179.0,
                        "direction": "UP",
                        "triggered": True,
                    }
                ],
            },
        ],
    )

    monkeypatch.setattr(sys, "argv", ["paper_anchor_sim.py", "--report"])
    sim.main()

    report = report_path.read_text(encoding="utf-8")
    assert "## 5. Distance Threshold Performance" in report
    assert "| >=200 | 0 | N/A |" in report
    assert "| ≥ $100 |" in report
    assert "| ≥ $120 |" in report
    assert "| ≥ $130 |" in report
    assert "| ≥ $150 |" in report
    assert "### Potential Live Threshold Simulation" in report
    assert "Est Signals/Day" in report
    assert "Recovery Factor" in report
    assert "### JSONL Field Coverage" in report
    assert "market title | N/A" in report
    assert "### Remaining Time Analysis" in report
    assert "Remaining time = market end time minus checkpoint entry time." in report
    assert "#### dist ≥ $120" in report
    assert "<1h" in report
    assert "### Hold Duration Analysis" in report
    assert "PnL/Hold Pearson r" in report
    assert "### Near-expiry Risk Summary" in report
    assert "WARNING: edge may be near-expiry dominated" in report
    assert "CAUTION: live execution may be sensitive to latency/slippage" in report
    assert "### Event Type / Market Group" in report
    assert "### High Distance Hold Time and PnL Distribution" in report
    assert "Hold >72h Share" in report
    assert "PnL p90" in report
    assert "Max Drawdown is calculated from the running cumulative fee-adjusted PnL curve." in report
    assert "Pearson r" in report
    assert "Profit Factor" in report
    assert "Max drawdown" in report
    assert "100-120" in report
