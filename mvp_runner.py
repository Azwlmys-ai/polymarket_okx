#!/usr/bin/env python3
"""
mvp_runner.py — Polymarket × OKX 极速套利验证 MVP

一条命令启动：
    python mvp_runner.py                    # 跑 30 分钟
    python mvp_runner.py --duration 600     # 跑 10 分钟

输出：
    mvp_run.log          实时日志
    MVP_RUN_REPORT.md    结束后自动生成报告

禁止真实下单。只做 paper trade。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import os
import ssl

import aiohttp
import certifi

from src.report import compute_per_asset_trade_stats, format_per_asset_trade_table


def _make_ssl_ctx() -> ssl.SSLContext:
    """
    返回适合当前网络环境的 SSL 上下文。
    - 默认使用 certifi 证书（解决 Homebrew Python 根证书缺失）
    - 若检测到代理 SSL 拦截（self-signed），自动降级为跳过验证
      （可通过环境变量 DISABLE_SSL_VERIFY=1 强制启用）
    """
    if os.environ.get("DISABLE_SSL_VERIFY", "").strip() == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx = ssl.create_default_context(cafile=certifi.where())
    return ctx


_SSL_CTX = _make_ssl_ctx()

# ─────────────────────────────────────────
# 配置
# ─────────────────────────────────────────
OKX_WS_URLS = [
    "wss://ws.okx.com:8443/ws/v5/public",
    "wss://wsaws.okx.com:8443/ws/v5/public",   # AWS 备用
    "wss://wsap.okx.com:8443/ws/v5/public",    # AP 备用
]
OKX_SYMBOLS         = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
POLY_GAMMA_URL      = "https://gamma-api.polymarket.com"
POLY_CLOB_URL       = "https://clob.polymarket.com"
POLY_POLL_S         = 8.0          # 价格刷新间隔（秒）
POLY_DISCOVERY_S    = 600.0        # 全量市场扫描间隔（秒）
POLY_TOP_N          = 15           # 每个资产缓存的 top N 市场
MOVE_THRESHOLD_PCT  = 0.001        # OKX 价格变动阈值（0.1%）
SIGNAL_WINDOW_S     = 60.0         # 用过去多少秒计算价格变动
SIGNAL_COOLDOWN_S   = 30.0         # 同资产信号冷却时间（秒）
HOLD_WINDOW_S       = 300.0        # paper trade 持仓窗口（5 分钟）
SLIPPAGE_PCT        = 0.002        # 滑点估算（0.2%）
FEE_PCT             = 0.002        # 手续费估算（0.2%）
STARTUP_TIMEOUT_S   = 120.0        # 市场缓存就绪等待上限（秒）
MIN_YES_PRICE       = 0.47         # YES 价格低于此不交易（方案A收窄，原0.45）
MAX_YES_PRICE       = 0.53         # YES 价格高于此不交易（方案A收窄，原0.55）
STOP_LOSS_YES_PRICE = 0.40         # YES 价格跌至此绝对值立即 paper close
STOP_LOSS_PCT       = 0.12         # YES 价格相对入场价下跌 >=12% 时 paper close
MIN_TIME_REMAINING_S = 300.0       # 市场至少还有 5 分钟才允许入场
INITIAL_CASH        = 1000.0       # 初始模拟资金（USDC）
RISK_PER_TRADE_PCT  = 0.02         # 每笔最大风险（2%）

# Step 9 — 并发扫描超时
SCAN_TIMEOUT_OKX_S  = 6.0          # 单次 OKX REST 轮询超时（秒）
SCAN_TIMEOUT_POLY_S = 15.0         # 单批 Polymarket 价格刷新超时（秒）

# OKX → Polymarket 资产关键词映射（用于全量扫描时过滤）
# 使用完整词避免 "hegseth" 匹配 "eth" 等误判
ASSET_KEYWORDS: dict[str, list[str]] = {
    "BTC-USDT": ["bitcoin", "btc"],
    "ETH-USDT": ["ethereum"],
    "SOL-USDT": ["solana"],
}

# ─────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────
@dataclass
class OkxTick:
    ts_ms: int
    market_id: str
    last: float
    bid: Optional[float]
    ask: Optional[float]

@dataclass
class PolyMarket:
    market_id: str
    symbol: str
    yes_price: float
    no_price: float
    ts_ms: int

@dataclass
class Signal:
    ts_ms: int
    asset: str                  # BTC / ETH / SOL
    okx_market_id: str
    okx_price_before: float
    okx_price_now: float
    pct_move: float
    poly_market_id: str
    poly_symbol: str
    poly_yes_price: float
    acted: bool = False
    no_trade_reason: str = ""

@dataclass
class Position:
    opened_ts_ms: int
    asset: str
    okx_market_id: str
    poly_market_id: str
    poly_symbol: str
    entry_yes_price: float      # 含滑点
    raw_yes_price: float        # 原始价格
    notional: float
    quantity: float
    fees: float
    signal_pct_move: float

@dataclass
class ClosedPosition:
    pos: Position
    closed_ts_ms: int
    exit_yes_price: Optional[float]
    pnl: Optional[float]
    close_reason: str           # "hold_window" | "stop" | "shutdown"

# ─────────────────────────────────────────
# 全局运行状态
# ─────────────────────────────────────────
@dataclass
class RunState:
    start_ts: float = field(default_factory=time.monotonic)
    start_wall: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # 行情
    okx_history: dict[str, deque] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=500))
    )   # market_id → deque of OkxTick
    poly_latest: dict[str, PolyMarket] = field(default_factory=dict)
    # market_id → PolyMarket

    # 交易
    cash: float = INITIAL_CASH
    open_positions: list[Position] = field(default_factory=list)
    closed_positions: list[ClosedPosition] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)

    # Polymarket 市场缓存（discovery 结果）
    # market_id → (asset, liquidity, tier, end_ts)
    # tier: 4=分钟区间 3=日线 2=本月 1=长期；end_ts=0.0 表示未知
    poly_cached_ids: dict[str, tuple[str, float, int, float]] = field(default_factory=dict)
    poly_discovery_done: asyncio.Event = field(default_factory=asyncio.Event)

    # 信号冷却：asset → 上次触发时间（monotonic）
    signal_last_ts: dict[str, float] = field(default_factory=dict)

    # 统计
    okx_ticks: int = 0
    poly_polls: int = 0
    poly_markets_found: int = 0
    poly_discovery_runs: int = 0
    reconnects: int = 0
    errors: int = 0
    stop_loss_count: int = 0
    no_trade_reasons: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    # OKX 数据源控制
    ws_consecutive_fails: int = 0   # 连续 WS 失败次数
    using_rest: bool = False        # True = 已切换到 REST fallback

    # 控制
    shutdown: asyncio.Event = field(default_factory=asyncio.Event)

state = RunState()

# ─────────────────────────────────────────
# 日志
# ─────────────────────────────────────────
log = logging.getLogger("mvp")

def _setup_logging(log_path: str = "mvp_run.log") -> None:
    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    datefmt = "%H:%M:%S"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt=datefmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, mode="a", encoding="utf-8"),
        ],
    )
    # 降低 aiohttp 噪音
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

# ─────────────────────────────────────────
# OKX WebSocket 任务（自动重连）
# ─────────────────────────────────────────
async def okx_task(url_index: int = 0) -> None:
    """持续连接 OKX WS，断线自动重连（尝试备用 URL）。"""
    url_idx = url_index
    delay = 2.0

    while not state.shutdown.is_set():
        url = OKX_WS_URLS[url_idx % len(OKX_WS_URLS)]
        try:
            log.info("[OKX] 连接 %s", url)
            connector = aiohttp.TCPConnector(ssl=_SSL_CTX)
            async with aiohttp.ClientSession(connector=connector) as session:
                timeout = aiohttp.ClientTimeout(connect=15)
                async with session.ws_connect(url, timeout=timeout) as ws:
                    # 订阅
                    args = [{"channel": "tickers", "instId": s} for s in OKX_SYMBOLS]
                    await ws.send_str(json.dumps({"op": "subscribe", "args": args}))
                    log.info("[OKX] 已订阅: %s", OKX_SYMBOLS)
                    delay = 2.0  # 成功连接后重置延迟
                    state.ws_consecutive_fails = 0  # 连接成功，重置失败计数

                    # ping 任务
                    async def _ping():
                        while not ws.closed:
                            await asyncio.sleep(20)
                            if not ws.closed:
                                await ws.send_str("ping")
                    ping = asyncio.create_task(_ping())

                    try:
                        async for msg in ws:
                            if state.shutdown.is_set():
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                if msg.data == "pong":
                                    continue
                                _handle_okx_msg(msg.data)
                            elif msg.type in (
                                aiohttp.WSMsgType.ERROR,
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSED,
                            ):
                                break
                    finally:
                        ping.cancel()

        except Exception as exc:  # noqa: BLE001
            state.reconnects += 1
            state.errors += 1
            state.ws_consecutive_fails += 1
            # 轮换备用 URL
            url_idx += 1
            log.warning(
                "[OKX] 断线 [%s] %s — %.0fs 后重连 (尝试 %s) [连续失败 %d/3]",
                type(exc).__name__, repr(exc), delay,
                OKX_WS_URLS[url_idx % len(OKX_WS_URLS)],
                state.ws_consecutive_fails,
            )
            if state.ws_consecutive_fails >= 3:
                log.warning("[OKX] WS 连续失败 3 次，切换到 REST polling fallback")
                state.using_rest = True
                return   # 退出 okx_task，由 okx_rest_task 接管
            try:
                await asyncio.wait_for(
                    asyncio.shield(state.shutdown.wait()), timeout=delay
                )
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 30.0)


def _handle_okx_msg(raw: str) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return

    if msg.get("event"):
        return

    arg = msg.get("arg", {})
    if arg.get("channel") != "tickers":
        return

    data_list = msg.get("data")
    if not data_list:
        return

    item = data_list[0]

    def _f(k: str) -> Optional[float]:
        v = item.get(k)
        try:
            f = float(v)
            return f if f != 0.0 else None
        except (TypeError, ValueError):
            return None

    inst_id: str = item.get("instId", arg.get("instId", ""))
    if not inst_id:
        return
    ts_str = item.get("ts")
    ts_ms = int(ts_str) if ts_str else int(time.time() * 1000)
    last = _f("last")
    if last is None:
        return

    tick = OkxTick(
        ts_ms=ts_ms,
        market_id=inst_id,
        last=last,
        bid=_f("bidPx"),
        ask=_f("askPx"),
    )
    state.okx_history[inst_id].append(tick)
    state.okx_ticks += 1

# ─────────────────────────────────────────
# REST Polling Fallback（WS 失败 3 次后启用）
# 优先级：OKX REST → OKX AWS REST → Binance REST
# ─────────────────────────────────────────

# OKX REST 备用端点
_OKX_REST_URLS = [
    "https://www.okx.com/api/v5/market/ticker",
    "https://aws.okx.com/api/v5/market/ticker",
]

# Binance 最终兜底
_BINANCE_REST_URL = "https://api.binance.com/api/v3/ticker/bookTicker"
_BINANCE_SYMBOL_MAP = {
    "BTC-USDT": "BTCUSDT",
    "ETH-USDT": "ETHUSDT",
    "SOL-USDT": "SOLUSDT",
}


async def okx_rest_task() -> None:
    """REST polling fallback — 等待 using_rest=True 后每 1 秒轮询一次。"""
    connector = aiohttp.TCPConnector(ssl=_SSL_CTX)
    async with aiohttp.ClientSession(
        headers={"User-Agent": "poly-okx-mvp/1.0"},
        connector=connector,
    ) as session:
        while not state.shutdown.is_set():
            if not state.using_rest:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(state.shutdown.wait()), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    pass
                continue

            # 并发轮询所有 OKX 交易对（asyncio.gather，单边超时保护）
            if state.shutdown.is_set():
                break
            t0_okx = time.monotonic()
            okx_results = await asyncio.gather(
                *[
                    asyncio.wait_for(
                        _poll_price(session, sym),
                        timeout=SCAN_TIMEOUT_OKX_S,
                    )
                    for sym in OKX_SYMBOLS
                ],
                return_exceptions=True,
            )
            okx_ok = sum(1 for r in okx_results if not isinstance(r, Exception))
            okx_fail = len(okx_results) - okx_ok
            okx_elapsed = time.monotonic() - t0_okx
            if okx_fail:
                for sym, r in zip(OKX_SYMBOLS, okx_results):
                    if isinstance(r, Exception):
                        state.errors += 1
                        log.warning(
                            "[OKX-REST] %s 失败 [%s] %s",
                            sym, type(r).__name__, repr(r),
                        )
            log.info(
                "[OKX-REST] 并发轮询 %d/%d 成功 | %.2fs",
                okx_ok, len(OKX_SYMBOLS), okx_elapsed,
            )

            # 一轮轮询完后等 1 秒
            try:
                await asyncio.wait_for(
                    asyncio.shield(state.shutdown.wait()), timeout=1.0
                )
            except asyncio.TimeoutError:
                pass


async def _poll_price(session: aiohttp.ClientSession, inst_id: str) -> None:
    """依次尝试 OKX REST → Binance REST，成功后写入 okx_history。"""
    short_timeout = aiohttp.ClientTimeout(total=5)

    # ── 1. 尝试各 OKX REST 端点 ──
    for rest_url in _OKX_REST_URLS:
        try:
            async with session.get(
                rest_url, params={"instId": inst_id}, timeout=short_timeout
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
            items = data.get("data") or []
            if not items:
                continue
            item = items[0]
            last = _safe_float(item.get("last"))
            if last is None:
                continue
            _push_tick(inst_id, last, _safe_float(item.get("bidPx")), _safe_float(item.get("askPx")))
            log.info("[OKX-REST] %s price=%.4f", inst_id, last)
            return
        except Exception:  # noqa: BLE001
            continue

    # ── 2. OKX 全部失败，用 Binance 兜底 ──
    bnb_sym = _BINANCE_SYMBOL_MAP.get(inst_id)
    if not bnb_sym:
        return
    async with session.get(
        _BINANCE_REST_URL, params={"symbol": bnb_sym}, timeout=short_timeout
    ) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    bid = _safe_float(data.get("bidPrice"))
    ask = _safe_float(data.get("askPrice"))
    last = ((bid or 0) + (ask or 0)) / 2 if bid and ask else (bid or ask)
    if last is None or last == 0:
        return
    _push_tick(inst_id, last, bid, ask)
    log.info("[OKX-REST] %s price=%.4f (via Binance)", inst_id, last)


def _safe_float(v: object) -> Optional[float]:
    try:
        f = float(v)  # type: ignore[arg-type]
        return f if f != 0.0 else None
    except (TypeError, ValueError):
        return None


def _push_tick(inst_id: str, last: float, bid: Optional[float], ask: Optional[float]) -> None:
    tick = OkxTick(
        ts_ms=int(time.time() * 1000),
        market_id=inst_id,
        last=last,
        bid=bid,
        ask=ask,
    )
    state.okx_history[inst_id].append(tick)
    state.okx_ticks += 1


# ─────────────────────────────────────────
# Polymarket 全量市场发现任务（启动 + 每 10 分钟）
# ─────────────────────────────────────────
async def poly_discovery_task() -> None:
    """全量扫描 Polymarket 市场，缓存每资产 top N（按流动性）。"""
    connector = aiohttp.TCPConnector(ssl=_SSL_CTX)
    async with aiohttp.ClientSession(
        headers={"User-Agent": "poly-okx-mvp/1.0"},
        connector=connector,
    ) as session:
        while not state.shutdown.is_set():
            try:
                await _discover_poly_markets(session)
                state.poly_discovery_runs += 1
            except Exception as exc:  # noqa: BLE001
                state.errors += 1
                log.warning("[POLY-DISC] 扫描失败 [%s] %s", type(exc).__name__, repr(exc))
            finally:
                state.poly_discovery_done.set()  # 首次完成后解锁价格刷新任务

            try:
                await asyncio.wait_for(
                    asyncio.shield(state.shutdown.wait()), timeout=POLY_DISCOVERY_S
                )
            except asyncio.TimeoutError:
                pass


async def _discover_poly_markets(session: aiohttp.ClientSession) -> None:
    """翻页扫描全部活跃市场，筛选加密相关，按流动性取 top N 写入缓存。"""
    # Gamma API migrated to keyset pagination on 2026-05-01.
    # The old offset endpoint returns HTTP 422 past offset 10000.
    keyset_url = f"{POLY_GAMMA_URL}/markets/keyset"
    timeout = aiohttp.ClientTimeout(total=20)
    # asset → list of (liquidity, market_id, question)
    candidates: dict[str, list[tuple[float, str, str]]] = {k: [] for k in ASSET_KEYWORDS}

    BATCH = 100
    MAX_PAGES = 200          # hard guard: 200 × 100 = 20,000 markets max
    next_cursor: str = ""
    pages = 0
    total_scanned = 0
    while not state.shutdown.is_set() and pages < MAX_PAGES:
        params: dict[str, str] = {"limit": str(BATCH), "active": "true", "closed": "false"}
        if next_cursor:
            params["next_cursor"] = next_cursor
        async with session.get(keyset_url, params=params, timeout=timeout) as resp:
            resp.raise_for_status()
            page_data = await resp.json(content_type=None)
        items = page_data.get("markets", []) if isinstance(page_data, dict) else page_data
        if not items:
            break
        total_scanned += len(items)
        pages += 1

        now_ts = time.time()
        for m in items:
            q: str = m.get("question") or m.get("title") or ""
            q_low = q.lower()
            mid = str(m.get("id") or m.get("conditionId") or "")
            if not mid:
                continue

            # 解析 endDate → Unix 时间戳（0.0 = 未知）
            end_ts = 0.0
            end_date = m.get("endDate") or m.get("end_date_iso") or ""
            if end_date:
                try:
                    from datetime import datetime, timezone
                    end_dt = datetime.fromisoformat(end_date.rstrip("Z")).replace(tzinfo=timezone.utc)
                    end_ts = end_dt.timestamp()
                    if end_ts < now_ts + MIN_TIME_REMAINING_S:
                        continue  # 已过期或剩余时间不足，跳过
                except (ValueError, TypeError):
                    pass

            liq = float(m.get("liquidity") or 0)
            yes_price = _parse_yes_price(m)
            if yes_price is None or not (MIN_YES_PRICE < yes_price < MAX_YES_PRICE):
                continue

            # 短期优先打分（tier 越高越优先）：
            #   4 = 分钟区间市场（"10:25PM-10:30PM" 等）
            #   3 = 日线 "up or down" 方向市场
            #   2 = 本月内到期（"in may" 等）
            #   1 = 长期市场
            import re as _re
            tier = 1
            if ("up or down" in q_low or "higher or lower" in q_low) and _re.search(
                r"\d{1,2}:\d{2}(am|pm)-\d{1,2}:\d{2}(am|pm)", q_low
            ):
                tier = 4
            elif "up or down" in q_low or "higher or lower" in q_low:
                tier = 3
            elif any(kw in q_low for kw in ["in may", "may 2026", "this week", "this month", "by may"]):
                tier = 2

            for asset, kws in ASSET_KEYWORDS.items():
                if any(kw in q_low for kw in kws):
                    score = tier * 1_000_000_000 + liq
                    candidates[asset].append((score, liq, tier, end_ts, mid, q[:120]))
                    break

        next_cursor = page_data.get("next_cursor", "") if isinstance(page_data, dict) else ""
        if not next_cursor:
            break

    # 每资产取 top N（短期优先 + 流动性）
    new_cache: dict[str, tuple[str, float, int, float]] = {}  # market_id → (asset, liq, tier, end_ts)
    total_cached = 0
    for asset, cands in candidates.items():
        cands.sort(key=lambda x: -x[0])
        top = cands[:POLY_TOP_N]
        for score, liq, tier, end_ts, mid, q in top:
            new_cache[mid] = (asset, liq, tier, end_ts)
        total_cached += len(top)
        if top:
            tier_labels = {4: "分钟区间", 3: "日线", 2: "本月", 1: "长期"}
            log.info(
                "[POLY-DISC] %s: %d 个候选 → 缓存 %d 个 | 最高: [%s] $%.0f liq | %s",
                asset, len(cands), len(top),
                tier_labels.get(top[0][2], "?"), top[0][1], top[0][5][:55],
            )

    state.poly_cached_ids = new_cache
    log.info(
        "[POLY-DISC] 扫描完成: %d 个市场 → 缓存 %d 个加密市场",
        total_scanned, total_cached,
    )


def _parse_yes_price(market: dict) -> Optional[float]:
    op_raw = market.get("outcomePrices")
    if op_raw:
        try:
            prices = op_raw if isinstance(op_raw, list) else json.loads(op_raw)
            v = float(prices[0]) if prices else None
            return v if v and v > 0 else None
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    for tok in (market.get("tokens") or []):
        if str(tok.get("outcome", "")).upper() == "YES":
            try:
                v = float(tok.get("price", 0))
                return v if v > 0 else None
            except (ValueError, TypeError):
                pass
    return None


# ─────────────────────────────────────────
# Polymarket 价格刷新任务（每 POLY_POLL_S 秒）
# ─────────────────────────────────────────
async def poly_task() -> None:
    """等待首次发现完成后，每 POLY_POLL_S 秒批量刷新缓存市场的价格。"""
    connector = aiohttp.TCPConnector(ssl=_SSL_CTX)
    async with aiohttp.ClientSession(
        headers={"User-Agent": "poly-okx-mvp/1.0"},
        connector=connector,
    ) as session:
        # 等待首次 discovery 完成
        log.info("[POLY] 等待市场发现完成…")
        try:
            await asyncio.wait_for(
                asyncio.shield(state.poly_discovery_done.wait()), timeout=120.0
            )
        except asyncio.TimeoutError:
            log.warning("[POLY] 市场发现超时，直接开始轮询")

        while not state.shutdown.is_set():
            t0 = time.monotonic()
            try:
                await _poll_poly_prices(session)
                state.poly_polls += 1
            except Exception as exc:  # noqa: BLE001
                state.errors += 1
                log.warning("[POLY] 价格刷新失败 [%s] %s", type(exc).__name__, repr(exc))

            elapsed = time.monotonic() - t0
            sleep = max(0.0, POLY_POLL_S - elapsed)
            try:
                await asyncio.wait_for(
                    asyncio.shield(state.shutdown.wait()), timeout=sleep
                )
            except asyncio.TimeoutError:
                pass


async def _fetch_poly_batch(
    session: aiohttp.ClientSession,
    batch: list[str],
    ts_ms: int,
) -> list[tuple[str, PolyMarket]]:
    """单批次 Polymarket 价格拉取；返回 (market_id, PolyMarket) 列表。
    异常由调用方 asyncio.gather(return_exceptions=True) 捕获，不在此处吞掉。
    """
    url = f"{POLY_GAMMA_URL}/markets"
    timeout = aiohttp.ClientTimeout(total=SCAN_TIMEOUT_POLY_S)
    params = [("id", mid) for mid in batch]
    async with session.get(url, params=params, timeout=timeout) as resp:
        resp.raise_for_status()
        items = await resp.json(content_type=None)
    if not isinstance(items, list):
        return []
    results: list[tuple[str, PolyMarket]] = []
    for m in items:
        mid = str(m.get("id") or "")
        q: str = m.get("question") or m.get("title") or ""
        yes_price = _parse_yes_price(m)
        if yes_price is None:
            continue
        results.append((mid, PolyMarket(
            market_id=mid,
            symbol=q[:120],
            yes_price=yes_price,
            no_price=1.0 - yes_price,
            ts_ms=ts_ms,
        )))
    return results


async def _poll_poly_prices(session: aiohttp.ClientSession) -> None:
    """并发拉取所有缓存市场的最新价格（asyncio.gather，单批超时保护）。
    连续 3 次无价格的市场自动从缓存中剔除（已结算）。
    """
    cached_ids = list(state.poly_cached_ids.keys())
    if not cached_ids:
        return

    BATCH = 50
    ts_ms = int(time.time() * 1000)
    t0 = time.monotonic()

    batches = [cached_ids[i : i + BATCH] for i in range(0, len(cached_ids), BATCH)]
    raw_results = await asyncio.gather(
        *[
            asyncio.wait_for(
                _fetch_poly_batch(session, b, ts_ms),
                timeout=SCAN_TIMEOUT_POLY_S + 2.0,  # 外层留 2s 余量
            )
            for b in batches
        ],
        return_exceptions=True,
    )

    found = 0
    returned_ids: set[str] = set()
    ok_batches = 0
    fail_batches = 0

    for r in raw_results:
        if isinstance(r, Exception):
            fail_batches += 1
            state.errors += 1
            log.warning("[POLY] 批次失败 [%s] %s", type(r).__name__, repr(r))
        else:
            ok_batches += 1
            for mid, pm in r:
                state.poly_latest[mid] = pm
                returned_ids.add(mid)
                found += 1

    elapsed = time.monotonic() - t0
    log.info(
        "[POLY] 价格刷新: %d 市场 | %d/%d 批成功 | %.2fs",
        found, ok_batches, len(batches), elapsed,
    )

    # 1) 按 endDate 立即剔除已过期市场
    now_ts = time.time()
    for mid in list(state.poly_cached_ids.keys()):
        entry = state.poly_cached_ids.get(mid)
        if entry and entry[3] > 0 and entry[3] < now_ts:
            state.poly_cached_ids.pop(mid, None)
            state.poly_latest.pop(mid, None)
            log.info("[POLY] 已到期剔除 id=%s (asset=%s end=%s)",
                     mid, entry[0],
                     datetime.fromtimestamp(entry[3], tz=timezone.utc).strftime("%H:%M UTC"))

    # 2) 连续无价格（API 不再返回）剔除
    missing = set(cached_ids) - returned_ids
    if missing:
        if not hasattr(state, "_poly_miss_count"):
            state._poly_miss_count = defaultdict(int)  # type: ignore[attr-defined]
        for mid in missing:
            state._poly_miss_count[mid] += 1  # type: ignore[attr-defined]
            if state._poly_miss_count[mid] >= 3:  # type: ignore[attr-defined]
                evicted = state.poly_cached_ids.pop(mid, None)
                if evicted:
                    log.info("[POLY] 无价格剔除 id=%s (asset=%s)", mid, evicted[0])
        for mid in returned_ids:
            state._poly_miss_count[mid] = 0  # type: ignore[attr-defined]

    state.poly_markets_found = found
    log.debug("[POLY] 价格刷新: %d/%d 个市场", found, len(cached_ids))

# ─────────────────────────────────────────
# 信号检测 + Paper Trade 任务
# ─────────────────────────────────────────
async def strategy_task() -> None:
    """每秒检测信号、管理持仓。"""
    log.info("[STRAT] 等待 Polymarket 市场缓存就绪…")
    try:
        await asyncio.wait_for(
            asyncio.shield(state.poly_discovery_done.wait()),
            timeout=STARTUP_TIMEOUT_S,
        )
        log.info("[STRAT] 市场缓存已就绪（缓存 %d 个市场），信号引擎启动",
                 len(state.poly_cached_ids))
    except asyncio.TimeoutError:
        log.error("[STRAT] 市场缓存等待超时 (%.0fs)，终止运行", STARTUP_TIMEOUT_S)
        state.shutdown.set()
        return

    while not state.shutdown.is_set():
        try:
            _detect_signals()
            _manage_positions()
        except Exception as exc:  # noqa: BLE001
            state.errors += 1
            log.error("[STRAT] 异常: %s", repr(exc))
        try:
            await asyncio.wait_for(
                asyncio.shield(state.shutdown.wait()), timeout=1.0
            )
        except asyncio.TimeoutError:
            pass


def _detect_signals() -> None:
    now_ms = int(time.time() * 1000)
    now_mono = time.monotonic()
    window_ms = int(SIGNAL_WINDOW_S * 1000)

    for okx_mid, kws in ASSET_KEYWORDS.items():
        asset_name = okx_mid.split("-")[0]

        hist = state.okx_history.get(okx_mid)
        if not hist or len(hist) < 2:
            continue

        latest = hist[-1]
        cutoff_ms = now_ms - window_ms
        baseline: Optional[OkxTick] = None
        for tick in hist:
            if tick.ts_ms >= cutoff_ms:
                baseline = tick
                break

        if baseline is None or baseline.last == 0:
            continue

        pct = (latest.last - baseline.last) / baseline.last

        if abs(pct) < MOVE_THRESHOLD_PCT:
            continue

        # ── Fix 1: 冷却过滤 ──────────────────────────
        last_ts = state.signal_last_ts.get(okx_mid, 0.0)
        if now_mono - last_ts < SIGNAL_COOLDOWN_S:
            continue
        state.signal_last_ts[okx_mid] = now_mono

        # ── Fix 3: 只做多，下跌静默跳过（不再刷日志）──
        if pct <= 0:
            state.no_trade_reasons["okx_下跌_跳过"] += 1
            continue

        # 仅在确认方向后才打日志
        log.info(
            "[SIGNAL] %s ↑ %.2f%% (%.4f → %.4f) | Poly 市场数: %d",
            okx_mid, pct * 100,
            baseline.last, latest.last, state.poly_markets_found,
        )

        # ── Fix 2+: 匹配缓存中 tier 最高的活跃市场 ────
        # 跳过已过期或 60 秒内即将结算的市场
        best_pm: Optional[PolyMarket] = None
        best_tier = -1
        entry_now = time.time()
        for mid, pm in state.poly_latest.items():
            cache_entry = state.poly_cached_ids.get(mid)
            if cache_entry is None:
                continue
            _, _, tier, end_ts = cache_entry
            # 已过期或剩余时间不足 MIN_TIME_REMAINING_S → 跳过
            if end_ts > 0 and entry_now > end_ts - MIN_TIME_REMAINING_S:
                continue
            sym_low = pm.symbol.lower()
            if any(kw in sym_low for kw in kws) and tier > best_tier:
                best_pm = pm
                best_tier = tier
        matched_pm = best_pm
        if matched_pm is not None:
            log.info("[SIGNAL] 选中 [tier=%d] %s", best_tier, matched_pm.symbol[:70])

        if matched_pm is None:
            state.no_trade_reasons["无匹配Poly市场"] += 1
            log.info("[SIGNAL] 无活跃 Polymarket 市场，跳过")
            continue

        if matched_pm.yes_price < MIN_YES_PRICE or matched_pm.yes_price > MAX_YES_PRICE:
            state.no_trade_reasons["YES价格超范围"] += 1
            continue

        already_open = any(p.asset == asset_name for p in state.open_positions)
        if already_open:
            state.no_trade_reasons["已有持仓"] += 1
            continue

        sig = Signal(
            ts_ms=now_ms,
            asset=asset_name,
            okx_market_id=okx_mid,
            okx_price_before=baseline.last,
            okx_price_now=latest.last,
            pct_move=pct,
            poly_market_id=matched_pm.market_id,
            poly_symbol=matched_pm.symbol,
            poly_yes_price=matched_pm.yes_price,
        )
        state.signals.append(sig)
        _open_position(sig, matched_pm)


def _open_position(sig: Signal, pm: PolyMarket) -> None:
    # 含滑点的入场价
    entry = pm.yes_price * (1 + SLIPPAGE_PCT)
    if entry >= 1.0:
        state.no_trade_reasons["滑点后价格≥1"] += 1
        sig.no_trade_reason = "滑点后价格>=1"
        return

    notional = state.cash * RISK_PER_TRADE_PCT
    if notional < 0.01:
        state.no_trade_reasons["资金不足"] += 1
        sig.no_trade_reason = "资金不足"
        return

    quantity = notional / entry
    fees = notional * FEE_PCT
    state.cash -= (notional + fees)

    pos = Position(
        opened_ts_ms=sig.ts_ms,
        asset=sig.asset,
        okx_market_id=sig.okx_market_id,
        poly_market_id=sig.poly_market_id,
        poly_symbol=sig.poly_symbol,
        entry_yes_price=entry,
        raw_yes_price=pm.yes_price,
        notional=notional,
        quantity=quantity,
        fees=fees,
        signal_pct_move=sig.pct_move,
    )
    state.open_positions.append(pos)
    sig.acted = True

    log.info(
        "[OPEN]  %s | YES %.4f→%.4f(+slippage) | notional=%.2f USDC | %s",
        sig.asset, pm.yes_price, entry, notional, pm.symbol[:60],
    )


def _manage_positions() -> None:
    now_ms = int(time.time() * 1000)
    hold_ms = int(HOLD_WINDOW_S * 1000)
    to_close: list[int] = []

    for i, pos in enumerate(state.open_positions):
        pm = state.poly_latest.get(pos.poly_market_id)

        # --- 止损检查（不受持仓窗口限制）---
        # 相对止损：YES 价格相对入场价下跌 >= STOP_LOSS_PCT
        if pm is not None and pm.yes_price <= pos.entry_yes_price * (1.0 - STOP_LOSS_PCT):
            exit_price = pm.yes_price
            pnl = (exit_price - pos.entry_yes_price) * pos.quantity - pos.fees
            closed = ClosedPosition(
                pos=pos,
                closed_ts_ms=now_ms,
                exit_yes_price=exit_price,
                pnl=pnl,
                close_reason="stop_loss",
            )
            state.closed_positions.append(closed)
            state.cash += pnl + pos.notional
            state.stop_loss_count += 1
            to_close.append(i)
            _log_close(closed)
            continue  # already handled, skip absolute stop and hold_window check

        # 绝对止损：YES 价格跌至固定下限
        if pm is not None and pm.yes_price <= STOP_LOSS_YES_PRICE:
            exit_price = pm.yes_price
            pnl = (exit_price - pos.entry_yes_price) * pos.quantity - pos.fees
            closed = ClosedPosition(
                pos=pos,
                closed_ts_ms=now_ms,
                exit_yes_price=exit_price,
                pnl=pnl,
                close_reason="stop_loss_yes_price",
            )
            state.closed_positions.append(closed)
            state.cash += pnl + pos.notional
            state.stop_loss_count += 1
            to_close.append(i)
            _log_close(closed)
            continue  # already handled, skip hold_window check

        # --- 持仓窗口到期检查 ---
        if now_ms - pos.opened_ts_ms < hold_ms:
            continue   # 还在持仓窗口内

        exit_price = pm.yes_price if pm else None

        pnl_hw: Optional[float] = None
        if exit_price is not None:
            pnl_hw = (exit_price - pos.entry_yes_price) * pos.quantity - pos.fees

        closed = ClosedPosition(
            pos=pos,
            closed_ts_ms=now_ms,
            exit_yes_price=exit_price,
            pnl=pnl_hw,
            close_reason="hold_window_expired",
        )
        state.closed_positions.append(closed)

        if pnl_hw is not None:
            state.cash += pnl_hw + pos.notional  # 收回本金 + 盈亏
        else:
            state.cash += pos.notional             # 无法获取价格，保守处理

        to_close.append(i)
        _log_close(closed)

    for i in reversed(to_close):
        state.open_positions.pop(i)


def _log_close(c: ClosedPosition) -> None:
    hold_s = (c.closed_ts_ms - c.pos.opened_ts_ms) / 1000
    pnl_str = f"{c.pnl:+.4f} USDC" if c.pnl is not None else "N/A"
    direction = "✓ 盈" if (c.pnl or 0) > 0 else "✗ 亏"
    log.info(
        "[CLOSE] %s | %s %s | exit=%.4f | hold=%.0fs | %s",
        c.pos.asset, direction, pnl_str,
        c.exit_yes_price or 0,
        hold_s, c.pos.poly_symbol[:60],
    )

# ─────────────────────────────────────────
# 进度日志任务
# ─────────────────────────────────────────
async def heartbeat_task(duration_s: float) -> None:
    """每 60 秒打印一次运行摘要。"""
    interval = 60.0
    elapsed = 0.0
    while not state.shutdown.is_set() and elapsed < duration_s:
        try:
            await asyncio.wait_for(
                asyncio.shield(state.shutdown.wait()), timeout=interval
            )
        except asyncio.TimeoutError:
            pass
        elapsed += interval
        _print_progress(elapsed, duration_s)


def _print_progress(elapsed: float, total: float) -> None:
    closed = state.closed_positions
    pnls = [c.pnl for c in closed if c.pnl is not None]
    wins = sum(1 for p in pnls if p > 0)
    log.info(
        "━━ 进度 %.0f/%.0fs | OKX ticks: %d | Poly 缓存: %d 价格: %d | 信号: %d | "
        "持仓: %d | 已平: %d | 胜: %d | 净盈亏: %+.3f USDC | 重连: %d ━━",
        elapsed, total,
        state.okx_ticks, len(state.poly_cached_ids), state.poly_markets_found,
        len(state.signals), len(state.open_positions),
        len(closed), wins,
        sum(pnls), state.reconnects,
    )

# ─────────────────────────────────────────
# 强制平仓（关闭时）
# ─────────────────────────────────────────
def _force_close_all() -> None:
    now_ms = int(time.time() * 1000)
    for pos in state.open_positions:
        pm = state.poly_latest.get(pos.poly_market_id)
        exit_price = pm.yes_price if pm else None
        pnl: Optional[float] = None
        if exit_price is not None:
            pnl = (exit_price - pos.entry_yes_price) * pos.quantity - pos.fees
        closed = ClosedPosition(
            pos=pos,
            closed_ts_ms=now_ms,
            exit_yes_price=exit_price,
            pnl=pnl,
            close_reason="shutdown",
        )
        state.closed_positions.append(closed)
        _log_close(closed)
    state.open_positions.clear()

# ─────────────────────────────────────────
# 报告生成
# ─────────────────────────────────────────
def _generate_report(duration_s: float, report_path: str = "MVP_RUN_REPORT.md") -> None:
    now_wall = datetime.now(timezone.utc).isoformat()
    elapsed_s = time.monotonic() - state.start_ts

    closed = state.closed_positions
    pnls = [c.pnl for c in closed if c.pnl is not None]
    wins = sum(1 for p in pnls if p > 0)
    losses = len(pnls) - wins
    total_pnl = sum(pnls)
    win_rate = wins / len(pnls) if pnls else None

    # 最大回撤
    max_dd = 0.0
    peak = 0.0
    cumulative = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_dd = max(max_dd, dd)

    # 信号延迟统计（OKX tick 到信号检测的近似值）
    acted_signals = [s for s in state.signals if s.acted]

    # 无机会原因
    reasons_md = ""
    if state.no_trade_reasons:
        lines = []
        for reason, cnt in sorted(
            state.no_trade_reasons.items(), key=lambda x: -x[1]
        ):
            lines.append(f"| {reason} | {cnt} |")
        reasons_md = "\n".join(lines)
    else:
        reasons_md = "| — | — |"

    # Polymarket 价格状态快照
    poly_snapshot = ""
    for pm in list(state.poly_latest.values())[:10]:
        poly_snapshot += f"- {pm.symbol[:80]}  YES={pm.yes_price:.3f}\n"
    if not poly_snapshot:
        poly_snapshot = "（未获取到 Polymarket 数据）\n"

    report = f"""# MVP 运行报告

**生成时间：** {now_wall}
**启动时间：** {state.start_wall}
**实际运行：** {elapsed_s:.0f} 秒（目标 {duration_s:.0f} 秒）

---

## 数据流状态

| 指标 | 数值 |
|---|---|
| OKX Tick 总数 | {state.okx_ticks} |
| OKX 重连次数 | {state.reconnects} |
| Polymarket 轮询次数 | {state.poly_polls} |
| Polymarket 市场数（最后一次） | {state.poly_markets_found} |
| 错误总数 | {state.errors} |

---

## 信号与交易

| 指标 | 数值 |
|---|---|
| 信号检测数 | {len(state.signals)} |
| 实际 Paper Trade 数 | {len(acted_signals)} |
| 已平仓 | {len(closed)} |
| 当前持仓 | {len(state.open_positions)} |

---

## Paper Trade 结果（模拟）

| 指标 | 数值 |
|---|---|
| 初始资金 | {INITIAL_CASH:.2f} USDC |
| 最终资金 | {state.cash:.4f} USDC |
| 净盈亏 | {total_pnl:+.4f} USDC |
| 已平仓盈利笔数 | {wins} |
| 已平仓亏损笔数 | {losses} |
| 胜率 | {f"{win_rate:.1%}" if win_rate is not None else "N/A"} |
| 止损触发次数 | {state.stop_loss_count} |
| hold_window 平仓次数 | {sum(1 for c in closed if c.close_reason == "hold_window_expired")} |
| 大亏单数（>5 USDC） | {sum(1 for p in pnls if p < -5.0)} |
| 最大回撤 | {max_dd:.4f} USDC |

> ⚠️ 以上为本地模拟数据，不代表真实收益。

---

## 无机会原因分析

| 原因 | 次数 |
|---|---|
{reasons_md}

---

## Polymarket 市场价格快照（最多 10 个）

{poly_snapshot}
---

## 平仓明细

| 资产 | 入场 YES | 出场 YES | 盈亏 USDC | 持仓秒 | 原因 |
|---|---|---|---|---|---|
"""
    for c in closed:
        hold_s = (c.closed_ts_ms - c.pos.opened_ts_ms) / 1000
        pnl_str = f"{c.pnl:+.4f}" if c.pnl is not None else "N/A"
        exit_str = f"{c.exit_yes_price:.4f}" if c.exit_yes_price else "N/A"
        report += (
            f"| {c.pos.asset} | {c.pos.entry_yes_price:.4f} | "
            f"{exit_str} | {pnl_str} | {hold_s:.0f} | {c.close_reason} |\n"
        )

    if not closed:
        report += "| — | — | — | — | — | — |\n"

    report += f"""
---

## 结论

"""
    if state.okx_ticks == 0:
        report += "**❌ OKX 数据未接入** — WebSocket 连接失败，请检查网络/防火墙。\n"
        report += f"已尝试 URL: {OKX_WS_URLS}\n"
    elif state.poly_markets_found == 0:
        report += "**⚠️ Polymarket 数据未获取** — 请检查 gamma-api.polymarket.com 连接。\n"
    elif len(state.signals) == 0:
        report += (
            f"**未检测到套利信号** — 在运行期间内，"
            f"OKX 价格变动均未超过 {MOVE_THRESHOLD_PCT*100:.1f}% 阈值，"
            f"或无匹配 Polymarket 市场。\n"
        )
    elif len(acted_signals) == 0:
        report += "**有信号但未交易** — 原因见上方「无机会原因分析」。\n"
    elif win_rate is not None and win_rate >= 0.5:
        report += (
            f"**✅ 初步信号：存在潜在边缘** — 胜率 {win_rate:.1%}，"
            f"净盈亏 {total_pnl:+.4f} USDC。需更长时间验证。\n"
        )
    else:
        report += (
            f"**⚠️ 当前参数下无明显优势** — 胜率 {win_rate:.1%}，"
            f"净盈亏 {total_pnl:+.4f} USDC。可调整阈值或持仓时间再验证。\n"
        )

    report += "\n---\n*本报告由 mvp_runner.py 自动生成，仅用于研究目的。禁止用于真实交易。*\n"

    # Per-asset 分位数统计表（直接传 ClosedPosition 列表，report.py 负责字段提取）
    per_asset_stats = compute_per_asset_trade_stats(list(closed))
    report += "\n---\n\n" + format_per_asset_trade_table(per_asset_stats)

    Path(report_path).write_text(report, encoding="utf-8")
    log.info("报告已生成: %s", report_path)

# ─────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────
async def _run(duration_s: float) -> None:
    log.info("━━━━━━ MVP 启动 | 目标运行 %.0f 秒 ━━━━━━", duration_s)
    log.info("OKX 订阅: %s | 阈值: %.1f%% | 持仓窗口: %.0fs",
             OKX_SYMBOLS, MOVE_THRESHOLD_PCT * 100, HOLD_WINDOW_S)

    tasks = [
        asyncio.create_task(okx_task(), name="okx"),
        asyncio.create_task(okx_rest_task(), name="okx_rest"),
        asyncio.create_task(poly_discovery_task(), name="poly_discovery"),
        asyncio.create_task(poly_task(), name="poly"),
        asyncio.create_task(strategy_task(), name="strategy"),
        asyncio.create_task(heartbeat_task(duration_s), name="heartbeat"),
    ]

    # 定时停止
    async def _stopper():
        await asyncio.sleep(duration_s)
        log.info("━━ 运行时间到，准备退出 ━━")
        state.shutdown.set()

    stopper = asyncio.create_task(_stopper(), name="stopper")
    tasks.append(stopper)

    try:
        await state.shutdown.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    _force_close_all()


def main() -> None:
    global MOVE_THRESHOLD_PCT
    parser = argparse.ArgumentParser(
        description="Polymarket × OKX Paper Trade MVP Runner"
    )
    parser.add_argument(
        "--duration", type=float, default=1800,
        help="运行时长（秒），默认 1800 = 30 分钟",
    )
    parser.add_argument(
        "--log", default="mvp_run.log",
        help="日志文件路径（默认 mvp_run.log）",
    )
    parser.add_argument(
        "--report", default="MVP_RUN_REPORT.md",
        help="报告文件路径（默认 MVP_RUN_REPORT.md）",
    )
    parser.add_argument(
        "--threshold", type=float, default=MOVE_THRESHOLD_PCT,
        help=f"OKX 价格变动阈值（默认 {MOVE_THRESHOLD_PCT}）",
    )
    args = parser.parse_args()

    # 允许从命令行覆盖阈值
    MOVE_THRESHOLD_PCT = args.threshold

    _setup_logging(args.log)

    # SIGINT / SIGTERM 优雅退出
    def _handle_signal(sig, frame):  # noqa: ANN001
        log.info("收到中断信号，准备退出…")
        state.shutdown.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        asyncio.run(_run(args.duration))
    except KeyboardInterrupt:
        pass
    finally:
        _generate_report(args.duration, args.report)
        log.info("━━━━━━ MVP 结束 ━━━━━━")


if __name__ == "__main__":
    main()
