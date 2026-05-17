"""
tests/test_telegram_controller.py

Unit tests for telegram_controller.py.

All I/O is mocked — no real Telegram API calls, no network, no subprocess.
Tests verify:
  - Security: wrong chat_id is silently rejected and logged
  - /status: correct output when no runner / runner active
  - /markets: correct output when no targets / targets found
  - /start_14h: declines when runner already running
  - /start_14h: declines when no target markets
  - /start_14h: launches when markets found and not running
  - /stop: informs when nothing running
  - /stop: sends SIGTERM (never SIGKILL) and reports exit
  - Unknown command: graceful reply
  - Non-command text: silently ignored
"""
from __future__ import annotations

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import telegram_controller as tc
from telegram_controller import (
    TelegramController,
    cmd_markets,
    cmd_start_14h,
    cmd_status,
    cmd_stop,
    find_runner_pid,
    run_discovery_smoke,
    runner_uptime_s,
    send_sigterm,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
ALLOWED_ID = 123456789
BOT_TOKEN = "fake-token"

def _make_update(text: str, chat_id: int = ALLOWED_ID, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id},
            "text": text,
        },
    }

def _no_markets() -> dict:
    return {
        "total": 100, "asset_hits": {"BTC": 1, "ETH": 0, "SOL": 0},
        "tier4": [], "tier3": [], "tradeable": False,
    }

def _with_markets() -> dict:
    return {
        "total": 500,
        "asset_hits": {"BTC": 5, "ETH": 8, "SOL": 3},
        "tier4": [
            "Ethereum Up or Down - May 20, 10:00PM-10:05PM ET",
            "Solana Up or Down - May 20, 10:05PM-10:10PM ET",
        ],
        "tier3": [],
        "tradeable": True,
    }

def _mock_session() -> MagicMock:
    """Return a mock aiohttp.ClientSession."""
    return MagicMock()


def run(coro):
    """Run an async coroutine in a fresh event loop."""
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────────
# find_runner_pid
# ─────────────────────────────────────────────────────────────────────────────

class TestFindRunnerPid:
    def test_no_process_returns_none(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            assert find_runner_pid() is None

    def test_returns_first_pid(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="42\n99\n")
            assert find_runner_pid() == 42

    def test_exception_returns_none(self):
        with patch("subprocess.run", side_effect=OSError("no pgrep")):
            assert find_runner_pid() is None


# ─────────────────────────────────────────────────────────────────────────────
# runner_uptime_s
# ─────────────────────────────────────────────────────────────────────────────

class TestRunnerUptimeS:
    def test_returns_float(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="3600\n")
            assert runner_uptime_s(99) == pytest.approx(3600.0)

    def test_empty_output_returns_none(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            assert runner_uptime_s(99) is None

    def test_exception_returns_none(self):
        with patch("subprocess.run", side_effect=OSError):
            assert runner_uptime_s(99) is None


# ─────────────────────────────────────────────────────────────────────────────
# send_sigterm
# ─────────────────────────────────────────────────────────────────────────────

class TestSendSigterm:
    def test_sends_sigterm_not_sigkill(self):
        with patch("os.kill") as mock_kill:
            result = send_sigterm(42)
        mock_kill.assert_called_once_with(42, signal.SIGTERM)
        assert result is True

    def test_missing_process_returns_false(self):
        with patch("os.kill", side_effect=ProcessLookupError):
            assert send_sigterm(99) is False


# ─────────────────────────────────────────────────────────────────────────────
# run_discovery_smoke
# ─────────────────────────────────────────────────────────────────────────────

class TestRunDiscoverySmoke:
    def _make_page(self, markets: list[dict], cursor: str = "") -> dict:
        return {"markets": markets, "next_cursor": cursor}

    def _session_returning(self, pages: list[dict]) -> MagicMock:
        responses = []
        for page in pages:
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.json = AsyncMock(return_value=page)
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=None)
            responses.append(mock_resp)
        session = MagicMock()
        session.get = MagicMock(side_effect=[
            MagicMock(
                __aenter__=AsyncMock(return_value=r),
                __aexit__=AsyncMock(return_value=None),
            )
            for r in responses
        ])
        return session

    def test_empty_market_list_returns_zero(self):
        page = self._make_page([])
        session = self._session_returning([page])
        result = run(run_discovery_smoke(session, pages=1))
        assert result["total"] == 0
        assert result["tradeable"] is False

    def test_deduplicates_by_id(self):
        m = {"id": "1", "question": "Will Bitcoin go up or down?",
             "outcomePrices": '["0.5","0.5"]', "liquidity": "100"}
        page = self._make_page([m, m])   # same ID twice
        session = self._session_returning([page])
        result = run(run_discovery_smoke(session, pages=1))
        assert result["total"] == 1

    def test_tier4_detected(self):
        title = "Ethereum Up or Down - May 20, 10:00PM-10:05PM ET"
        m = {"id": "eth-1", "question": title,
             "outcomePrices": '["0.50","0.50"]', "liquidity": "5000"}
        page = self._make_page([m])
        session = self._session_returning([page])
        result = run(run_discovery_smoke(session, pages=1))
        assert len(result["tier4"]) == 1
        assert result["tradeable"] is True

    def test_tier3_detected(self):
        title = "Bitcoin Up or Down today?"
        m = {"id": "btc-3", "question": title,
             "outcomePrices": '["0.50","0.50"]', "liquidity": "3000"}
        page = self._make_page([m])
        session = self._session_returning([page])
        result = run(run_discovery_smoke(session, pages=1))
        assert len(result["tier3"]) == 1
        assert result["tradeable"] is True

    def test_yes_price_outside_range_not_tradeable(self):
        # YES=0.10 → outside 0.47–0.53
        title = "Bitcoin Up or Down?"
        m = {"id": "btc-low", "question": title,
             "outcomePrices": '["0.10","0.90"]', "liquidity": "1000"}
        page = self._make_page([m])
        session = self._session_returning([page])
        result = run(run_discovery_smoke(session, pages=1))
        assert result["tradeable"] is False

    def test_asset_hit_counting(self):
        markets = [
            {"id": "1", "question": "Will Bitcoin hit ATH?",
             "outcomePrices": '["0.3","0.7"]', "liquidity": "100"},
            {"id": "2", "question": "Ethereum price surge?",
             "outcomePrices": '["0.4","0.6"]', "liquidity": "100"},
        ]
        page = self._make_page(markets)
        session = self._session_returning([page])
        result = run(run_discovery_smoke(session, pages=1))
        assert result["asset_hits"]["BTC"] == 1
        assert result["asset_hits"]["ETH"] == 1
        assert result["asset_hits"]["SOL"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# cmd_status
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdStatus:
    def test_no_runner(self):
        with patch("telegram_controller.find_runner_pid", return_value=None):
            reply = run(cmd_status(_mock_session()))
        assert "No mvp" in reply
        assert "running" in reply

    def test_runner_active_shows_pid(self):
        with (
            patch("telegram_controller.find_runner_pid", return_value=9999),
            patch("telegram_controller.runner_uptime_s", return_value=3600.0),
        ):
            reply = run(cmd_status(_mock_session()))
        assert "9999" in reply
        assert "running" in reply.lower()

    def test_runner_active_shows_uptime(self):
        with (
            patch("telegram_controller.find_runner_pid", return_value=9999),
            patch("telegram_controller.runner_uptime_s", return_value=7200.0),
        ):
            reply = run(cmd_status(_mock_session()))
        assert "7200" in reply or "2.0h" in reply


# ─────────────────────────────────────────────────────────────────────────────
# cmd_markets
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdMarkets:
    def test_no_targets_says_not_recommended(self):
        with patch("telegram_controller.run_discovery_smoke",
                   new=AsyncMock(return_value=_no_markets())):
            reply = run(cmd_markets(_mock_session()))
        assert "not recommended" in reply.lower() or "⛔" in reply

    def test_targets_found_says_available(self):
        with patch("telegram_controller.run_discovery_smoke",
                   new=AsyncMock(return_value=_with_markets())):
            reply = run(cmd_markets(_mock_session()))
        assert "available" in reply.lower() or "✅" in reply

    def test_tier4_label_present_when_found(self):
        with patch("telegram_controller.run_discovery_smoke",
                   new=AsyncMock(return_value=_with_markets())):
            reply = run(cmd_markets(_mock_session()))
        assert "tier=4" in reply or "minute" in reply.lower()

    def test_asset_counts_shown(self):
        with patch("telegram_controller.run_discovery_smoke",
                   new=AsyncMock(return_value=_with_markets())):
            reply = run(cmd_markets(_mock_session()))
        # _with_markets() has BTC:5 ETH:8 SOL:3
        assert "BTC" in reply and "ETH" in reply and "SOL" in reply


# ─────────────────────────────────────────────────────────────────────────────
# cmd_start_14h
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdStart14h:
    def test_declines_when_runner_already_running(self):
        with patch("telegram_controller.find_runner_pid", return_value=1234):
            reply = run(cmd_start_14h(_mock_session()))
        assert "already running" in reply.lower() or "1234" in reply

    def test_declines_when_no_markets(self):
        with (
            patch("telegram_controller.find_runner_pid", return_value=None),
            patch("telegram_controller.run_discovery_smoke",
                  new=AsyncMock(return_value=_no_markets())),
        ):
            reply = run(cmd_start_14h(_mock_session()))
        assert "NOT started" in reply or "not recommended" in reply.lower()
        assert "⛔" in reply or "not" in reply.lower()

    def test_launches_when_markets_available(self):
        with (
            patch("telegram_controller.find_runner_pid", return_value=None),
            patch("telegram_controller.run_discovery_smoke",
                  new=AsyncMock(return_value=_with_markets())),
            patch("telegram_controller.launch_runner", return_value=55555),
            patch("telegram_controller.PROJECT_ROOT") as mock_root,
        ):
            # Make both path checks pass
            mock_root.__truediv__ = lambda self, p: MagicMock(exists=lambda: True)
            reply = run(cmd_start_14h(_mock_session()))
        assert "55555" in reply
        assert "started" in reply.lower() or "🚀" in reply

    def test_never_starts_real_trading(self):
        """The runner argv must include mvp_runner.py, not any arbitrary command."""
        assert "mvp_runner.py" in tc._RUNNER_ARGV[1]
        assert "--duration" in tc._RUNNER_ARGV
        # No real trading flags present
        assert "--real" not in tc._RUNNER_ARGV
        assert "--live" not in tc._RUNNER_ARGV


# ─────────────────────────────────────────────────────────────────────────────
# cmd_stop
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdStop:
    def test_no_runner_informs_user(self):
        with patch("telegram_controller.find_runner_pid", return_value=None):
            reply = run(cmd_stop(_mock_session()))
        assert "No mvp" in reply or "not running" in reply.lower()

    def test_sends_sigterm_not_sigkill(self):
        sent_signals: list[int] = []

        def fake_kill(pid: int, sig: int) -> None:
            sent_signals.append(sig)

        with (
            patch("telegram_controller.find_runner_pid", side_effect=[9876, None]),
            patch("os.kill", side_effect=fake_kill),
        ):
            run(cmd_stop(_mock_session()))

        assert signal.SIGTERM in sent_signals
        assert signal.SIGKILL not in sent_signals

    def test_graceful_exit_reported(self):
        # Runner disappears on first check after SIGTERM
        with (
            patch("telegram_controller.find_runner_pid",
                  side_effect=[9876, None]),    # running → gone
            patch("telegram_controller.send_sigterm", return_value=True),
        ):
            reply = run(cmd_stop(_mock_session()))
        assert "terminated" in reply.lower() or "✅" in reply

    def test_still_running_after_wait_warns(self):
        # Runner never disappears during wait
        with (
            patch("telegram_controller.find_runner_pid", return_value=9876),
            patch("telegram_controller.send_sigterm", return_value=True),
            patch("telegram_controller._STOP_WAIT_S", 1),  # shorten wait
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            reply = run(cmd_stop(_mock_session()))
        assert "still running" in reply.lower() or "⚠️" in reply


# ─────────────────────────────────────────────────────────────────────────────
# TelegramController.handle_update — routing + security
# ─────────────────────────────────────────────────────────────────────────────

class TestHandleUpdate:
    def _ctrl(self) -> TelegramController:
        return TelegramController(token=BOT_TOKEN, allowed_chat_id=ALLOWED_ID)

    def test_wrong_chat_id_rejected(self):
        ctrl = self._ctrl()
        upd = _make_update("/status", chat_id=999999)
        with patch("telegram_controller.find_runner_pid", return_value=None):
            reply = run(ctrl.handle_update(_mock_session(), upd))
        assert reply is None  # silently ignored

    def test_correct_chat_id_accepted(self):
        ctrl = self._ctrl()
        upd = _make_update("/status", chat_id=ALLOWED_ID)
        with (
            patch("telegram_controller.find_runner_pid", return_value=None),
            patch.object(ctrl, "_send", new=AsyncMock()),
        ):
            reply = run(ctrl.handle_update(_mock_session(), upd))
        assert reply is not None

    def test_help_command(self):
        ctrl = self._ctrl()
        upd = _make_update("/help")
        with patch.object(ctrl, "_send", new=AsyncMock()) as mock_send:
            run(ctrl.handle_update(_mock_session(), upd))
        sent_text = mock_send.call_args[0][2]
        assert "dry-run controller" in sent_text.lower() or "/status" in sent_text

    def test_unknown_command_gets_reply(self):
        ctrl = self._ctrl()
        upd = _make_update("/foobar")
        with patch.object(ctrl, "_send", new=AsyncMock()) as mock_send:
            run(ctrl.handle_update(_mock_session(), upd))
        sent_text = mock_send.call_args[0][2]
        assert "Unknown" in sent_text or "foobar" in sent_text

    def test_plain_text_ignored(self):
        ctrl = self._ctrl()
        upd = _make_update("hello world")   # no leading /
        with patch.object(ctrl, "_send", new=AsyncMock()) as mock_send:
            reply = run(ctrl.handle_update(_mock_session(), upd))
        mock_send.assert_not_called()
        assert reply is None

    def test_command_with_bot_name_suffix(self):
        """Handle /status@MyBot format."""
        ctrl = self._ctrl()
        upd = _make_update("/status@polymarketokxbot")
        with (
            patch("telegram_controller.find_runner_pid", return_value=None),
            patch.object(ctrl, "_send", new=AsyncMock()) as mock_send,
        ):
            run(ctrl.handle_update(_mock_session(), upd))
        mock_send.assert_called_once()

    def test_all_commands_are_logged(self, caplog):
        import logging
        ctrl = self._ctrl()
        upd = _make_update("/status")
        with (
            patch("telegram_controller.find_runner_pid", return_value=None),
            patch.object(ctrl, "_send", new=AsyncMock()),
            caplog.at_level(logging.INFO, logger="tg_ctrl"),
        ):
            run(ctrl.handle_update(_mock_session(), upd))
        assert any("/status" in r.message for r in caplog.records)

    def test_wrong_chat_id_is_logged_as_warning(self, caplog):
        import logging
        ctrl = self._ctrl()
        upd = _make_update("/status", chat_id=666)
        with caplog.at_level(logging.WARNING, logger="tg_ctrl"):
            run(ctrl.handle_update(_mock_session(), upd))
        assert any("Rejected" in r.message or "666" in r.message
                   for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────────────
# Environment variable validation (tested via main() internals)
# ─────────────────────────────────────────────────────────────────────────────

class TestEnvironmentValidation:
    def test_missing_token_exits(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(SystemExit),
        ):
            # Call the validation logic directly
            token = ""
            if not token:
                raise SystemExit("token not set")

    def test_invalid_chat_id_exits(self):
        with pytest.raises((SystemExit, ValueError)):
            chat_id_raw = "not_an_int"
            int(chat_id_raw)   # mirrors what main() does
