"""
research/run.py — Entry point for microstructure & event alpha research.

Usage:
    python -m research.run [--duration 3600] [--no-news]

STATS_ONLY — No real orders. No capital at risk.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

from research.engine import (
    EngineState,
    heartbeat_task,
    okx_ws_task,
    poly_discovery_task,
    poly_poll_task,
    save_experiment_results,
    signal_detection_task,
    DAILY_REPORT_PATH,
)
from research.stats import generate_daily_report, go_nogo_check, compute_experiment_summaries

# ─────────────────────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────────────────────

LOG_FMT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FMT,
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("research.run")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Lightweight news event collector
# ─────────────────────────────────────────────────────────────────────────────

# Known crypto-breaking-news RSS/API sources (free, no auth required for RSS)
NEWS_RSS_FEEDS: list[tuple[str, str]] = [
    # ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    # ("CoinTelegraph", "https://cointelegraph.com/rss"),
    # ("Decrypt", "https://decrypt.co/feed"),
    # ("The Block", "https://www.theblock.co/rss"),
    # ("CryptoSlate", "https://cryptoslate.com/feed/"),
]

NEWS_KEYWORDS: dict[str, list[str]] = {
    "etf": ["etf", "bitcoin etf", "spot etf", "etf approval", "etf denial"],
    "fed": ["fed", "federal reserve", "interest rate", "fomc", "inflation", "cpi"],
    "hack": ["hack", "exploit", "security breach", "stolen", "drained"],
    "liquidation": ["liquidation", "liquidated", "margin call", "cascade"],
    "macro": ["gdp", "unemployment", "treasury", "bond", "yield", "tariff"],
}


def _match_keywords(text: str) -> list[str]:
    """Return list of matching keyword categories."""
    tl = text.lower()
    matched = []
    for cat, kws in NEWS_KEYWORDS.items():
        for kw in kws:
            if kw in tl:
                matched.append(cat)
                break
    return matched


def _classify_sentiment(headline: str) -> str:
    """Simple keyword-based sentiment classification."""
    hl = headline.lower()
    bullish_words = [
        "surge", "soar", "rally", "bull", "breakout", "approval",
        "positive", "upgrade", "record high", "accumulate",
    ]
    bearish_words = [
        "crash", "plunge", "dump", "bear", "crash", "sell-off",
        "rejection", "denial", "hack", "exploit", "liquidation",
        "crackdown", "ban", "lawsuit", "sec", "fine",
    ]
    bullish_count = sum(1 for w in bullish_words if w in hl)
    bearish_count = sum(1 for w in bearish_words if w in hl)
    if bullish_count > bearish_count:
        return "bullish"
    elif bearish_count > bullish_count:
        return "bearish"
    return "neutral"


async def news_collector_task(state: EngineState, scan_interval_s: float = 30.0) -> None:
    """
    Lightweight news collector — scrapes RSS feeds, matches keywords.
    Falls back to manual mode if no RSS feeds are configured or available.
    """
    from research.models import NewsEvent
    import xml.etree.ElementTree as ET

    # Try to import feedparser, fallback to ET-based RSS parsing
    try:
        import feedparser
        HAS_FEEDPARSER = True
    except ImportError:
        HAS_FEEDPARSER = False

    conn = None
    try:
        import aiohttp
        import ssl as _ssl_mod
        ctx = _ssl_mod.create_default_context()
        try:
            import certifi
            ctx = _ssl_mod.create_default_context(cafile=certifi.where())
        except ImportError:
            pass
        conn = aiohttp.TCPConnector(ssl=ctx)
    except ImportError:
        aiohttp = None  # type: ignore

    last_headlines: set[str] = set()
    rss_feeds = list(NEWS_RSS_FEEDS)

    while not state.shutdown.is_set():
        ts_now = time.time()

        for source_name, feed_url in rss_feeds:
            if state.shutdown.is_set():
                break
            try:
                async with aiohttp.ClientSession(connector=conn) as session:
                    async with session.get(feed_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        xml_text = await resp.text()

                entries = []
                if HAS_FEEDPARSER:
                    parsed = feedparser.parse(xml_text)
                    for e in parsed.entries:
                        entries.append({
                            "title": e.get("title", ""),
                            "published": e.get("published", ""),
                            "summary": e.get("summary", ""),
                        })
                else:
                    # Basic XML RSS parser
                    root = ET.fromstring(xml_text)
                    for item in root.iter("item"):
                        title_el = item.find("title")
                        entries.append({
                            "title": title_el.text.strip() if title_el is not None and title_el.text else "",
                            "published": "",
                            "summary": "",
                        })

                for entry in entries:
                    title = entry.get("title", "")
                    if not title or title in last_headlines:
                        continue
                    last_headlines.add(title)
                    if len(last_headlines) > 500:
                        last_headlines = set(list(last_headlines)[-300:])

                    matched = _match_keywords(title)
                    if not matched:
                        continue  # skip non-crypto-relevant headlines

                    sentiment = _classify_sentiment(title)
                    ne = NewsEvent(
                        event_timestamp=ts_now,
                        event_source=source_name,
                        event_type=matched[0] if matched else "other",
                        headline=title[:200],
                        event_sentiment=sentiment,
                        keywords=matched,
                    )
                    state.news_events.append(ne)
                    log.info("[NEWS] %s | %s | %s: %s", sentiment, matched[0], source_name, title[:80])

            except Exception:
                pass  # Silently skip failed feeds

        try:
            await asyncio.wait_for(
                asyncio.shield(state.shutdown.wait()), timeout=scan_interval_s
            )
        except asyncio.TimeoutError:
            pass

    log.info("[NEWS] collector stopped (%d events captured)", len(state.news_events))


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────


async def _run(duration_s: float, enable_news: bool) -> None:
    """Core run loop."""
    state = EngineState()

    log.info("═══════════════════════════════════════════════════════════")
    log.info(" MICROSTRUCTURE ALPHA RESEARCH ENGINE")
    log.info(" STATS_ONLY — No real orders. No capital at risk.")
    log.info(" Duration: %.0fs | News: %s", duration_s, "ON" if enable_news else "OFF")
    log.info("═══════════════════════════════════════════════════════════")

    t_start = time.monotonic()

    # Signal handler for graceful shutdown
    def _handle_sig(sig, frame):
        log.info("Signal %s received, shutting down...", sig)
        state.shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_sig)
        except Exception:
            pass

    # Build tasks
    tasks = [
        asyncio.create_task(poly_discovery_task(state), name="disc"),
        asyncio.create_task(poly_poll_task(state), name="poly"),
        asyncio.create_task(okx_ws_task(state), name="okx"),
        asyncio.create_task(signal_detection_task(state), name="detect"),
        asyncio.create_task(heartbeat_task(state, duration_s), name="heart"),
    ]

    if enable_news:
        tasks.append(asyncio.create_task(
            news_collector_task(state, scan_interval_s=30.0), name="news"
        ))

    # Run until duration or shutdown
    try:
        await asyncio.wait_for(asyncio.shield(state.shutdown.wait()), timeout=duration_s)
    except asyncio.TimeoutError:
        log.info("Duration elapsed (%.0fs), stopping...", duration_s)

    state.shutdown.set()

    # Cancel all tasks
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    elapsed = time.monotonic() - t_start
    log.info("Session ended after %.0fs", elapsed)

    # ── Save outputs ──────────────────────────────────────────────────────────
    save_experiment_results(state)

    # Generate daily report
    md = generate_daily_report(
        signals=state.tagged_signals,
        news_events=state.news_events,
        output_path=DAILY_REPORT_PATH,
        elapsed_s=elapsed,
    )

    # Print quick summary
    summaries = [
        s for s in state.tagged_signals
    ]
    log.info("═══════════════════════════════════════════════════════════")
    log.info(" SESSION SUMMARY")
    log.info(" Signals: %d raw, %d tagged | News: %d",
             len(state.raw_events), len(state.tagged_signals),
             len(state.news_events))
    log.info(" OKX ticks: %d | Poly polls: %d | Markets: %d | Errors: %d",
             state.okx_ticks, state.poly_polls,
             len(state.poly_markets), state.errors)
    log.info(" Report: %s", DAILY_REPORT_PATH)
    log.info(" JSONL:  research/raw_events.jsonl, research/tagged_signals.jsonl")
    log.info(" JSON:   research/experiment_results.json")

    # ── GO / NO-GO ────────────────────────────────────────────────────────────
    if state.tagged_signals:
        summaries = compute_experiment_summaries(state.tagged_signals)
        gate = go_nogo_check(summaries, state.tagged_signals)
        log.info(" ─────────────────────────────────────────────────────────")
        log.info(" GO / NO-GO: %s", gate["verdict"])
        log.info(" Reason: %s", gate["reason"])
        for cname, cdata in gate.get("criteria", {}).items():
            met = "✅" if cdata["met"] else "❌"
            log.info("   %s %s: %s (need %s)", met, cname, cdata.get("value", "?"), cdata.get("threshold", "?"))
    else:
        log.info(" ─────────────────────────────────────────────────────────")
        log.info(" GO / NO-GO: NO-GO (no signals collected)")
    log.info("═══════════════════════════════════════════════════════════")


def main():
    import argparse

    ap = argparse.ArgumentParser(
        description="Polymarket Microstructure Alpha Research Engine"
    )
    ap.add_argument(
        "--duration", type=float, default=float(
            os.environ.get("RESEARCH_DURATION", "3600")
        ),
        help="Session duration in seconds (default: 3600)"
    )
    ap.add_argument(
        "--no-news", action="store_true",
        help="Disable news/event collector"
    )
    args = ap.parse_args()

    try:
        asyncio.run(_run(args.duration, not args.no_news))
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except Exception:
        log.exception("Fatal error in research engine")
        sys.exit(1)


if __name__ == "__main__":
    main()