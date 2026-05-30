"""Offline tests for SHADOW_ONLY execution feasibility reporting."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from research import shadow_execution_recorder as shadow


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def test_shadow_recorder_generates_events_and_report_with_missing_fields(tmp_path, monkeypatch):
    signals_path = tmp_path / "research" / "paper_anchor_signals.jsonl"
    events_path = tmp_path / "research" / "shadow_execution_events.jsonl"
    report_path = tmp_path / "research" / "shadow_execution_report.md"
    _write_jsonl(
        signals_path,
        [
            {
                "slug": "btc-updown-5m-1",
                "event_start_ts": 1767225600,
                "end_ts": 1767225900,
                "resolved": True,
                "outcome": "UP",
                "checkpoints": [
                    {
                        "offset_s": 120,
                        "ts_utc": "2026-01-01T00:02:00Z",
                        "btc_live": 101.0,
                        "distance": 135.0,
                        "direction": "UP",
                        "triggered": True,
                        "poly_bid": 0.49,
                        "poly_ask": 0.51,
                        "poly_spread": 0.02,
                        "poly_liquidity": 100.0,
                    }
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
                        "offset_s": 180,
                        "ts_utc": "2026-01-01T00:08:00Z",
                        "btc_live": 99.0,
                        "distance": 151.0,
                        "direction": "DOWN",
                        "triggered": True,
                        # Deliberately missing bid/ask/liquidity.
                    }
                ],
            },
        ],
    )

    monkeypatch.setattr(shadow, "resolve_clob_token_ids", lambda slug: (None, None, None))
    monkeypatch.setattr(sys, "argv", [
        "shadow_execution_recorder.py",
        "--threshold",
        "130",
        "--notional-usdc",
        "10",
        "--signals-path",
        str(signals_path),
        "--events-path",
        str(events_path),
        "--report-path",
        str(report_path),
    ])
    shadow.main()

    event_lines = events_path.read_text(encoding="utf-8").splitlines()
    assert len(event_lines) == 2
    first_event = json.loads(event_lines[0])
    second_event = json.loads(event_lines[1])
    assert first_event["SHADOW_ONLY"] is True
    assert first_event["clob_orderbook_available"] is False
    assert first_event["fallback_used"] is True
    assert first_event["estimated_fill_price_10"] == 0.51
    assert first_event["shadow_adjusted_pnl_10"] is not None
    assert second_event["SHADOW_ONLY"] is True
    assert second_event["reject_reason"] == "missing_clob_token_id"

    report = report_path.read_text(encoding="utf-8")
    assert "SHADOW_ONLY=true" in report
    assert "Total shadow candidates" in report
    assert "Worst-case Execution Scenario" in report
    assert "Edge Survival Analysis" in report
    assert "Near-expiry Distribution" in report
    assert "Hold Duration Distribution" in report
    assert "WARNING: edge may be near-expiry dominated" in report
    assert "No trading API calls are made" in report
    assert "fallback_used=true" in report


def test_follow_mode_writes_real_clob_shadow_event(tmp_path):
    signals_path = tmp_path / "research" / "paper_anchor_signal_events.jsonl"
    events_path = tmp_path / "research" / "shadow_execution_events.jsonl"
    report_path = tmp_path / "research" / "shadow_execution_report.md"
    fresh = {
        "event_type": "signal_started",
        "ts": "2026-01-01T00:02:00Z",
        "slug": "btc-updown-5m-new",
        "event_start_ts": 1767225600,
        "market_end_ts": 1767225900,
        "checkpoint_offset_s": 120,
        "btc_live": 101.0,
        "dist": 135.0,
        "direction": "UP",
        "poly_bid": 0.49,
        "poly_ask": 0.51,
        "poly_liquidity": 100.0,
    }
    _write_jsonl(signals_path, [fresh])

    def fake_resolver(slug: str):
        assert slug == "btc-updown-5m-new"
        return "YES_TOKEN", "NO_TOKEN", {"slug": slug}

    def fake_fetcher(token_id: str):
        assert token_id == "YES_TOKEN"
        return (
            {
                "bids": [{"price": "0.50", "size": "100"}],
                "asks": [
                    {"price": "0.51", "size": "20"},
                    {"price": "0.52", "size": "30"},
                    {"price": "0.53", "size": "50"},
                ],
            },
            8.0,
            None,
        )

    processed = shadow.follow_signals(
        threshold=130,
        notional_usdc=10,
        signals_path=signals_path,
        events_path=events_path,
        report_path=report_path,
        poll_interval_sec=0.01,
        max_events=1,
        since_end=False,
        orderbook_fetcher=fake_fetcher,
        token_resolver=fake_resolver,
    )
    assert processed == 1
    event_lines = events_path.read_text(encoding="utf-8").splitlines()
    assert len(event_lines) == 1
    event = json.loads(event_lines[0])
    assert event["SHADOW_ONLY"] is True
    assert event["slug"] == "btc-updown-5m-new"
    assert event["clob_token_id"] == "YES_TOKEN"
    assert event["clob_orderbook_available"] is True
    assert event["fallback_used"] is False
    assert event["executable_10"] is True
    assert event["executable_25"] is True
    assert event["executable_50"] is True
    report = report_path.read_text(encoding="utf-8")
    assert "real_clob_book_available_rate | 100.0%" in report
    assert "fallback_used_rate | 0.0%" in report


def test_follow_mode_falls_back_when_resolver_fails(tmp_path):
    signals_path = tmp_path / "research" / "paper_anchor_signal_events.jsonl"
    events_path = tmp_path / "research" / "shadow_execution_events.jsonl"
    report_path = tmp_path / "research" / "shadow_execution_report.md"
    row = {
        "event_type": "signal_started",
        "ts": "2026-01-01T00:02:00Z",
        "slug": "btc-updown-5m-new",
        "event_start_ts": 1767225600,
        "market_end_ts": 1767225900,
        "checkpoint_offset_s": 120,
        "btc_live": 101.0,
        "dist": 135.0,
        "direction": "UP",
        "poly_bid": 0.49,
        "poly_ask": 0.51,
        "poly_liquidity": 100.0,
    }
    _write_jsonl(signals_path, [row])
    processed = shadow.follow_signals(
        threshold=130,
        notional_usdc=10,
        signals_path=signals_path,
        events_path=events_path,
        report_path=report_path,
        poll_interval_sec=0.01,
        max_events=1,
        since_end=False,
        token_resolver=lambda slug: (None, None, None),
    )
    assert processed == 1
    event = json.loads(events_path.read_text(encoding="utf-8").splitlines()[0])
    assert event["SHADOW_ONLY"] is True
    assert event["fallback_used"] is True
    assert event["reject_reason"] == "token_resolve_failed"


def test_shadow_recorder_computes_mock_clob_depth_and_fill_prices(tmp_path):
    signals_path = tmp_path / "research" / "paper_anchor_signals.jsonl"
    _write_jsonl(
        signals_path,
        [
            {
                "slug": "btc-updown-5m-1",
                "event_start_ts": 1767225600,
                "end_ts": 1767225900,
                "clobTokenIds": json.dumps(["YES_TOKEN", "NO_TOKEN"]),
                "resolved": True,
                "outcome": "UP",
                "checkpoints": [
                    {
                        "offset_s": 120,
                        "ts_utc": "2026-01-01T00:02:00Z",
                        "btc_live": 101.0,
                        "distance": 135.0,
                        "direction": "UP",
                        "triggered": True,
                        "poly_bid": 0.49,
                        "poly_ask": 0.51,
                        "poly_liquidity": 100.0,
                    }
                ],
            },
        ],
    )

    def fake_fetcher(token_id: str):
        assert token_id == "YES_TOKEN"
        return (
            {
                "bids": [{"price": "0.50", "size": "100"}],
                "asks": [
                    {"price": "0.51", "size": "20"},
                    {"price": "0.52", "size": "30"},
                    {"price": "0.53", "size": "40"},
                    {"price": "0.54", "size": "50"},
                    {"price": "0.55", "size": "60"},
                ],
            },
            12.5,
            None,
        )

    events = shadow.build_shadow_events(130, 10, signals_path, orderbook_fetcher=fake_fetcher)
    assert len(events) == 1
    event = events[0]
    assert event.SHADOW_ONLY is True
    assert event.clob_orderbook_available is True
    assert event.clob_fetch_latency_ms == 12.5
    assert event.best_bid == 0.50
    assert event.best_ask == 0.51
    assert event.bid_levels_count == 1
    assert event.ask_levels_count == 5
    assert event.side_specific_depth_top1 == 10.2
    assert event.side_specific_depth_top3 == 47.0
    assert event.side_specific_depth_top5 == 107.0
    assert event.executable_10 is True
    assert event.executable_25 is True
    assert event.executable_50 is True
    assert event.estimated_fill_price_10 == 0.51
    assert event.estimated_fill_price_50 is not None
    assert event.slippage_bps_10 is not None


def test_shadow_recorder_resolves_slug_tokens_and_selects_side_specific_token(tmp_path):
    signals_path = tmp_path / "research" / "paper_anchor_signals.jsonl"
    _write_jsonl(
        signals_path,
        [
            {
                "slug": "btc-updown-5m-1",
                "event_start_ts": 1767225600,
                "end_ts": 1767225900,
                "resolved": True,
                "outcome": "DOWN",
                "checkpoints": [
                    {
                        "offset_s": 120,
                        "ts_utc": "2026-01-01T00:02:00Z",
                        "btc_live": 99.0,
                        "distance": 140.0,
                        "direction": "DOWN",
                        "triggered": True,
                        "poly_bid": 0.49,
                        "poly_ask": 0.51,
                        "poly_liquidity": 100.0,
                    }
                ],
            },
        ],
    )
    fetched_tokens = []

    def fake_resolver(slug: str):
        assert slug == "btc-updown-5m-1"
        return "YES_TOKEN", "NO_TOKEN", {"slug": slug}

    def fake_fetcher(token_id: str):
        fetched_tokens.append(token_id)
        return (
            {
                "bids": [{"price": "0.49", "size": "100"}],
                "asks": [{"price": "0.50", "size": "100"}],
            },
            7.0,
            None,
        )

    events = shadow.build_shadow_events(
        130,
        10,
        signals_path,
        orderbook_fetcher=fake_fetcher,
        token_resolver=fake_resolver,
    )
    assert fetched_tokens == ["NO_TOKEN"]
    assert len(events) == 1
    assert events[0].clob_token_id == "NO_TOKEN"
    assert events[0].fallback_used is False
    assert events[0].clob_orderbook_available is True


def test_resolve_clob_token_ids_uses_read_only_market_response(monkeypatch):
    shadow._TOKEN_RESOLVE_CACHE.clear()

    def fake_http_getter(url: str, params: dict):
        assert url.endswith("/events/slug/btc-updown-5m-1")
        assert params == {}
        return (
            {
                "slug": "btc-updown-5m-1",
                "markets": [
                    {
                        "slug": "btc-updown-5m-1",
                        "clobTokenIds": json.dumps(["YES_TOKEN", "NO_TOKEN"]),
                    }
                ],
            },
            1.0,
            None,
        )

    yes, no, raw = shadow.resolve_clob_token_ids("btc-updown-5m-1", http_getter=fake_http_getter)
    assert yes == "YES_TOKEN"
    assert no == "NO_TOKEN"
    assert raw["slug"] == "btc-updown-5m-1"


def test_failed_token_resolve_is_not_permanently_cached():
    shadow._TOKEN_RESOLVE_CACHE.clear()
    calls = {"count": 0}

    def failing_http_getter(url: str, params: dict):
        calls["count"] += 1
        return None, 1.0, "temporary network failure"

    yes, no, raw = shadow.resolve_clob_token_ids(
        "btc-updown-5m-1",
        http_getter=failing_http_getter,
        retries=1,
    )
    assert (yes, no, raw) == (None, None, None)
    assert calls["count"] > 0
    assert "btc-updown-5m-1" not in shadow._TOKEN_RESOLVE_CACHE

    def successful_http_getter(url: str, params: dict):
        calls["count"] += 1
        return (
            {
                "slug": "btc-updown-5m-1",
                "markets": [
                    {
                        "slug": "btc-updown-5m-1",
                        "clobTokenIds": json.dumps(["YES_TOKEN", "NO_TOKEN"]),
                    }
                ],
            },
            1.0,
            None,
        )

    yes, no, raw = shadow.resolve_clob_token_ids(
        "btc-updown-5m-1",
        http_getter=successful_http_getter,
        retries=1,
    )
    assert yes == "YES_TOKEN"
    assert no == "NO_TOKEN"
    assert raw["slug"] == "btc-updown-5m-1"


def test_successful_token_resolve_is_cached():
    shadow._TOKEN_RESOLVE_CACHE.clear()
    calls = {"count": 0}

    def successful_http_getter(url: str, params: dict):
        calls["count"] += 1
        return (
            {
                "slug": "btc-updown-5m-1",
                "markets": [
                    {
                        "slug": "btc-updown-5m-1",
                        "clobTokenIds": json.dumps(["YES_TOKEN", "NO_TOKEN"]),
                    }
                ],
            },
            1.0,
            None,
        )

    assert shadow.resolve_clob_token_ids("btc-updown-5m-1", http_getter=successful_http_getter)[:2] == (
        "YES_TOKEN",
        "NO_TOKEN",
    )
    assert shadow.resolve_clob_token_ids("btc-updown-5m-1", http_getter=successful_http_getter)[:2] == (
        "YES_TOKEN",
        "NO_TOKEN",
    )
    assert calls["count"] == 1


def test_http_json_does_not_append_question_mark_for_empty_params(monkeypatch):
    requested_urls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout):
        requested_urls.append(req.full_url)
        return FakeResponse()

    monkeypatch.setattr(shadow.urllib.request, "urlopen", fake_urlopen)
    data, _, err = shadow._http_json("https://example.test/events/slug/test", {})
    assert data == {"ok": True}
    assert err is None
    assert requested_urls == ["https://example.test/events/slug/test"]


def test_token_parser_handles_json_string_clob_token_ids():
    yes, no = shadow._tokens_from_market(
        {
            "slug": "btc-updown-5m-1",
            "outcomes": json.dumps(["Up", "Down"]),
            "clobTokenIds": json.dumps(["UP_TOKEN", "DOWN_TOKEN"]),
        }
    )
    assert yes == "UP_TOKEN"
    assert no == "DOWN_TOKEN"


def test_token_parser_handles_list_clob_token_ids():
    yes, no = shadow._tokens_from_market(
        {
            "slug": "btc-updown-5m-1",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["YES_TOKEN", "NO_TOKEN"],
        }
    )
    assert yes == "YES_TOKEN"
    assert no == "NO_TOKEN"


def test_token_parser_handles_tokens_array():
    yes, no = shadow._tokens_from_market(
        {
            "slug": "btc-updown-5m-1",
            "tokens": [
                {"outcome": "Yes", "token_id": "YES_TOKEN"},
                {"outcome": "No", "token_id": "NO_TOKEN"},
            ],
        }
    )
    assert yes == "YES_TOKEN"
    assert no == "NO_TOKEN"


def test_token_parser_uses_outcomes_when_order_reversed():
    yes, no = shadow._tokens_from_market(
        {
            "slug": "btc-updown-5m-1",
            "outcomes": json.dumps(["Down", "Up"]),
            "clobTokenIds": json.dumps(["DOWN_TOKEN", "UP_TOKEN"]),
        }
    )
    assert yes == "UP_TOKEN"
    assert no == "DOWN_TOKEN"


def test_missing_token_fields_produce_missing_clob_token_id(tmp_path):
    signals_path = tmp_path / "research" / "paper_anchor_signals.jsonl"
    _write_jsonl(
        signals_path,
        [
            {
                "slug": "btc-updown-5m-1",
                "event_start_ts": 1767225600,
                "end_ts": 1767225900,
                "resolved": True,
                "outcome": "UP",
                "checkpoints": [
                        {
                            "offset_s": 120,
                            "ts_utc": "2026-01-01T00:02:00Z",
                            "btc_live": 101.0,
                            "distance": 135.0,
                            "direction": "UP",
                        "triggered": True,
                        "poly_bid": 0.49,
                        "poly_ask": 0.51,
                        "poly_liquidity": 100.0,
                    }
                ],
            },
        ],
    )
    events = shadow.build_shadow_events(
        130,
        10,
        signals_path,
        token_resolver=lambda slug: (None, None, None),
    )
    assert len(events) == 1
    assert events[0].reject_reason == "missing_clob_token_id"


def test_fresh_signal_started_event_fetches_real_book():
    raw = {
        "event_type": "signal_started",
        "ts": "2026-01-01T00:02:00Z",
        "slug": "btc-updown-5m-1",
        "direction": "UP",
        "dist": 135.0,
        "checkpoint": "T+120",
        "checkpoint_offset_s": 120,
        "event_start_ts": 1767225600,
        "market_end_ts": 1767225900,
        "poly_bid": 0.49,
        "poly_ask": 0.51,
        "poly_spread": 0.02,
        "poly_liquidity": 100.0,
        "anchor_price": 100.0,
        "btc_live": 101.0,
    }

    fetched = []

    def fake_fetcher(token_id: str):
        fetched.append(token_id)
        return (
            {
                "bids": [{"price": "0.50", "size": "100"}],
                "asks": [{"price": "0.51", "size": "100"}],
            },
            5.0,
            None,
        )

    events = shadow._events_from_follow_line(
        raw,
        130,
        10,
        orderbook_fetcher=fake_fetcher,
        token_resolver=lambda slug: ("YES_TOKEN", "NO_TOKEN", {"slug": slug}),
    )
    assert fetched == ["YES_TOKEN"]
    assert len(events) == 1
    event = events[0]
    assert event.clob_token_id == "YES_TOKEN"
    assert event.clob_orderbook_available is True
    assert event.fallback_used is False
    assert event.reject_reason == "fresh_real_book_ok"
    assert event.remaining_time_sec == 180.0
    assert event.remaining_bucket == "T-300~120"
    assert event.latency_spike_gt_1000ms is False


def test_resolved_record_is_not_treated_as_fresh_executable():
    raw = {
        "slug": "btc-updown-5m-1",
        "event_start_ts": 1767225600,
        "end_ts": 1767225900,
        "resolved": True,
        "outcome": "UP",
        "checkpoints": [
            {
                "offset_s": 120,
                "ts_utc": "2026-01-01T00:02:00Z",
                "btc_live": 101.0,
                "distance": 135.0,
                "direction": "UP",
                "triggered": True,
            }
        ],
    }

    def fail_fetcher(token_id: str):
        raise AssertionError("resolved records must not fetch fresh books")

    events = shadow._events_from_follow_line(
        raw,
        130,
        10,
        orderbook_fetcher=fail_fetcher,
        token_resolver=lambda slug: ("YES_TOKEN", "NO_TOKEN", {"slug": slug}),
    )
    assert events == []


def test_fresh_book_404_is_marked():
    raw = {
        "event_type": "signal_started",
        "ts": "2026-01-01T00:02:00Z",
        "slug": "btc-updown-5m-1",
        "direction": "DOWN",
        "dist": 151.0,
        "checkpoint_offset_s": 120,
        "event_start_ts": 1767225600,
        "market_end_ts": 1767225900,
        "poly_bid": 0.49,
        "poly_ask": 0.51,
        "poly_liquidity": 100.0,
    }
    events = shadow._events_from_follow_line(
        raw,
        130,
        10,
        orderbook_fetcher=lambda token_id: (None, 12.0, "HTTP Error 404: Not Found"),
        token_resolver=lambda slug: ("YES_TOKEN", "NO_TOKEN", {"slug": slug}),
    )
    assert len(events) == 1
    assert events[0].clob_token_id == "NO_TOKEN"
    assert events[0].fallback_used is True
    assert events[0].reject_reason == "fresh_book_404"


def test_fresh_token_resolve_failed_is_marked():
    raw = {
        "event_type": "signal_started",
        "ts": "2026-01-01T00:02:00Z",
        "slug": "btc-updown-5m-1",
        "direction": "UP",
        "dist": 151.0,
        "checkpoint_offset_s": 120,
        "event_start_ts": 1767225600,
        "market_end_ts": 1767225900,
    }
    events = shadow._events_from_follow_line(
        raw,
        130,
        10,
        token_resolver=lambda slug: (None, None, None),
    )
    assert len(events) == 1
    assert events[0].reject_reason == "token_resolve_failed"


def test_latency_tail_reports_p99_max_and_spike_duration(tmp_path):
    events = []
    latencies = iter([300.0, 1100.0, 1200.0, 1300.0])

    def fake_fetcher(token_id: str):
        return (
            {
                "bids": [{"price": "0.50", "size": "100"}],
                "asks": [{"price": "0.51", "size": "100"}],
            },
            next(latencies),
            None,
        )

    for ts in ["2026-01-01T00:00:00Z", "2026-01-01T00:00:02Z", "2026-01-01T00:00:05Z", "2026-01-01T00:00:20Z"]:
        events.extend(
            shadow._events_from_follow_line(
                {
                    "event_type": "signal_started",
                    "ts": ts,
                    "slug": "btc-updown-5m-1",
                    "direction": "UP",
                    "dist": 151.0,
                    "checkpoint_offset_s": 120,
                    "event_start_ts": 1767225600,
                    "market_end_ts": 1767225900,
                    "poly_bid": 0.49,
                    "poly_ask": 0.51,
                    "poly_liquidity": 100.0,
                },
                130,
                10,
                orderbook_fetcher=fake_fetcher,
                token_resolver=lambda slug: ("YES_TOKEN", "NO_TOKEN", {"slug": slug}),
            )
        )

    tail = shadow._latency_tail(events)
    assert tail["latency_p99_ms"] == 1300.0
    assert tail["latency_max_ms"] == 1300.0
    assert tail["spikes_gt_1000ms_count"] == 3
    assert tail["longest_spike_duration_sec"] == 3.0
    assert events[1].latency_spike_gt_1000ms is True

    report = shadow.generate_report(
        130,
        10,
        tmp_path / "missing.jsonl",
        selected_events=events,
        all_events=events,
        max_online_resolves=0,
    )
    assert "Execution Realism Latency Tail" in report
    assert "latency_p99_ms" in report
    assert "spikes_gt_1000ms_count" in report
    assert "longest_spike_duration_sec" in report


def test_near_expiry_bucket_aggregation_and_statuses(tmp_path):
    offsets = [
        (90, 210.0, "T-300~120"),
        (180, 120.0, "T-120~60"),
        (245, 55.0, "T-60~30"),
        (280, 20.0, "T-30~0"),
    ]
    events = []

    def fake_fetcher(token_id: str):
        return (
            {
                "bids": [{"price": "0.50", "size": "100"}],
                "asks": [{"price": "0.51", "size": "100"}],
            },
            300.0,
            None,
        )

    for offset, expected_remaining, expected_bucket in offsets:
        minute = offset // 60
        second = offset % 60
        generated = shadow._events_from_follow_line(
            {
                "event_type": "signal_started",
                "ts": f"2026-01-01T00:{minute:02d}:{second:02d}Z",
                "slug": f"btc-updown-5m-{offset}",
                "direction": "UP",
                "dist": 151.0,
                "checkpoint_offset_s": offset,
                "event_start_ts": 1767225600,
                "market_end_ts": 1767225900,
                "poly_bid": 0.49,
                "poly_ask": 0.51,
                "poly_liquidity": 100.0,
            },
            130,
            10,
            orderbook_fetcher=fake_fetcher,
            token_resolver=lambda slug: ("YES_TOKEN", "NO_TOKEN", {"slug": slug}),
        )
        assert len(generated) == 1
        assert generated[0].remaining_time_sec == expected_remaining
        assert generated[0].remaining_bucket == expected_bucket
        events.extend(generated)

    rows = shadow._near_expiry_bucket_rows(events)
    assert [row["bucket"] for row in rows] == ["T-300~120", "T-120~60", "T-60~30", "T-30~0"]
    assert all(row["n"] == 1 for row in rows)
    assert all(row["status"] == "WARNING" for row in rows)  # N < 50 is intentionally warning.

    report = shadow.generate_report(
        130,
        10,
        tmp_path / "missing.jsonl",
        selected_events=events,
        all_events=events,
        max_online_resolves=0,
    )
    assert "Near-expiry Bucket Aggregation" in report
    assert "T-300~120" in report
    assert "T-120~60" in report
    assert "T-60~30" in report
    assert "T-30~0" in report
    assert "HEALTHY" in report or "WARNING" in report or "NO-TRADE" in report


def test_shadow_recorder_does_not_import_network_or_trading_clients():
    source = Path(shadow.__file__).read_text(encoding="utf-8")
    banned_tokens = [
        "aiohttp",
        "requests",
        "create_order",
        "place_order",
        "private_key",
        "ALLOW_REAL_TRADING",
    ]
    assert not any(token in source for token in banned_tokens)
