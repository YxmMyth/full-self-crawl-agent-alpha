"""
Quick browser diagnostic: navigate to a URL, take a screenshot, dump page title + first 2000 chars of text.
Usage:
    python scripts/diag_browser.py https://codepen.io/trending
"""
import asyncio
import os
import sys
import base64

async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "https://codepen.io/trending"

    try:
        from rebrowser_playwright.async_api import async_playwright
    except ImportError:
        from playwright.async_api import async_playwright

    ws_url = os.environ.get("BROWSER_WS_URL", "")

    async with async_playwright() as p:
        if ws_url:
            print(f"[diag] Connecting to WS: {ws_url}")
            browser = await p.firefox.connect(ws_url)
        else:
            print("[diag] Launching local Chromium")
            browser = await p.chromium.launch(headless=True)

        # Load saved auth cookies if available
        storage_state = None
        state_path = os.environ.get("BROWSER_STORAGE_STATE", "")
        if not state_path:
            # Also check the default local path when running outside docker
            local_default = "states/auth_state.json"
            if os.path.exists(local_default):
                state_path = local_default
        if state_path and os.path.exists(state_path):
            storage_state = state_path
            print(f"[diag] Loading auth state from {state_path}")

        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
            storage_state=storage_state,
        )
        page = await context.new_page()

        print(f"[diag] Navigating to {url} ...")
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            print(f"[diag] HTTP status: {resp.status if resp else 'unknown'}")
        except Exception as e:
            print(f"[diag] Navigation error: {e}")

        # Wait for network to settle so JS-rendered pens load
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
            print("[diag] networkidle reached")
        except Exception:
            print("[diag] networkidle timed out, continuing anyway")

        title = await page.title()
        current_url = page.url
        print(f"[diag] Title: {title!r}")
        print(f"[diag] Final URL: {current_url}")

        # Check for CF challenge signals
        html = await page.content()
        cf_signals = ["challenge-form", "cf-browser-verification", "cf_clearance",
                      "Checking your browser", "Just a moment", "Enable JavaScript"]
        for sig in cf_signals:
            if sig.lower() in html.lower():
                print(f"[diag] ⚠️  CF signal detected: {sig!r}")

        # Dump visible text sample
        try:
            text = await page.evaluate("() => document.body.innerText")
            print(f"\n[diag] First 3000 chars of body text:\n{'='*60}")
            print(text[:3000])
            print('='*60)
        except Exception as e:
            print(f"[diag] Could not get body text: {e}")

        # Check multiple selectors to find pens and dump inner HTML snippet
        try:
            selectors = [
                ".single-pen", ".pen-grid-item", "[data-pen-slug]",
                "[class*='pen-item']", "[class*='PenItem']", "[class*='GridItem']",
                "article",
            ]
            for sel in selectors:
                items = await page.query_selector_all(sel)
                if items:
                    print(f"\n[diag] Selector {sel!r}: {len(items)} elements")
                    for i, item in enumerate(items[:2]):
                        txt = await item.inner_text()
                        print(f"  [{i}] text={txt[:200]!r}")
        except Exception as e:
            print(f"[diag] Element check error: {e}")

        # Try the JS extraction similar to execute_code
        try:
            data = await page.evaluate("""() => {
                const items = document.querySelectorAll('.single-pen, [data-pen-slug], .pen-grid-item');
                return Array.from(items).slice(0,5).map(el => ({
                    title: el.querySelector('.pen-title, [class*="title"], h3, h2')?.innerText?.trim() || 'N/A',
                    author: el.querySelector('.username, [class*="author"], [class*="user"]')?.innerText?.trim() || 'N/A',
                    html_snippet: el.outerHTML.slice(0, 400),
                }));
            }""")
            print(f"\n[diag] JS extraction ({len(data)} items):")
            for d in data:
                print(f"  title={d['title']!r}  author={d['author']!r}")
                print(f"  html: {d['html_snippet'][:300]}")
        except Exception as e:
            print(f"[diag] JS extraction error: {e}")

        await browser.close()

asyncio.run(main())
