"""
测试 Camoufox 多 page 并发能力：
并发开 N 个 page，各自导航到不同 URL，观察壁钟时间和成功率。
"""
import asyncio
import time
import sys
sys.path.insert(0, '/app')

CAMOUFOX_WS = "ws://camoufox:1234/ws"

TEST_URLS = [
    "https://example.com",
    "https://httpbin.org/get",
    "https://example.org",
    "https://httpbin.org/delay/1",
    "https://codepen.io",
]

async def navigate_page(context, idx, url):
    start = time.time()
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        title = await page.title()
        elems = await page.evaluate("document.querySelectorAll('*').length")
        return {"idx": idx, "url": url, "ok": True,
                "t": round(time.time()-start, 2), "title": title[:40], "elems": elems}
    except Exception as e:
        return {"idx": idx, "url": url, "ok": False,
                "t": round(time.time()-start, 2), "err": str(e)[:60]}
    finally:
        await page.close()

async def test_n(n: int):
    import os
    from playwright.async_api import async_playwright
    ws_url = os.environ.get("BROWSER_WS_URL", CAMOUFOX_WS)
    async with async_playwright() as p:
        browser = await p.firefox.connect(ws_url)
        context = await browser.new_context()
        print(f"\n=== {n} pages 并发 ===")
        t0 = time.time()
        results = await asyncio.gather(*[
            navigate_page(context, i, TEST_URLS[i % len(TEST_URLS)])
            for i in range(n)
        ])
        wall = round(time.time()-t0, 2)
        ok = sum(1 for r in results if r["ok"])
        print(f"  OK:{ok}/{n}  wall:{wall}s")
        for r in sorted(results, key=lambda x: x["idx"]):
            s = "V" if r["ok"] else "X"
            d = f"'{r.get('title','')}' elems={r.get('elems')}" if r["ok"] else r.get("err","")
            print(f"    {s} #{r['idx']} {r['t']}s  {d}")
        await context.close()
        await browser.close()

async def main():
    for n in [1, 2, 3, 5]:
        await test_n(n)
        await asyncio.sleep(1)

asyncio.run(main())
