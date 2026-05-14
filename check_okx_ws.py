"""
Minimal OKX WebSocket connectivity test — 60 seconds, no orders, no API keys.
"""
import asyncio
import json
import ssl
import time
import certifi
import aiohttp

OKX_WS_URLS = [
    "wss://ws.okx.com:8443/ws/v5/public",
    "wss://wsaws.okx.com:8443/ws/v5/public",
    "wss://wsap.okx.com:8443/ws/v5/public",
]
DURATION = 60

async def run():
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    results = {}

    for url in OKX_WS_URLS:
        print(f"\n--- 测试: {url} ---")
        ticks = 0
        errors = []
        connected = False
        t0 = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    url, ssl=ssl_ctx, timeout=aiohttp.ClientTimeout(total=20)
                ) as ws:
                    connected = True
                    print("  ✅ WebSocket 已连接")
                    sub = {"op": "subscribe", "args": [{"channel": "tickers", "instId": "BTC-USDT"}]}
                    await ws.send_str(json.dumps(sub))
                    deadline = time.monotonic() + DURATION
                    async for msg in ws:
                        if time.monotonic() > deadline:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("data"):
                                ticks += 1
                                if ticks == 1:
                                    print(f"  首 tick: {data['data'][0].get('last', '?')}")
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                            errors.append(f"WSMsgType={msg.type}")
                            break
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

        elapsed = time.monotonic() - t0
        results[url] = {"connected": connected, "ticks": ticks, "errors": errors, "elapsed_s": round(elapsed, 1)}
        status = "✅" if connected and ticks > 0 else "❌"
        print(f"  {status} 连接={connected} tick={ticks} 耗时={elapsed:.1f}s 错误={errors}")
        if connected and ticks > 0:
            break  # 第一个成功就够了

    print("\n=== OKX WS 连通性结果 ===")
    for url, r in results.items():
        print(f"  {url}")
        print(f"    connected={r['connected']} ticks={r['ticks']} elapsed={r['elapsed_s']}s errors={r['errors']}")

asyncio.run(run())
