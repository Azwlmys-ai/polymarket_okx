#!/usr/bin/env python3
"""
scripts/watch_poly_markets.py
只读 Polymarket 市场监控脚本。

功能：
- 扫描 /markets/keyset（去重），检查 BTC/ETH/SOL Up or Down 等短期加密市场
- 输出扫描报告，若发现可交易目标市场则提示可以启动 14h dry-run
- 不修改任何交易逻辑，不启动 mvp_runner.py，不接触真实交易

用法：
    python scripts/watch_poly_markets.py          # 单次扫描
    python scripts/watch_poly_markets.py --loop 30  # 每 30 分钟循环扫描
    python scripts/watch_poly_markets.py --pages 100  # 最多扫描 100 页

禁止真实交易。仅用于研究目的。
"""
from __future__ import annotations

import argparse
import re
import ssl
import json
import time
import urllib.request
from datetime import datetime, timezone

# ─────────────────────────────────────────
# 配置
# ─────────────────────────────────────────
GAMMA_URL   = "https://gamma-api.polymarket.com"
BATCH       = 100       # keyset API 每页上限
MAX_PAGES   = 300       # 保护上限（300×100=30,000 市场）

MIN_YES     = 0.47      # 与 mvp_runner.py 一致
MAX_YES     = 0.53
MAX_DAYS_TO_EXPIRY = 7  # 只考虑 7 天内到期的市场（过滤长期投机市场）

# 目标市场检测规则（短期加密方向市场）
# 只保留 tier=3/4：分钟级区间和日线方向。
# tier=2（长期价格触达）已移除——其唯一匹配 "Will bitcoin hit $1m before GTA VI?"
# 是长期投机市场，YES 价格不跟 OKX 实时信号，历史 14h 已验证无法盈利。
TARGET_PATTERNS = [
    # tier=4：分钟级区间市场（"7:25AM-7:30AM ET"）
    (4, re.compile(r"(bitcoin|btc|ethereum|eth|solana|sol).*"
                   r"(up or down|higher or lower).*"
                   r"\d{1,2}:\d{2}(am|pm).*-.*\d{1,2}:\d{2}(am|pm)", re.I)),
    # tier=3：日线方向市场（"Up or Down" 无时间后缀）
    (3, re.compile(r"(bitcoin|btc|ethereum|eth|solana|sol).*"
                   r"(up or down|higher or lower)", re.I)),
]

# BTC/ETH/SOL 候选词（不含方向词，用于广义统计）
ASSET_KW = {
    "BTC": ["bitcoin", " btc "],
    "ETH": ["ethereum", " eth "],
    "SOL": ["solana", " sol "],
}


# ─────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────
def _ssl_ctx() -> ssl.SSLContext:
    """跳过本地证书验证（macOS Homebrew Python 缺少根证书）。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_CTX = _ssl_ctx()
_HEADERS = {"User-Agent": "poly-watch/1.0"}


def fetch_page(cursor: str = "") -> dict:
    url = f"{GAMMA_URL}/markets/keyset?limit={BATCH}"
    if cursor:
        url += f"&next_cursor={cursor}"
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=15, context=_CTX) as r:
        return json.loads(r.read())


def parse_yes(m: dict) -> float | None:
    op = m.get("outcomePrices")
    if op:
        try:
            ps = op if isinstance(op, list) else json.loads(op)
            v = float(ps[0]) if ps else None
            return v if v and v > 0 else None
        except Exception:
            pass
    return None


def tier_of(title: str) -> int:
    """Return tier: 4=minute-level, 3=daily direction, 2=short-term price, 0=no match."""
    for tier, pat in TARGET_PATTERNS:
        if pat.search(title):
            return tier
    return 0


# ─────────────────────────────────────────
# 核心扫描
# ─────────────────────────────────────────
def scan_once(max_pages: int = MAX_PAGES) -> dict:
    """
    Scan Polymarket keyset, deduplicate, classify markets.
    Returns a result dict suitable for display.
    """
    seen: set[str] = set()
    targets: list[dict] = []          # tier >= 2 且 YES 在范围内
    asset_counts: dict[str, int] = {k: 0 for k in ASSET_KW}
    total_unique = 0
    pages = 0
    cursor = ""

    while pages < max_pages:
        try:
            page = fetch_page(cursor)
        except Exception as exc:
            print(f"  [WARN] fetch failed: {exc}")
            break

        markets = page.get("markets", [])
        if not markets:
            break
        pages += 1

        for m in markets:
            mid = str(m.get("id") or m.get("conditionId") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            total_unique += 1

            title = m.get("question") or m.get("title") or ""
            tl = title.lower()
            yes = parse_yes(m)
            end = (m.get("endDate") or "")[:10]
            liq = float(m.get("liquidity") or 0)

            # Asset count (broad)
            for asset, kws in ASSET_KW.items():
                if any(kw in tl for kw in kws):
                    asset_counts[asset] += 1

            # Target detection: tier>=3, YES in range, expiry within MAX_DAYS_TO_EXPIRY
            t = tier_of(title)
            if t >= 3 and yes is not None and MIN_YES < yes < MAX_YES:
                # Reject markets expiring more than MAX_DAYS_TO_EXPIRY days out
                days_left: float | None = None
                if end:
                    try:
                        exp = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
                        days_left = (exp - datetime.now(timezone.utc)).total_seconds() / 86400
                        if days_left > MAX_DAYS_TO_EXPIRY:
                            continue
                    except ValueError:
                        pass
                targets.append({
                    "tier": t,
                    "title": title[:100],
                    "yes": yes,
                    "end": end,
                    "liq": liq,
                    "days_left": days_left,
                })

        cursor = page.get("next_cursor", "")
        if not cursor:
            break
        time.sleep(0.04)

    targets.sort(key=lambda x: (-x["tier"], -x["liq"]))
    return {
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "pages": pages,
        "total_unique": total_unique,
        "asset_counts": asset_counts,
        "targets": targets,
    }


# ─────────────────────────────────────────
# 报告输出
# ─────────────────────────────────────────
TIER_LABEL = {4: "分钟级", 3: "日线方向", 2: "短期触达"}


def print_report(result: dict) -> bool:
    """Print scan report. Returns True if tradeable targets found."""
    r = result
    found = len(r["targets"]) > 0

    print()
    print("=" * 70)
    print(f"  Polymarket 市场监控报告")
    print(f"  扫描时间   : {r['scanned_at']}")
    print(f"  扫描页数   : {r['pages']}  唯一市场数: {r['total_unique']}")
    print("=" * 70)

    print(f"\n  BTC 相关市场: {r['asset_counts']['BTC']:>4} 个")
    print(f"  ETH 相关市场: {r['asset_counts']['ETH']:>4} 个")
    print(f"  SOL 相关市场: {r['asset_counts']['SOL']:>4} 个")

    if not found:
        print("\n  ❌ 未发现可交易目标市场（无短期加密方向市场，YES 价格在 0.47–0.53）")
        print("  → 暂不建议启动 14h dry-run")
    else:
        print(f"\n  ✅ 发现 {len(r['targets'])} 个可交易目标市场！")
        print()
        print(f"  {'tier':<8} {'YES':>6}  {'剩余天':>6}  {'到期':>10}  {'流动性':>10}  {'标题'}")
        print(f"  {'-'*100}")
        for m in r["targets"][:20]:
            label = TIER_LABEL.get(m["tier"], f"tier={m['tier']}")
            days_s = f"{m['days_left']:.1f}d" if m.get("days_left") is not None else "  N/A"
            print(f"  {label:<8} {m['yes']:.3f}  {days_s:>6}  {m['end']:>10}  "
                  f"${m['liq']:>8,.0f}  {m['title']}")
        print()
        print("  ⚡ 建议：可以启动 14h dry-run")
        print("  命令：DISABLE_SSL_VERIFY=1 .venv/bin/python mvp_runner.py \\")
        print("          --duration 50400 \\")
        print("          --log run_14h_v4.log \\")
        print("          --report MVP_RUN_REPORT_14h_v4.md")

    print("=" * 70)
    print()
    return found


# ─────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="只读 Polymarket 目标市场监控（不启动交易）"
    )
    parser.add_argument(
        "--loop", type=int, default=0, metavar="MINUTES",
        help="循环间隔分钟数（0=只跑一次）",
    )
    parser.add_argument(
        "--pages", type=int, default=MAX_PAGES, metavar="N",
        help=f"最多扫描页数（默认 {MAX_PAGES}，即 {MAX_PAGES*BATCH} 市场）",
    )
    args = parser.parse_args()

    while True:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M UTC')}] 开始扫描...", flush=True)
        result = scan_once(max_pages=args.pages)
        print_report(result)

        if args.loop <= 0:
            break
        print(f"  下次扫描: {args.loop} 分钟后（Ctrl+C 中断）\n")
        try:
            time.sleep(args.loop * 60)
        except KeyboardInterrupt:
            print("\n  已中断。")
            break


if __name__ == "__main__":
    main()
