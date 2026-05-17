#!/usr/bin/env python3
"""
telegram_controller.py — Read-only + dry-run remote control via Telegram Bot.

SECURITY MODEL (hard-coded, not overridable):
  - All incoming messages require TELEGRAM_ALLOWED_CHAT_ID whitelist match.
  - Only one whitelisted command set is accepted; arbitrary shell is impossible.
  - Only dry-run mvp_runner.py can be launched (real trading remains disabled).
  - /stop uses SIGTERM only — SIGKILL is never called.
  - Token and chat_id are read from environment variables; absent → safe exit.
  - All received commands are logged with chat_id for audit.

Allowed commands:
  /status    — Show running state, PID, uptime, last heartbeat
  /markets   — Run 5-page discovery smoke; report BTC/ETH/SOL targets
  /start_14h — Start 14h dry-run (only if markets found and none running)
  /stop      — Send SIGTERM to running mvp_runner.py

Usage:
    export TELEGRAM_BOT_TOKEN="7xxxxxxxx:AAxxxxxx"
    export TELEGRAM_ALLOWED_CHAT_ID="123456789"
    python telegram_controller.py

Dry-run only. No real trading. No API keys. No wallet operations.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import ssl
import subprocess
import sys
from pathlib import Path

import aiohttp

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent

ENV_TOKEN   = "TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "TELEGRAM_ALLOWED_CHAT_ID"
TG_BASE     = "https://api.telegram.org/bot{token}"
GAMMA_URL   = "https://gamma-api.polymarket.com"

# The ONLY command allowed to spawn a subprocess — hardcoded, not user-configurable.
_RUNNER_ARGV: list[str] = [
    str(PROJECT_ROOT / ".venv" / "bin" / "python"),
    str(PROJECT_ROOT / "mvp_runner.py"),
    "--duration", "50400",
    "--log", "run_14h_tg.log",
    "--report", "MVP_RUN_REPORT_14h_tg.md",
]

_MIN_YES = 0.47
_MAX_YES = 0.53
_DISC_PAGES = 15         # pages for /markets smoke (15×100 = 1,500 raw; covers Up or Down region)
_STOP_WAIT_S = 15        # seconds to wait for graceful exit after SIGTERM

log = logging.getLogger("tg_ctrl")

# ─────────────────────────────────────────────────────────────────────────────
# SSL (same approach as mvp_runner.py)
# ─────────────────────────────────────────────────────────────────────────────

def _make_ssl_ctx() -> ssl.SSLContext:
    if os.environ.get("DISABLE_SSL_VERIFY", "").strip() == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi  # type: ignore[import]
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()

_SSL_CTX = _make_ssl_ctx()

# ─────────────────────────────────────────────────────────────────────────────
# Process helpers (thin wrappers so tests can patch them)
# ─────────────────────────────────────────────────────────────────────────────

def find_runner_pid() -> int | None:
    """Return PID of a running mvp_runner.py process, or None."""
    try:
        r = subprocess.run(
            ["pgrep", "-f", "mvp_runner.py"],
            capture_output=True, text=True, timeout=5,
        )
        pids = [int(p) for p in r.stdout.split() if p.strip().isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def runner_uptime_s(pid: int) -> float | None:
    """Return elapsed seconds for *pid* via `ps -p <pid> -o etimes=`."""
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etimes="],
            capture_output=True, text=True, timeout=5,
        )
        val = r.stdout.strip()
        return float(val) if val else None
    except Exception:
        return None


def send_sigterm(pid: int) -> bool:
    """Send SIGTERM to *pid*. Returns True if the signal was delivered."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False


def launch_runner() -> int:
    """
    Start mvp_runner.py as a detached subprocess.
    Returns the new PID.
    Raises on failure (caller handles).
    """
    env = os.environ.copy()
    env["DISABLE_SSL_VERIFY"] = "1"
    proc = subprocess.Popen(
        _RUNNER_ARGV,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,   # detach from controller process
    )
    return proc.pid

# ─────────────────────────────────────────────────────────────────────────────
# Polymarket discovery (mirrors watch_poly_markets.py logic, read-only)
# ─────────────────────────────────────────────────────────────────────────────
_RE_TIER4 = re.compile(
    r"(bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple|dogecoin|doge|bnb)"
    r".*(up or down|higher or lower).*"
    r"\d{1,2}:\d{2}(am|pm).*-.*\d{1,2}:\d{2}(am|pm)", re.I,
)
_RE_TIER3 = re.compile(
    r"(bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple|dogecoin|doge|bnb)"
    r".*(up or down|higher or lower)", re.I,
)
_ASSET_KW: dict[str, list[str]] = {
    "BTC":  ["bitcoin", " btc "],
    "ETH":  ["ethereum", " eth "],
    "SOL":  ["solana", " sol "],
    "XRP":  ["xrp", "ripple"],
    "DOGE": ["dogecoin", "doge"],
    "BNB":  ["bnb"],
}


def _parse_yes(m: dict) -> float | None:
    op = m.get("outcomePrices")
    if op:
        try:
            ps = op if isinstance(op, list) else json.loads(op)
            v = float(ps[0]) if ps else None
            return v if v and v > 0 else None
        except Exception:
            pass
    return None


async def run_discovery_smoke(
    session: aiohttp.ClientSession,
    pages: int = _DISC_PAGES,
) -> dict:
    """
    Scan *pages* pages sorted by newest startDate (read-only).
    Short-term Up or Down markets appear in the first ~30 pages.

    Returns::

        {
            "total":      int,               # unique markets seen
            "asset_hits": {"BTC":n, ...},    # broad keyword matches
            "tier4":      [title, ...],      # minute-level targets (YES in range)
            "tier3":      [title, ...],      # daily-direction targets (YES in range)
            "tradeable":  bool,              # True if tier4 or tier3 non-empty
        }
    """
    seen: set[str] = set()
    asset_hits: dict[str, int] = {k: 0 for k in _ASSET_KW}
    tier4: list[str] = []
    tier3: list[str] = []
    total = 0
    offset = 0
    timeout = aiohttp.ClientTimeout(total=15)

    for _ in range(pages):
        url = (
            f"{GAMMA_URL}/markets"
            f"?limit=100&order=startDate&ascending=false&offset={offset}"
        )
        try:
            async with session.get(url, timeout=timeout, ssl=_SSL_CTX) as resp:
                if resp.status != 200:
                    break
                items = await resp.json(content_type=None)
        except Exception as exc:
            log.warning("[DISC] fetch failed: %s", exc)
            break

        if not isinstance(items, list) or not items:
            break

        for m in items:
            mid = str(m.get("id") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            total += 1

            title = m.get("question") or m.get("title") or ""
            tl = title.lower()
            yes = _parse_yes(m)

            for asset, kws in _ASSET_KW.items():
                if any(kw in tl for kw in kws):
                    asset_hits[asset] += 1

            if yes and _MIN_YES < yes < _MAX_YES:
                if _RE_TIER4.search(title):
                    tier4.append(title[:80])
                elif _RE_TIER3.search(title):
                    tier3.append(title[:80])

        offset += 100
        if len(items) < 100:
            break

    return {
        "total": total,
        "asset_hits": asset_hits,
        "tier4": tier4,
        "tier3": tier3,
        "tradeable": bool(tier4 or tier3),
    }

# ─────────────────────────────────────────────────────────────────────────────
# Command handlers
# Each returns a plain string (Telegram message text, Markdown OK).
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_status(session: aiohttp.ClientSession) -> str:
    pid = find_runner_pid()
    if pid is None:
        return "📊 *Status*\n\n❌ No mvp\\_runner.py running."

    uptime = runner_uptime_s(pid)
    uptime_str = (
        f"{int(uptime)}s  ({uptime / 3600:.1f}h)" if uptime else "unknown"
    )
    last_line = ""
    log_path = PROJECT_ROOT / "run_14h_tg.log"
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            for line in reversed(lines):
                if "进度" in line or "POLY-DISC" in line or "POLY" in line:
                    last_line = line.strip()[-120:]
                    break
        except Exception:
            pass

    parts = [
        "📊 *Status*",
        "",
        f"✅ mvp\\_runner.py running",
        f"PID: `{pid}`",
        f"Uptime: `{uptime_str}`",
    ]
    if last_line:
        parts += ["", f"Last log:", f"`{last_line}`"]
    return "\n".join(parts)


async def cmd_markets(session: aiohttp.ClientSession) -> str:
    result = await run_discovery_smoke(session, pages=_DISC_PAGES)
    hits = result["asset_hits"]
    parts = [
        "🔍 *Markets Scan*",
        f"_(sampled {_DISC_PAGES} pages ≈ {result['total']} unique)_",
        "",
        f"BTC: {hits['BTC']}  |  ETH: {hits['ETH']}  |  SOL: {hits['SOL']}",
        "",
    ]
    if result["tier4"]:
        parts.append(f"⚡ *tier=4 (minute-level)*: {len(result['tier4'])} found")
        for t in result["tier4"][:5]:
            parts.append(f"  • {t}")
    elif result["tier3"]:
        parts.append(f"📈 *tier=3 (daily direction)*: {len(result['tier3'])} found")
        for t in result["tier3"][:5]:
            parts.append(f"  • {t}")
    else:
        parts.append("❌ No BTC/ETH/SOL Up or Down markets in YES range")

    parts += [
        "",
        "✅ Target markets found — /start\\_14h available"
        if result["tradeable"]
        else "⛔ No target markets — 14h run not recommended",
    ]
    return "\n".join(parts)


async def cmd_start_14h(session: aiohttp.ClientSession) -> str:
    # Guard 1: duplicate run
    pid = find_runner_pid()
    if pid is not None:
        return (
            f"⚠️ mvp\\_runner.py already running (PID `{pid}`).\n"
            "Use /stop first."
        )

    # Guard 2: target markets must exist
    result = await run_discovery_smoke(session, pages=_DISC_PAGES)
    if not result["tradeable"]:
        return (
            "⛔ *14h NOT started*\n\n"
            "No BTC/ETH/SOL Up or Down markets found.\n"
            "Run /markets again later to recheck."
        )

    # Guard 3: runner binary must exist
    runner_py = PROJECT_ROOT / "mvp_runner.py"
    venv_py   = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not runner_py.exists():
        return "❌ mvp\\_runner.py not found in project root."
    if not venv_py.exists():
        return "❌ .venv/bin/python not found. Run `python -m venv .venv && pip install -r requirements.txt`."

    # Launch (real trading permanently disabled by runner itself)
    try:
        new_pid = launch_runner()
        log.info("[SECURITY] /start_14h: dry-run launched PID=%d — real trading disabled", new_pid)
        return (
            "🚀 *14h dry-run started*\n\n"
            f"PID: `{new_pid}`\n"
            "Log: `run_14h_tg.log`\n"
            "Report: `MVP_RUN_REPORT_14h_tg.md`\n\n"
            "Use /status to monitor, /stop to terminate.\n\n"
            "⚠️ _Dry-run only. Real trading permanently disabled._"
        )
    except Exception as exc:
        log.error("[SECURITY] /start_14h launch failed: %s", exc)
        return f"❌ Failed to start runner: {exc}"


async def cmd_stop(session: aiohttp.ClientSession) -> str:
    pid = find_runner_pid()
    if pid is None:
        return "ℹ️ No mvp\\_runner.py running."

    ok = send_sigterm(pid)
    if not ok:
        return f"⚠️ PID `{pid}` not found (may have already exited)."

    log.info("[SECURITY] /stop: SIGTERM sent to PID %d (no SIGKILL)", pid)

    for _ in range(_STOP_WAIT_S):
        await asyncio.sleep(1)
        if find_runner_pid() is None:
            return f"✅ PID `{pid}` terminated gracefully. Report generated."

    return (
        f"⚠️ PID `{pid}` still running after {_STOP_WAIT_S}s.\n"
        "Runner may be finishing open positions. Check with /status."
    )

# ─────────────────────────────────────────────────────────────────────────────
# Telegram polling controller
# ─────────────────────────────────────────────────────────────────────────────

_COMMAND_HANDLERS: dict[str, object] = {
    "/status":    cmd_status,
    "/markets":   cmd_markets,
    "/start_14h": cmd_start_14h,
    "/stop":      cmd_stop,
}

_HELP_TEXT = (
    "🤖 *polymarket\\_okx dry-run controller*\n\n"
    "/status — runner state + uptime\n"
    "/markets — scan Polymarket for BTC/ETH/SOL targets\n"
    "/start\\_14h — launch 14h dry-run (if markets available)\n"
    "/stop — graceful SIGTERM\n\n"
    "_Real trading is permanently disabled._"
)


class TelegramController:
    def __init__(self, token: str, allowed_chat_id: int) -> None:
        self.token = token
        self.allowed_chat_id = allowed_chat_id
        self._base = TG_BASE.format(token=token)
        self._offset = 0

    # ── Low-level Telegram API ──────────────────────────────────────────────

    async def _tg_get(
        self, session: aiohttp.ClientSession, method: str, **params: object
    ) -> dict:
        url = f"{self._base}/{method}"
        timeout = aiohttp.ClientTimeout(total=40)
        async with session.get(url, params=params, timeout=timeout, ssl=_SSL_CTX) as r:
            return await r.json(content_type=None)

    async def _send(
        self, session: aiohttp.ClientSession, chat_id: int, text: str
    ) -> None:
        url = f"{self._base}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with session.post(
                url, json=payload, timeout=timeout, ssl=_SSL_CTX
            ) as r:
                await r.json(content_type=None)
        except Exception as exc:
            log.warning("sendMessage failed: %s", exc)

    # ── Update routing ──────────────────────────────────────────────────────

    async def handle_update(
        self, session: aiohttp.ClientSession, update: dict
    ) -> str | None:
        """
        Route one Telegram update to a command handler.
        Returns the reply string (for testing), or None if the update was ignored.
        """
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return None

        chat_id: int = msg.get("chat", {}).get("id", 0)
        text: str = (msg.get("text") or "").strip()

        # ── Security: whitelist check ──────────────────────────────────────
        if chat_id != self.allowed_chat_id:
            log.warning(
                "[SECURITY] Rejected message from chat_id=%d text=%r",
                chat_id, text,
            )
            return None

        # ── Command extraction (handle /cmd@BotName) ───────────────────────
        cmd = text.split()[0].split("@")[0].lower() if text else ""
        log.info("[CMD] chat_id=%d cmd=%r", chat_id, cmd)

        if cmd in ("/help", "/start"):
            reply = _HELP_TEXT
        elif cmd in _COMMAND_HANDLERS:
            handler = _COMMAND_HANDLERS[cmd]
            try:
                reply = await handler(session)  # type: ignore[operator]
            except Exception as exc:
                log.error("[CMD] %s raised: %s", cmd, exc, exc_info=True)
                reply = f"❌ Internal error: {exc}"
        elif cmd.startswith("/"):
            reply = f"Unknown command: `{cmd}`\n\nTry /help."
        else:
            return None   # ignore non-command plain messages

        await self._send(session, chat_id, reply)
        return reply

    # ── Main polling loop ───────────────────────────────────────────────────

    async def run_polling(self) -> None:
        """Long-poll Telegram getUpdates. Runs until cancelled."""
        conn = aiohttp.TCPConnector(ssl=_SSL_CTX)
        async with aiohttp.ClientSession(
            headers={"User-Agent": "poly-tg-ctrl/1.0"},
            connector=conn,
        ) as session:
            log.info("Telegram polling started (allowed_chat_id=%d)", self.allowed_chat_id)
            while True:
                try:
                    data = await self._tg_get(
                        session, "getUpdates",
                        offset=self._offset,
                        timeout=30,
                        allowed_updates="message",
                    )
                    for upd in data.get("result", []):
                        self._offset = upd["update_id"] + 1
                        await self.handle_update(session, upd)
                except asyncio.CancelledError:
                    log.info("Polling cancelled — shutting down.")
                    break
                except Exception as exc:
                    log.warning("Polling error: %s — retry in 5s", exc)
                    await asyncio.sleep(5)

# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    token = os.environ.get(ENV_TOKEN, "").strip()
    chat_id_raw = os.environ.get(ENV_CHAT_ID, "").strip()

    if not token:
        sys.exit(f"[ERROR] Environment variable {ENV_TOKEN!r} not set. Exiting safely.")
    if not chat_id_raw:
        sys.exit(f"[ERROR] Environment variable {ENV_CHAT_ID!r} not set. Exiting safely.")
    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        sys.exit(
            f"[ERROR] {ENV_CHAT_ID!r} must be an integer Telegram chat ID. "
            f"Got: {chat_id_raw!r}"
        )

    controller = TelegramController(token=token, allowed_chat_id=chat_id)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(controller.run_polling())

    def _on_signal(sig: int, _frame: object) -> None:
        log.info("Signal %s received — stopping controller.", sig)
        task.cancel()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
        log.info("Telegram controller stopped.")


if __name__ == "__main__":
    main()
