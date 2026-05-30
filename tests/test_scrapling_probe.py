"""tests/test_scrapling_probe.py — offline unit tests for pure functions."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from research.scrapling_probe import (
    detect_blocked_like,
    detect_scrapling,
    extract_title,
    structure_hash,
    summarize_results,
    verdict,
    MIN_MEANINGFUL_HTML,
)

# ---------------------------------------------------------------------------
# detect_scrapling
# ---------------------------------------------------------------------------

class TestDetectScrapling:
    def test_returns_dict(self):
        r = detect_scrapling()
        assert isinstance(r, dict)

    def test_required_keys(self):
        r = detect_scrapling()
        for k in ("available", "version", "message", "install_cmd"):
            assert k in r

    def test_available_is_bool(self):
        assert isinstance(detect_scrapling()["available"], bool)

    def test_not_available_has_install_cmd(self):
        r = detect_scrapling()
        if not r["available"]:
            assert "pip install" in r["install_cmd"]

    def test_available_has_no_install_cmd(self):
        r = detect_scrapling()
        if r["available"]:
            assert r["install_cmd"] == ""


# ---------------------------------------------------------------------------
# extract_title
# ---------------------------------------------------------------------------

class TestExtractTitle:
    def test_basic(self):
        assert extract_title("<html><title>Hello</title></html>") == "Hello"

    def test_with_attrs(self):
        assert extract_title('<title lang="en">Markets</title>') == "Markets"

    def test_whitespace_collapsed(self):
        assert extract_title("<title>  Hello   World  </title>") == "Hello World"

    def test_none_when_missing(self):
        assert extract_title("<html><body>no title</body></html>") is None

    def test_empty_title_returns_none(self):
        assert extract_title("<title>   </title>") is None

    def test_multiline(self):
        html = "<html><title>\n  Page Title\n</title></html>"
        result = extract_title(html)
        assert result == "Page Title"

    def test_case_insensitive(self):
        assert extract_title("<TITLE>Upper</TITLE>") == "Upper"

    def test_empty_string(self):
        assert extract_title("") is None


# ---------------------------------------------------------------------------
# detect_blocked_like
# ---------------------------------------------------------------------------

class TestDetectBlockedLike:
    def _ok_html(self, extra: str = "") -> str:
        return "<html><head><title>Polymarket</title></head><body>" + "x" * 600 + extra + "</body></html>"

    def test_ok_page(self):
        blocked, reason = detect_blocked_like(self._ok_html(), 200)
        assert blocked is False
        assert reason is None

    def test_403(self):
        blocked, reason = detect_blocked_like(self._ok_html(), 403)
        assert blocked is True
        assert "403" in reason

    def test_429(self):
        blocked, reason = detect_blocked_like(self._ok_html(), 429)
        assert blocked is True
        assert "429" in reason

    def test_503(self):
        blocked, reason = detect_blocked_like(self._ok_html(), 503)
        assert blocked is True

    def test_too_small(self):
        blocked, reason = detect_blocked_like("<html>tiny</html>", 200)
        assert blocked is True
        assert reason == "html_too_small"

    def test_empty_html(self):
        blocked, reason = detect_blocked_like("", 200)
        assert blocked is True

    def test_cloudflare_signal(self):
        html = self._ok_html() + " Cloudflare challenge "
        blocked, reason = detect_blocked_like(html, 200)
        assert blocked is True
        assert "cloudflare" in reason

    def test_just_a_moment_signal(self):
        html = self._ok_html() + " Just a moment... "
        blocked, reason = detect_blocked_like(html, 200)
        assert blocked is True

    def test_captcha_signal(self):
        html = self._ok_html() + " please complete captcha "
        blocked, reason = detect_blocked_like(html, 200)
        assert blocked is True
        assert "captcha" in reason

    def test_case_insensitive_signals(self):
        html = self._ok_html() + " CLOUDFLARE protection "
        blocked, reason = detect_blocked_like(html, 200)
        assert blocked is True

    def test_robots_meta_tag_not_blocked(self):
        # <meta name="robots" content="index,follow"> is normal SEO — not a block signal
        html = self._ok_html() + '<meta name="robots" content="index, follow">'
        blocked, reason = detect_blocked_like(html, 200)
        assert blocked is False

    def test_boundary_size_just_ok(self):
        html = "x" * MIN_MEANINGFUL_HTML
        blocked, _ = detect_blocked_like(html, 200)
        assert blocked is False

    def test_boundary_size_just_small(self):
        html = "x" * (MIN_MEANINGFUL_HTML - 1)
        blocked, _ = detect_blocked_like(html, 200)
        assert blocked is True


# ---------------------------------------------------------------------------
# summarize_results
# ---------------------------------------------------------------------------

def _rec(status="ok", latency=200.0, blocked=False):
    return {
        "status": status,
        "latency_ms": latency,
        "blocked_like": blocked,
        "scrapling_available": False,
    }


class TestSummarizeResults:
    def test_empty(self):
        s = summarize_results([])
        assert s["total"] == 0
        assert s["fetch_success_rate"] == 0.0

    def test_all_ok(self):
        recs = [_rec("ok", 100), _rec("ok", 200), _rec("ok", 300)]
        s = summarize_results(recs)
        assert s["fetch_ok"] == 3
        assert s["fetch_success_rate"] == pytest.approx(1.0)
        assert s["avg_latency_ms"] == pytest.approx(200.0)

    def test_mixed(self):
        recs = [_rec("ok", 100), _rec("error", 500), _rec("ok", 300, blocked=True)]
        s = summarize_results(recs)
        assert s["total"] == 3
        assert s["fetch_ok"] == 2
        assert s["fetch_errors"] == 1
        assert s["blocked_count"] == 1
        assert s["fetch_success_rate"] == pytest.approx(2 / 3, abs=1e-3)
        assert s["blocked_rate"] == pytest.approx(1 / 3, abs=1e-3)

    def test_p95_latency(self):
        recs = [_rec("ok", float(i * 10)) for i in range(1, 21)]  # 10..200
        s = summarize_results(recs)
        assert s["p95_latency_ms"] is not None
        # p95 of 20 items = item at index 19 (last) of sorted list
        assert s["p95_latency_ms"] == 200.0

    def test_scrapling_available_propagated(self):
        recs = [{"status": "ok", "latency_ms": 100, "blocked_like": False,
                 "scrapling_available": True}]
        s = summarize_results(recs)
        assert s["scrapling_available"] is True

    def test_none_latency_skipped(self):
        recs = [_rec("error", None), _rec("ok", 200)]
        s = summarize_results(recs)
        assert s["avg_latency_ms"] == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------

class TestVerdict:
    def _info(self, available: bool) -> dict:
        return {"available": available, "version": "0.1" if available else None,
                "message": "", "install_cmd": "" if available else "pip install scrapling"}

    def test_no_scrapling_is_need_more_test(self):
        summary = {"fetch_success_rate": 1.0, "blocked_rate": 0.0}
        assert verdict(summary, self._info(False)) == "NEED_MORE_TEST"

    def test_high_block_rate_is_nogo(self):
        summary = {"fetch_success_rate": 0.3, "blocked_rate": 0.7}
        assert verdict(summary, self._info(True)) == "NO-GO"

    def test_low_success_rate_is_nogo(self):
        summary = {"fetch_success_rate": 0.4, "blocked_rate": 0.1}
        assert verdict(summary, self._info(True)) == "NO-GO"

    def test_scrapling_ok_returns_need_more_test(self):
        summary = {"fetch_success_rate": 0.9, "blocked_rate": 0.1}
        assert verdict(summary, self._info(True)) == "NEED_MORE_TEST"

    def test_never_go(self):
        # Verify GO is never returned by this function
        for avail in (True, False):
            for sr, br in [(1.0, 0.0), (0.9, 0.1), (0.0, 1.0)]:
                result = verdict({"fetch_success_rate": sr, "blocked_rate": br},
                                 self._info(avail))
                assert result != "GO", f"Got GO for avail={avail} sr={sr} br={br}"
