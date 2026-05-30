"""
vps_deep_inspect.py — read-only deep inspection for 24h shadow validation report.
Run on VPS: python3 research/vps_deep_inspect.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from collections import Counter

SIGNAL_PATH = "research/paper_anchor_signal_events.jsonl"
SHADOW_PATH = "research/shadow_execution_events.jsonl"


def load_jsonl(path: str) -> list[dict]:
    items = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError:
        pass
    return items


def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    # ── Signals ──
    signals = load_jsonl(SIGNAL_PATH)
    total_sig = len(signals)
    started = [s for s in signals if s.get("event_type") == "signal_started"]
    started_24h = []
    for s in started:
        ts_str = s.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts >= cutoff:
                started_24h.append(s)
        except ValueError:
            pass

    dists = [s.get("dist", 0) for s in started_24h]
    d130 = sum(1 for d in dists if d >= 130)
    d150 = sum(1 for d in dists if d >= 150)
    d180 = sum(1 for d in dists if d >= 180)

    high = sorted(
        [s for s in started_24h if s.get("dist", 0) >= 130],
        key=lambda x: -x.get("dist", 0),
    )[:10]

    print(f"SIGNAL_TOTAL_LINES={total_sig}")
    print(f"SIGNAL_24H_STARTED={len(started_24h)}")
    print(f"DIST_GE130={d130}")
    print(f"DIST_GE150={d150}")
    print(f"DIST_GE180={d180}")
    print("===HIGH_DIST_TOP10===")
    for s in high:
        keys = ["ts", "slug", "direction", "dist", "checkpoint", "btc_live", "market_end_ts", "checkpoint_offset_s"]
        summary = {k: s[k] for k in keys if k in s}
        # remaining seconds: market_end_ts - (event_start_ts + checkpoint_offset_s)
        me = s.get("market_end_ts", 0)
        es = s.get("event_start_ts", me)
        offset = s.get("checkpoint_offset_s", 0)
        summary["remaining_sec_till_end"] = max(0, me - es - offset)
        print(json.dumps(summary, default=str))

    # ── Shadow ──
    shadow = load_jsonl(SHADOW_PATH)
    total_sh = len(shadow)
    clob_ok = sum(1 for e in shadow if e.get("clob_orderbook_available") is True)
    fb_used = sum(1 for e in shadow if e.get("fallback_used") is True)
    missing_token = sum(1 for e in shadow if e.get("reject_reason") == "missing_clob_token_id")
    bff = sum(1 for e in shadow if e.get("reject_reason") == "book_fetch_failed")

    exec_10 = sum(1 for e in shadow if e.get("executable_10") is True)
    exec_25 = sum(1 for e in shadow if e.get("executable_25") is True)
    exec_50 = sum(1 for e in shadow if e.get("executable_50") is True)

    latencies = []
    for e in shadow:
        v = e.get("clob_fetch_latency_ms")
        if v is not None and v > 0:
            latencies.append(v)
    latencies.sort()

    def pct(lst, p):
        if not lst:
            return 0
        idx = int(len(lst) * p / 100)
        return lst[min(idx, len(lst) - 1)]

    print(f"SHADOW_TOTAL_LINES={total_sh}")
    print(f"SHADOW_CLOB_OK={clob_ok}")
    print(f"SHADOW_FALLBACK_USED={fb_used}")
    print(f"SHADOW_MISSING_TOKEN={missing_token}")
    print(f"SHADOW_BOOK_FETCH_FAILED={bff}")
    print(f"SHADOW_EXECUTABLE_10={exec_10}")
    print(f"SHADOW_EXECUTABLE_25={exec_25}")
    print(f"SHADOW_EXECUTABLE_50={exec_50}")
    print(f"BOOK_LATENCY_MIN={pct(latencies, 0)}")
    print(f"BOOK_LATENCY_P50={pct(latencies, 50)}")
    print(f"BOOK_LATENCY_P95={pct(latencies, 95)}")
    print(f"BOOK_LATENCY_MAX={pct(latencies, 100)}")

    # ── Near-expiry: high-dist samples remaining time ──
    print("===NEAR_EXPIRY===")
    rem_times = []
    for s in started_24h:
        if s.get("dist", 0) >= 130:
            me = s.get("market_end_ts", 0)
            es = s.get("event_start_ts", me)
            offset = s.get("checkpoint_offset_s", 0)
            remaining = max(0, me - es - offset)
            rem_times.append(remaining)
    if rem_times:
        rem_sorted = sorted(rem_times)
        print(f"NEAR_EXPIRY_COUNT={len(rem_sorted)}")
        print(f"NEAR_EXPIRY_MIN_S={min(rem_sorted)}")
        print(f"NEAR_EXPIRY_P25_S={pct(rem_sorted, 25)}")
        print(f"NEAR_EXPIRY_P50_S={pct(rem_sorted, 50)}")
        print(f"NEAR_EXPIRY_P75_S={pct(rem_sorted, 75)}")
        print(f"NEAR_EXPIRY_MAX_S={max(rem_sorted)}")
        # bucket: <60s, 60-120s, 120-180s, 180-300s
        lt60 = sum(1 for r in rem_sorted if r < 60)
        lt120 = sum(1 for r in rem_sorted if 60 <= r < 120)
        lt180 = sum(1 for r in rem_sorted if 120 <= r < 180)
        lt300 = sum(1 for r in rem_sorted if 180 <= r < 300)
        gt300 = sum(1 for r in rem_sorted if r >= 300)
        print(f"NEAR_EXPIRY_BUCKET_lt60s={lt60}")
        print(f"NEAR_EXPIRY_BUCKET_60_120s={lt120}")
        print(f"NEAR_EXPIRY_BUCKET_120_180s={lt180}")
        print(f"NEAR_EXPIRY_BUCKET_180_300s={lt300}")
        print(f"NEAR_EXPIRY_BUCKET_gt300s={gt300}")


if __name__ == "__main__":
    main()