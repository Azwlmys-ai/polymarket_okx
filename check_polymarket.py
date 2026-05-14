"""
Minimal Polymarket HTTP connectivity test — no orders, no API keys, no wallet.
Silent failure is forbidden: all exceptions are printed with repr().
"""
import asyncio
import aiohttp

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL  = "https://clob.polymarket.com"

KEYWORDS = ["bitcoin", "ethereum", "solana", "btc", "eth", "sol",
            "up or down", "price"]

async def fetch_gamma(session: aiohttp.ClientSession):
    url = f"{GAMMA_URL}/markets"
    params = {"limit": 50, "active": "true", "closed": "false"}
    print(f"\n--- Gamma API: {url} ---")
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            print(f"  HTTP 状态码: {resp.status}")
            if resp.status != 200:
                body = await resp.text()
                print(f"  ❌ 非 200 响应: {body[:300]}")
                return
            data = await resp.json(content_type=None)
            markets = data if isinstance(data, list) else data.get("data", data.get("markets", []))
            print(f"  返回市场数: {len(markets)}")
            crypto = [m for m in markets
                      if any(kw in (m.get("question") or "").lower() for kw in KEYWORDS)]
            print(f"  加密相关市场: {len(crypto)}")
            for m in crypto[:5]:
                print(f"    - {m.get('question','?')[:80]}  YES={m.get('outcomePrices','?')}")
    except Exception as exc:
        print(f"  ❌ 异常: {type(exc).__name__}: {exc}")
        print(f"  repr: {repr(exc)}")

async def fetch_clob(session: aiohttp.ClientSession):
    url = f"{CLOB_URL}/markets"
    params = {"limit": 5}
    print(f"\n--- CLOB API: {url} ---")
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            print(f"  HTTP 状态码: {resp.status}")
            if resp.status != 200:
                body = await resp.text()
                print(f"  ❌ 非 200 响应: {body[:300]}")
                return
            data = await resp.json(content_type=None)
            items = data if isinstance(data, list) else data.get("data", [])
            print(f"  返回条目数: {len(items)}")
    except Exception as exc:
        print(f"  ❌ 异常: {type(exc).__name__}: {exc}")
        print(f"  repr: {repr(exc)}")

async def run():
    async with aiohttp.ClientSession() as session:
        await fetch_gamma(session)
        await fetch_clob(session)
    print("\n=== Polymarket 连通性测试完成 ===")

asyncio.run(run())
