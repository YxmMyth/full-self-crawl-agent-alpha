import asyncio
from playwright.async_api import async_playwright


async def explore_hn():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        print("=== STEP 1: Homepage ===")
        await page.goto("https://news.ycombinator.com", wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(1500)
        title = await page.title()
        url = page.url
        print(f"Title: {title}")
        print(f"URL: {url}")

        # Nav links
        nav_links = await page.eval_on_selector_all(
            ".hnname, .pagetop a, span.topsel a, td.toptext a",
            "els => els.map(e => ({text: e.innerText.trim(), href: e.href}))"
        )
        print(f"\nTop nav links ({len(nav_links)}):")
        for l in nav_links:
            print(f"  {l['text']:20s} -> {l['href']}")

        # All links in top bar
        topbar_links = await page.eval_on_selector_all(
            "#hnmain > tbody > tr:first-child a",
            "els => els.map(e => ({text: e.innerText.trim(), href: e.href}))"
        )
        print(f"\nTopbar links ({len(topbar_links)}):")
        for l in topbar_links:
            print(f"  {l['text']:20s} -> {l['href']}")

        # Footer/nav links
        footer_links = await page.eval_on_selector_all(
            ".yclinks a, #hnmain a[href*='news'], #hnmain a[href*='ask'], #hnmain a[href*='show'], #hnmain a[href*='jobs']",
            "els => [...new Map(els.map(e => [e.href, {text: e.innerText.trim(), href: e.href}])).values()]"
        )
        print(f"\nNavigation section links ({len(footer_links)}):")
        for l in footer_links:
            print(f"  {l['text']:20s} -> {l['href']}")

        # Sample top stories
        print("\n=== Top stories (first 5) ===")
        stories = await page.eval_on_selector_all(
            ".athing",
            "els => els.slice(0,5).map(e => ({id: e.id, title: e.querySelector('.titleline a')?.innerText?.trim()?.substring(0,60), url: e.querySelector('.titleline a')?.href}))"
        )
        for s in stories:
            print(f"  [{s['id']}] {s['title']}")

        print("\n=== STEP 2: Check /search endpoint ===")
        await page.goto("https://news.ycombinator.com/search?q=threejs", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(1500)
        title2 = await page.title()
        url2 = page.url
        print(f"Title: {title2}")
        print(f"URL: {url2}")

        # Also check Algolia search
        print("\n=== STEP 3: hn.algolia.com search for threejs ===")
        await page.goto("https://hn.algolia.com/?q=threejs", wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)
        title3 = await page.title()
        url3 = page.url
        print(f"Title: {title3}")
        print(f"URL: {url3}")
        result_links = await page.eval_on_selector_all(
            ".Story_title a, .Story-title a, [class*=story] a",
            "els => els.slice(0,10).map(e => ({text: e.innerText.trim().substring(0,60), href: e.href}))"
        )
        print(f"Results ({len(result_links)}):")
        for r in result_links:
            print(f"  {r['text']}")

        print("\n=== STEP 4: Back to HN, 'ask' section ===")
        await page.goto("https://news.ycombinator.com/ask", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(1500)
        ask_stories = await page.eval_on_selector_all(
            ".athing",
            "els => els.slice(0,5).map(e => ({title: e.querySelector('.titleline a')?.innerText?.trim()?.substring(0,60)}))"
        )
        print("Ask HN stories:")
        for s in ask_stories:
            print(f"  {s['title']}")

        await browser.close()
        print("\nDone.")


asyncio.run(explore_hn())



asyncio.run(explore_codepen())
