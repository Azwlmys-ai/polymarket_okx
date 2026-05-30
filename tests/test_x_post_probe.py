"""tests/test_x_post_probe.py — offline unit tests for pure functions."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from research.x_post_probe import (
    classify_fetch_result,
    detect_block_signals,
    extract_meta_text,
    normalize_x_url,
)


# ---------------------------------------------------------------------------
# normalize_x_url
# ---------------------------------------------------------------------------

class TestNormalizeXUrl:
    def test_x_com_unchanged(self):
        url = "https://x.com/user/status/123"
        assert normalize_x_url(url) == "https://x.com/user/status/123"

    def test_twitter_rewritten(self):
        assert normalize_x_url("https://twitter.com/user/status/123") \
               == "https://x.com/user/status/123"

    def test_www_twitter_rewritten(self):
        assert normalize_x_url("https://www.twitter.com/user/status/123") \
               == "https://x.com/user/status/123"

    def test_www_x_rewritten(self):
        assert normalize_x_url("https://www.x.com/user/status/123") \
               == "https://x.com/user/status/123"

    def test_http_upgraded(self):
        result = normalize_x_url("http://twitter.com/user/status/123")
        assert result.startswith("https://x.com")

    def test_other_domain_unchanged(self):
        url = "https://polymarket.com/market/abc"
        assert normalize_x_url(url) == url

    def test_whitespace_stripped(self):
        result = normalize_x_url("  https://x.com/user/status/1  ")
        assert result == "https://x.com/user/status/1"


# ---------------------------------------------------------------------------
# detect_block_signals
# ---------------------------------------------------------------------------

def _big(extra: str = "") -> str:
    """Build an HTML string large enough not to trigger size heuristics."""
    return "<html><body>" + "x" * 500 + extra + "</body></html>"


class TestDetectBlockSignals:
    def test_clean_page(self):
        s = detect_block_signals(_big(), 200)
        assert s["blocked"] is False
        assert s["has_login_wall"] is False
        assert s["has_captcha"] is False
        assert s["has_rate_limit"] is False

    def test_status_429(self):
        s = detect_block_signals(_big(), 429)
        assert s["has_rate_limit"] is True
        assert s["blocked"] is True

    def test_status_401(self):
        s = detect_block_signals("", 401)
        assert s["has_login_wall"] is True
        assert s["blocked"] is True

    def test_status_403(self):
        s = detect_block_signals("", 403)
        assert s["has_login_wall"] is True
        assert s["blocked"] is True

    def test_captcha_in_html(self):
        s = detect_block_signals(_big(" hcaptcha required "), 200)
        assert s["has_captcha"] is True
        assert s["blocked"] is True

    def test_recaptcha(self):
        s = detect_block_signals(_big(" recaptcha "), 200)
        assert s["has_captcha"] is True

    def test_rate_limit_text(self):
        s = detect_block_signals(_big(" rate limit exceeded "), 200)
        assert s["has_rate_limit"] is True
        assert s["blocked"] is True

    def test_too_many_requests_text(self):
        s = detect_block_signals(_big(" too many requests "), 200)
        assert s["has_rate_limit"] is True

    def test_sign_in_to_x(self):
        s = detect_block_signals(_big(" sign in to x "), 200)
        assert s["has_login_wall"] is True
        assert s["blocked"] is True

    def test_authwall_marker(self):
        s = detect_block_signals(_big(" authwall "), 200)
        assert s["has_login_wall"] is True

    def test_case_insensitive(self):
        s = detect_block_signals(_big(" CAPTCHA "), 200)
        assert s["has_captcha"] is True

    def test_empty_html(self):
        s = detect_block_signals("", 200)
        assert s["blocked"] is True

    def test_503(self):
        s = detect_block_signals(_big(), 503)
        assert s["blocked"] is True

    def test_returns_all_keys(self):
        s = detect_block_signals(_big(), 200)
        for k in ("has_login_wall", "has_captcha", "has_rate_limit", "blocked"):
            assert k in s


# ---------------------------------------------------------------------------
# extract_meta_text
# ---------------------------------------------------------------------------

class TestExtractMetaText:
    def _og(self, prop: str, value: str) -> str:
        return f'<meta property="og:{prop}" content="{value}"/>'

    def test_og_title(self):
        html = self._og("title", "My Tweet")
        r = extract_meta_text(html)
        assert r["og_title"] == "My Tweet"

    def test_og_description(self):
        html = self._og("description", "Tweet text here")
        r = extract_meta_text(html)
        assert r["og_description"] == "Tweet text here"

    def test_title_tag(self):
        r = extract_meta_text("<title>Hello World</title>")
        assert r["title"] == "Hello World"

    def test_title_whitespace_collapsed(self):
        r = extract_meta_text("<title>  Hello   World  </title>")
        assert r["title"] == "Hello World"

    def test_html_entities_decoded(self):
        html = self._og("title", "It&#x27;s a tweet")
        r = extract_meta_text(html)
        assert r["og_title"] == "It's a tweet"

    def test_empty_html(self):
        r = extract_meta_text("")
        for v in r.values():
            assert v is None

    def test_no_meta(self):
        r = extract_meta_text("<html><body>nothing</body></html>")
        assert r["og_title"] is None
        assert r["og_description"] is None

    def test_reversed_attribute_order(self):
        # content attr before property attr
        html = '<meta content="Rev Title" property="og:title"/>'
        r = extract_meta_text(html)
        assert r["og_title"] == "Rev Title"

    def test_hydration_full_text(self):
        html = '{"full_text":"This is a real tweet about markets"}'
        r = extract_meta_text(html)
        assert r["hydration_text"] is not None
        assert "real tweet" in r["hydration_text"]

    def test_hydration_skips_urls(self):
        html = '{"full_text":"https://t.co/abc123"}'
        r = extract_meta_text(html)
        assert r["hydration_text"] is None   # URL-only strings skipped

    def test_returns_all_keys(self):
        r = extract_meta_text("")
        for k in ("og_title", "og_description", "title", "hydration_text"):
            assert k in r


# ---------------------------------------------------------------------------
# classify_fetch_result
# ---------------------------------------------------------------------------

class TestClassifyFetchResult:
    def _signals(self, **kw) -> dict:
        base = {"has_login_wall": False, "has_captcha": False,
                "has_rate_limit": False, "blocked": False}
        base.update(kw)
        return base

    def _meta(self, **kw) -> dict:
        base = {"og_title": None, "og_description": None,
                "title": None, "hydration_text": None}
        base.update(kw)
        return base

    def test_full_quality_with_og_description(self):
        meta    = self._meta(og_description="Great tweet content here")
        signals = self._signals()
        r = classify_fetch_result(200, "html", signals, meta)
        assert r["quality"] == "full"
        assert r["success"] is True
        assert r["blocked"] is False

    def test_partial_quality_with_title_only(self):
        meta    = self._meta(title="Tweet Title Page")
        signals = self._signals()
        r = classify_fetch_result(200, "html", signals, meta)
        assert r["quality"] == "partial"
        assert r["success"] is True

    def test_empty_no_meta(self):
        r = classify_fetch_result(200, "html", self._signals(), self._meta())
        assert r["quality"] == "empty"
        assert r["success"] is False

    def test_blocked_overrides_content(self):
        meta    = self._meta(og_description="some text that would otherwise pass")
        signals = self._signals(blocked=True, has_login_wall=True)
        r = classify_fetch_result(403, "", signals, meta)
        assert r["quality"] == "blocked"
        assert r["success"] is False
        assert r["blocked"] is True

    def test_text_candidates_populated(self):
        meta = self._meta(
            og_description="Tweet body text",
            og_title="Tweet Title",
        )
        r = classify_fetch_result(200, "html", self._signals(), meta)
        assert len(r["text_candidates"]) >= 1
        assert "Tweet body text" in r["text_candidates"]

    def test_text_candidates_empty_when_no_meta(self):
        r = classify_fetch_result(200, "html", self._signals(), self._meta())
        assert r["text_candidates"] == []

    def test_has_content_false_when_blocked(self):
        meta    = self._meta(og_title="Something")
        signals = self._signals(blocked=True)
        r = classify_fetch_result(403, "", signals, meta)
        assert r["has_content"] is False

    def test_short_texts_excluded(self):
        meta = self._meta(og_title="Hi")    # too short (≤ 10 chars)
        r = classify_fetch_result(200, "html", self._signals(), meta)
        assert r["text_candidates"] == []
