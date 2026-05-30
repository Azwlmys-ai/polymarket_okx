"""
vps_health_check.py — Read-only VPS health check script.
Designed for cron/systemd-timer. No orders, no trading, no process kills.

Outputs:
  research/vps_health_report.md     — structured markdown report
  research/vps_health_events.jsonl  — append-only event log

Usage:
  python3 research/vps_health_check.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Fixed paths (relative to /opt/polymarket_okx)
# ─────────────────────────────────────────────────────────────────────────────
VPS_ROOT = Path(os.environ.get("VPS_ROOT", "/opt/polymarket_okx"))
SIGNAL_EVENTS_PATH = VPS_ROOT / "research" / "paper_anchor_signal_events.jsonl"
SHADOW_EVENTS_PATH = VPS_ROOT / "research" / "shadow_execution_events.jsonl"
REPORT_PATH = VPS_ROOT / "research" / "vps_health_report.md"
HEALTH_EVENTS_PATH = VPS_ROOT / "research" / "vps_health_events.jsonl"
SERVICE_NAME = "polymarket-okx-anchor.service"
TMUX_SESSION = "shadow_follow"
SHADOW_PROCESS_KEYWORD = "shadow_execution_recorder.py"

LOOKBACK_SHADOW_N = int(os.environ.get("HEALTH_LOOKBACK_SHADOW", "100"))
RECENT_MINUTES = int(os.environ.get("HEALTH_RECENT_MINUTES", "15"))


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HealthEvent:
    ts_utc: str
    status: str  # HEALTHY | PARTIAL | BROKEN
    service_active: bool
    service_uptime_sec: float | None
    signal_events_lines: int
    signal_events_mtime: float | None
    signal_events_size_mb: float
    shadow_events_lines: int
    shadow_events_mtime: float | None
    shadow_events_size_mb: float
    shadow_recent_total: int
    shadow_fresh_real_book_ok: int
    shadow_missing_clob_token_id: int
    shadow_book_fetch_failed: int
    shadow_fallback_used: int
    shadow_process_running: bool
    tmux_session_exists: bool
    errors_recent: int
    exceptions_recent: int
    tracebacks_recent: int
    journal_errors_last_15m: int
    latest_signal_samples: list[dict[str, Any]] = field(default_factory=list)
    latest_shadow_samples: list[dict[str, Any]] = field(default_factory=list)
    extra_warnings: list[str] = field(default_factory=list)
    # Shadow idle tracking (added for threshold-aware stale detection)
    shadow_idle_ok: bool = False
    shadow_threshold: int = 0
    shadow_idle_reason: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: float = 10.0, text: bool = True) -> tuple[str, int]:
    """Run a subprocess safely. Returns (stdout, returncode)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=text, timeout=timeout)
        return (r.stdout or "").strip(), r.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return str(exc), -1


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL file into a list of dicts. Returns [] on any error."""
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return items


def _file_info(path: Path) -> tuple[int, float | None, float]:
    """Return (line_count, mtime_epoch, size_mb)."""
    if not path.exists():
        return 0, None, 0.0
    try:
        stat = path.stat()
        mtime = stat.st_mtime
        size_mb = stat.st_size / (1024 * 1024)
        # Count lines quickly
        with path.open("r", encoding="utf-8") as f:
            line_count = sum(1 for _ in f)
        return line_count, mtime, size_mb
    except OSError:
        return 0, None, 0.0


def _mtime_delta_s(mtime: float | None) -> float | None:
    """Seconds since mtime."""
    if mtime is None:
        return None
    return time.time() - mtime


# ─────────────────────────────────────────────────────────────────────────────
# Check 1: systemd service status
# ─────────────────────────────────────────────────────────────────────────────

def check_service() -> tuple[bool, float | None]:
    """
    Returns (is_active, uptime_seconds_or_None).
    Uses 'systemctl is-active' and 'systemctl show' for uptime.
    """
    stdout, rc = _run(["systemctl", "is-active", "--quiet", SERVICE_NAME], timeout=5.0)
    is_active = (rc == 0)

    uptime_sec: float | None = None
    if is_active:
        show_out, _ = _run(
            ["systemctl", "show", SERVICE_NAME, "--property=ActiveEnterTimestampMonotonic"],
            timeout=5.0,
        )
        if show_out:
            try:
                # ActiveEnterTimestampMonotonic is in microseconds
                key, val = show_out.split("=", 1)
                monotonic_us = int(val.strip())
                # Get current monotonic time
                now_mono_out, _ = _run(
                    ["cat", "/proc/uptime"],
                    timeout=3.0,
                )
                if now_mono_out:
                    now_sec = float(now_mono_out.split()[0])
                    uptime_sec = now_sec - (monotonic_us / 1_000_000)
            except (ValueError, IndexError):
                pass

    return is_active, uptime_sec


# ─────────────────────────────────────────────────────────────────────────────
# Check 2: signal events (paper_anchor_signal_events.jsonl)
# ─────────────────────────────────────────────────────────────────────────────

def check_signal_events() -> tuple[int, float | None, float, list[dict[str, Any]]]:
    """Return (lines, mtime, size_mb, tail_3_samples)."""
    lines, mtime, size_mb = _file_info(SIGNAL_EVENTS_PATH)
    samples: list[dict[str, Any]] = []
    if mtime is not None:
        items = _parse_jsonl(SIGNAL_EVENTS_PATH)
        samples = items[-3:] if len(items) >= 3 else items
    return lines, mtime, size_mb, samples


# ─────────────────────────────────────────────────────────────────────────────
# Check 3: shadow execution events (shadow_execution_events.jsonl)
# ─────────────────────────────────────────────────────────────────────────────

def check_shadow_events() -> tuple[int, float | None, float, list[dict[str, Any]], Counter]:
    """
    Returns (lines, mtime, size_mb, tail_5_samples, recent_counter).
    recent_counter analyzes the most recent LOOKBACK_SHADOW_N events for:
      fresh_real_book_ok, missing_clob_token_id, book_fetch_failed, fallback_used=true
    """
    lines, mtime, size_mb = _file_info(SHADOW_EVENTS_PATH)
    items = _parse_jsonl(SHADOW_EVENTS_PATH)
    tail_5 = items[-5:] if len(items) >= 5 else items

    # Analyze recent events
    recent = items[-LOOKBACK_SHADOW_N:] if len(items) >= LOOKBACK_SHADOW_N else items
    counter: Counter = Counter()
    counter["total"] = len(recent)
    for ev in recent:
        rr = ev.get("reject_reason", "")
        if rr == "fresh_real_book_ok":
            counter["fresh_real_book_ok"] += 1
        elif rr == "missing_clob_token_id":
            counter["missing_clob_token_id"] += 1
        elif rr == "book_fetch_failed":
            counter["book_fetch_failed"] += 1
        elif rr in ("book_endpoint_failed", "fresh_book_404", "token_resolve_failed",
                     "insufficient_liquidity"):
            counter[rr] += 1
        if ev.get("fallback_used") is True:
            counter["fallback_used"] += 1

    return lines, mtime, size_mb, tail_5, counter


# ─────────────────────────────────────────────────────────────────────────────
# Check 4: shadow_execution_recorder.py --follow process exists
# ─────────────────────────────────────────────────────────────────────────────

def check_shadow_process() -> bool:
    """Check if a shadow_execution_recorder.py --follow process is running."""
    stdout, rc = _run(["pgrep", "-f", SHADOW_PROCESS_KEYWORD], timeout=3.0)
    return rc == 0 and len(stdout.strip()) > 0


# ─────────────────────────────────────────────────────────────────────────────
# Check 5: tmux session shadow_follow exists
# ─────────────────────────────────────────────────────────────────────────────

def check_tmux_session() -> bool:
    """Check if tmux session shadow_follow exists."""
    out, rc = _run(["tmux", "ls", "-F", "#{session_name}"], timeout=3.0)
    if rc != 0:
        return False
    for name in out.split("\n"):
        if name.strip() == TMUX_SESSION:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Check 5b: detect --threshold from running shadow recorder process
# ─────────────────────────────────────────────────────────────────────────────

def get_shadow_threshold() -> int:
    """
    Read --threshold value from the running shadow_execution_recorder.py
    process's /proc/<pid>/cmdline. Returns 0 if process not found or flag absent.
    """
    stdout, rc = _run(["pgrep", "-f", SHADOW_PROCESS_KEYWORD], timeout=3.0)
    if rc != 0 or not stdout.strip():
        return 0
    for pid_str in stdout.strip().split():
        try:
            cmdline_path = Path(f"/proc/{pid_str.strip()}/cmdline")
            if not cmdline_path.exists():
                continue
            raw = cmdline_path.read_bytes().decode("utf-8", errors="replace")
            args = raw.split("\x00")
            for i, arg in enumerate(args):
                if arg == "--threshold" and i + 1 < len(args):
                    return int(args[i + 1])
        except (ValueError, OSError):
            continue
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Check 5c: shadow idle vs truly stale
# ─────────────────────────────────────────────────────────────────────────────

def check_shadow_idle_ok(
    sh_mtime: float,
    threshold: int,
    signal_items: list[dict[str, Any]],
) -> tuple[bool, str]:
    """
    Determine whether a long shadow-events gap is benign idle or a real stale.

    Returns (idle_ok, reason_message) where:
      idle_ok=True  → no signal_started event with dist >= threshold occurred
                       after sh_mtime, so shadow recorder had nothing to write.
                       Caller should report IDLE_OK, not STALE.
      idle_ok=False → at least one qualifying signal existed but shadow did not
                       write, which is a real anomaly.

    Rules:
      - Only considers event_type == "signal_started" entries.
      - Only considers events with ts > sh_mtime.
      - Qualifying dist is dist >= threshold.
    """
    if threshold <= 0:
        return False, "threshold unknown (process not running or flag missing)"

    sh_mtime_dt = datetime.fromtimestamp(sh_mtime, tz=timezone.utc)
    qualifying: list[dict[str, Any]] = []

    for item in signal_items:
        if item.get("event_type") != "signal_started":
            continue
        dist = item.get("dist", 0)
        if dist < threshold:
            continue
        ts_str = item.get("ts", "")
        if not ts_str:
            continue
        try:
            ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts_dt > sh_mtime_dt:
                qualifying.append(item)
        except ValueError:
            continue

    if not qualifying:
        return (
            True,
            f"shadow recorder idle because no signal exceeded threshold={threshold}"
            f" since last shadow event",
        )
    return (
        False,
        f"{len(qualifying)} signal(s) with dist>={threshold} existed after last shadow"
        f" write but shadow_execution_events.jsonl was not updated",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Check 6: recent ERROR / Exception / Traceback in journal
# ─────────────────────────────────────────────────────────────────────────────

def check_journal_errors() -> int:
    """Count journal ERROR lines in last RECENT_MINUTES for the service."""
    out, rc = _run(
        [
            "journalctl", "-u", SERVICE_NAME,
            "--since", f"{RECENT_MINUTES} min ago",
            "--no-pager", "-q",
            "-p", "3",  # err priority
        ],
        timeout=10.0,
    )
    if rc != 0 or not out:
        return 0
    return len([l for l in out.split("\n") if l.strip()])


# ─────────────────────────────────────────────────────────────────────────────
# Main health check
# ─────────────────────────────────────────────────────────────────────────────

def run_health_check() -> HealthEvent:
    ts = _now_utc()
    warnings: list[str] = []

    # 1. Service
    svc_active, svc_uptime = check_service()

    # 2. Signal events
    sig_lines, sig_mtime, sig_mb, sig_samples = check_signal_events()
    sig_age = _mtime_delta_s(sig_mtime)

    # 3. Shadow events
    sh_lines, sh_mtime, sh_mb, sh_samples, sh_counter = check_shadow_events()
    sh_age = _mtime_delta_s(sh_mtime)

    # 4. Shadow process
    sh_proc = check_shadow_process()

    # 5. Tmux session
    tmux_ok = check_tmux_session()

    # 6. Journal errors
    j_errors = check_journal_errors()

    # ── Shadow threshold-aware idle check ──
    shadow_threshold = get_shadow_threshold() if sh_proc else 0
    shadow_idle_ok = False
    shadow_idle_reason = ""

    if sh_mtime is not None and sh_age is not None and sh_age > 600:
        if sh_proc and tmux_ok and shadow_threshold > 0:
            # Load all signal events once for idle analysis
            sig_items_all = _parse_jsonl(SIGNAL_EVENTS_PATH)
            shadow_idle_ok, shadow_idle_reason = check_shadow_idle_ok(
                sh_mtime, shadow_threshold, sig_items_all
            )
        # If process not running or threshold unknown, idle_ok stays False → real stale below

    # ── Build warnings ──
    if not svc_active:
        warnings.append("service NOT active")
    if sig_mtime is None:
        warnings.append("signal_events.jsonl NOT FOUND")
    elif sig_age is not None and sig_age > 600:
        warnings.append(f"signal_events.jsonl STALE: {sig_age:.0f}s since last write")
    if sh_mtime is None:
        warnings.append("shadow_events.jsonl NOT FOUND")
    elif sh_age is not None and sh_age > 600:
        if shadow_idle_ok:
            # Benign idle: no qualifying signals during stale window — not a warning
            pass
        else:
            # Real stale: either process dead, or signals existed but shadow didn't write
            warnings.append(
                f"shadow_events.jsonl STALE: {sh_age:.0f}s — {shadow_idle_reason}"
                if shadow_idle_reason
                else f"shadow_events.jsonl STALE: {sh_age:.0f}s since last write"
            )
    if not sh_proc:
        warnings.append("shadow_execution_recorder.py process NOT found")
    if not tmux_ok:
        warnings.append("tmux session shadow_follow NOT found")
    if j_errors > 0:
        warnings.append(f"journalctl shows {j_errors} ERROR lines in last {RECENT_MINUTES}min")

    # ── Fresh real book OK ratio ──
    total_recent = sh_counter.get("total", 0)
    fresh_ok = sh_counter.get("fresh_real_book_ok", 0)
    missing = sh_counter.get("missing_clob_token_id", 0)
    bff = sh_counter.get("book_fetch_failed", 0)
    fallback = sh_counter.get("fallback_used", 0)

    # ── Determine status ──
    if not svc_active:
        status = "BROKEN"
    elif warnings and any(w.startswith("service") for w in warnings):
        status = "BROKEN"
    elif len(warnings) >= 3:
        status = "PARTIAL"
    elif warnings:
        status = "PARTIAL"
    else:
        status = "HEALTHY"

    # Promote to BROKEN only when shadow is GENUINELY stale (not idle_ok),
    # fresh_ok == 0, and signals are actively being produced.
    if (
        not shadow_idle_ok
        and fresh_ok == 0
        and sh_age is not None and sh_age > 300
        and sig_lines > 0
    ):
        if status == "HEALTHY":
            status = "PARTIAL"
        if sh_age > 1800:
            status = "BROKEN"

    event = HealthEvent(
        ts_utc=ts,
        status=status,
        service_active=svc_active,
        service_uptime_sec=svc_uptime,
        signal_events_lines=sig_lines,
        signal_events_mtime=sig_mtime,
        signal_events_size_mb=sig_mb,
        shadow_events_lines=sh_lines,
        shadow_events_mtime=sh_mtime,
        shadow_events_size_mb=sh_mb,
        shadow_recent_total=total_recent,
        shadow_fresh_real_book_ok=fresh_ok,
        shadow_missing_clob_token_id=missing,
        shadow_book_fetch_failed=bff,
        shadow_fallback_used=fallback,
        shadow_process_running=sh_proc,
        tmux_session_exists=tmux_ok,
        errors_recent=0,
        exceptions_recent=0,
        tracebacks_recent=0,
        journal_errors_last_15m=j_errors,
        latest_signal_samples=sig_samples,
        latest_shadow_samples=sh_samples,
        extra_warnings=warnings,
        shadow_idle_ok=shadow_idle_ok,
        shadow_threshold=shadow_threshold,
        shadow_idle_reason=shadow_idle_reason,
    )
    return event


# ─────────────────────────────────────────────────────────────────────────────
# Report rendering
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_age(mtime: float | None) -> str:
    if mtime is None:
        return "N/A (file missing)"
    age = time.time() - mtime
    if age < 60:
        return f"{age:.0f}s ago"
    elif age < 3600:
        return f"{age / 60:.1f}m ago"
    else:
        return f"{age / 3600:.1f}h ago"


def _fmt_uptime(sec: float | None) -> str:
    if sec is None:
        return "N/A"
    if sec < 3600:
        return f"{sec / 60:.1f}m"
    elif sec < 86400:
        return f"{sec / 3600:.1f}h"
    else:
        return f"{sec / 86400:.1f}d"


def _fmt_pct(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "N/A"
    return f"{numerator / denominator:.1%}"


def render_report(event: HealthEvent) -> str:
    lines: list[str] = []
    a = lines.append

    a(f"# VPS Health Check Report")
    a(f"")
    a(f"> Generated: {event.ts_utc}")
    a(f"> Status: **{event.status}**")
    a(f"> Service: {SERVICE_NAME}")
    a(f"> VPS Root: {VPS_ROOT}")
    a(f"")
    a("---")
    a("")
    a("## 1. Main Service")
    a("")
    a(f"| Metric | Value |")
    a(f"|--------|-------|")
    a(f"| Active | {'YES' if event.service_active else '**NO**'} |")
    a(f"| Uptime | {_fmt_uptime(event.service_uptime_sec)} |")
    a("")
    a("## 2. Fresh Signal Events")
    a("")
    a(f"| Metric | Value |")
    a(f"|--------|-------|")
    a(f"| Lines | {event.signal_events_lines} |")
    a(f"| Last write | {_fmt_age(event.signal_events_mtime)} |")
    a(f"| Size | {event.signal_events_size_mb:.2f} MB |")
    a("")
    if event.latest_signal_samples:
        a("### Latest signal samples")
        a("```json")
        for s in event.latest_signal_samples:
            a(json.dumps(s, ensure_ascii=False, default=str))
        a("```")
    a("")
    a("## 3. Shadow Execution Events")
    a("")
    a(f"| Metric | Value |")
    a(f"|--------|-------|")
    a(f"| Lines | {event.shadow_events_lines} |")
    a(f"| Last write | {_fmt_age(event.shadow_events_mtime)} |")
    a(f"| Size | {event.shadow_events_size_mb:.2f} MB |")
    a(f"| Threshold | {event.shadow_threshold if event.shadow_threshold else 'unknown'} |")
    if event.shadow_idle_ok:
        a(f"| Status | ✅ IDLE_OK |")
        a(f"| Idle reason | {event.shadow_idle_reason} |")
    a("")
    a("## 4. Real CLOB / Fallback Analysis (last ~100 events)")
    a("")
    rec = event.shadow_recent_total
    a(f"| Metric | Value |")
    a(f"|--------|-------|")
    a(f"| Recent events analyzed | {rec} |")
    a(f"| fresh_real_book_ok | {event.shadow_fresh_real_book_ok} ({_fmt_pct(event.shadow_fresh_real_book_ok, rec)}) |")
    a(f"| missing_clob_token_id | {event.shadow_missing_clob_token_id} ({_fmt_pct(event.shadow_missing_clob_token_id, rec)}) |")
    a(f"| book_fetch_failed | {event.shadow_book_fetch_failed} ({_fmt_pct(event.shadow_book_fetch_failed, rec)}) |")
    a(f"| fallback_used=true | {event.shadow_fallback_used} ({_fmt_pct(event.shadow_fallback_used, rec)}) |")
    a("")
    if event.latest_shadow_samples:
        a("### Latest shadow samples")
        a("```json")
        for s in event.latest_shadow_samples:
            a(json.dumps(s, ensure_ascii=False, default=str))
        a("```")
    a("")
    a("## 5. Process / Session Health")
    a("")
    a(f"| Metric | Value |")
    a(f"|--------|-------|")
    a(f"| shadow_execution_recorder.py running | {'YES' if event.shadow_process_running else '**NO**'} |")
    a(f"| tmux session shadow_follow | {'YES' if event.tmux_session_exists else '**NO**'} |")
    a("")
    a("## 6. Anomalies")
    a("")
    a(f"| Metric | Value |")
    a(f"|--------|-------|")
    a(f"| Journal ERRORs (last {RECENT_MINUTES}min) | {event.journal_errors_last_15m} |")
    a("")
    if event.extra_warnings:
        a("### Warnings")
        for w in event.extra_warnings:
            a(f"- ⚠️ {w}")
        a("")
    else:
        a("No warnings detected.")
        a("")
    a("---")
    a("")
    a("## Next Steps Suggestions")
    a("")
    if event.status == "HEALTHY":
        a("- All systems nominal. No action required.")
    elif event.status == "PARTIAL":
        a("- Review warnings above. May need `systemctl restart` or log investigation.")
        if not event.shadow_process_running:
            a("- Start shadow follower: `tmux new -s shadow_follow -d 'python3 research/shadow_execution_recorder.py --follow'`")
    else:
        a("- **IMMEDIATE ATTENTION REQUIRED**")
        if not event.service_active:
            a(f"- Restart service: `sudo systemctl restart {SERVICE_NAME}`")
        a("- Check full logs: `sudo journalctl -u polymarket-okx-anchor.service --no-pager -n 200`")

    return "\n".join(lines)


def append_health_event(event: HealthEvent) -> None:
    """Append one HealthEvent to vps_health_events.jsonl."""
    HEALTH_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec = asdict(event)
    # Don't serialize samples in the jsonl to keep it lean
    rec.pop("latest_signal_samples", None)
    rec.pop("latest_shadow_samples", None)
    rec.pop("extra_warnings", None)
    with HEALTH_EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Self-test (python3 vps_health_check.py --test)
# ─────────────────────────────────────────────────────────────────────────────

def _run_self_tests() -> None:
    """Minimal unit tests for check_shadow_idle_ok logic. Exits 0 on pass."""
    import time as _time

    now = _time.time()
    threshold = 130

    def make_signal(dist: float, offset_s: float) -> dict[str, Any]:
        ts = datetime.fromtimestamp(now - offset_s, tz=timezone.utc)
        return {
            "event_type": "signal_started",
            "dist": dist,
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    # sh_mtime = 1 hour ago
    sh_mtime_1h = now - 3600

    # Case 1: no signals after sh_mtime → IDLE_OK
    signals_none: list[dict[str, Any]] = [
        make_signal(80.0, 7200),   # 2h ago — before sh_mtime
        make_signal(110.0, 5400),  # 1.5h ago — before sh_mtime
    ]
    ok, reason = check_shadow_idle_ok(sh_mtime_1h, threshold, signals_none)
    assert ok is True, f"Case 1 FAIL: expected idle_ok=True, got {ok}; reason={reason}"
    assert f"threshold={threshold}" in reason, f"Case 1 FAIL: reason missing threshold: {reason}"
    print(f"  ✅ Case 1 PASS: no qualifying signals → IDLE_OK ({reason})")

    # Case 2: signals exist after sh_mtime but dist < threshold → IDLE_OK
    signals_low_dist: list[dict[str, Any]] = [
        make_signal(60.0, 1800),   # 30m ago — after sh_mtime, but dist < 130
        make_signal(129.9, 900),   # 15m ago — after sh_mtime, dist just below threshold
    ]
    ok, reason = check_shadow_idle_ok(sh_mtime_1h, threshold, signals_low_dist)
    assert ok is True, f"Case 2 FAIL: expected idle_ok=True, got {ok}; reason={reason}"
    print(f"  ✅ Case 2 PASS: signals below threshold → IDLE_OK ({reason})")

    # Case 3: qualifying signal (dist >= threshold) after sh_mtime → NOT idle_ok
    signals_qualifying: list[dict[str, Any]] = [
        make_signal(131.0, 1800),  # 30m ago — after sh_mtime, dist > 130
    ]
    ok, reason = check_shadow_idle_ok(sh_mtime_1h, threshold, signals_qualifying)
    assert ok is False, f"Case 3 FAIL: expected idle_ok=False, got {ok}; reason={reason}"
    assert "1 signal" in reason, f"Case 3 FAIL: reason missing count: {reason}"
    print(f"  ✅ Case 3 PASS: qualifying signal present → NOT idle_ok ({reason})")

    # Case 4: threshold=0 (process not found) → NOT idle_ok
    ok, reason = check_shadow_idle_ok(sh_mtime_1h, 0, signals_none)
    assert ok is False, f"Case 4 FAIL: expected idle_ok=False with threshold=0, got {ok}"
    print(f"  ✅ Case 4 PASS: threshold=0 → NOT idle_ok ({reason})")

    # Case 5: mixed signals — one before, one after sh_mtime above threshold
    signals_mixed: list[dict[str, Any]] = [
        make_signal(150.0, 7200),  # 2h ago — before sh_mtime → ignored
        make_signal(135.0, 1800),  # 30m ago — after sh_mtime → qualifies
    ]
    ok, reason = check_shadow_idle_ok(sh_mtime_1h, threshold, signals_mixed)
    assert ok is False, f"Case 5 FAIL: expected idle_ok=False, got {ok}; reason={reason}"
    print(f"  ✅ Case 5 PASS: only post-mtime signals counted ({reason})")

    print("\n✅ All self-tests passed.")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if "--test" in sys.argv:
        print("Running self-tests for check_shadow_idle_ok …\n")
        _run_self_tests()
        sys.exit(0)

    event = run_health_check()
    report = render_report(event)

    # Write report
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"[vps_health_check] Report written to {REPORT_PATH}")

    # Append event
    append_health_event(event)
    print(f"[vps_health_check] Event appended to {HEALTH_EVENTS_PATH}")

    # Exit with status code for monitoring
    if event.status == "HEALTHY":
        sys.exit(0)
    elif event.status == "PARTIAL":
        sys.exit(1)
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()