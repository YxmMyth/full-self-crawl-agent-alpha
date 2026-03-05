"""Test clicking the Export button on a CodePen pen page."""
import asyncio
import json
import os


async def main():
    from playwright.async_api import async_playwright

    ws_url = os.environ.get("BROWSER_WS_URL", "ws://localhost:1234/ws")
    auth_path = os.environ.get("BROWSER_STORAGE_STATE", "")

    async with async_playwright() as p:
        browser = await p.firefox.connect(ws_url)
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            storage_state=auth_path if auth_path and os.path.exists(auth_path) else None,
        )
        page = await ctx.new_page()
        await page.goto(
            "https://codepen.io/inverser/pen/QWaLqPm",
            wait_until="networkidle",
            timeout=30000,
        )
        print("Page loaded:", await page.title())

        # Use Playwright's text selector to find Export button in footer
        export_btn = page.get_by_role("button", name="Export")
        count = await export_btn.count()
        print(f"Export buttons found: {count}")

        # Click it
        await export_btn.click()
        await page.wait_for_timeout(1500)
        print("Clicked Export, waiting for dropdown...")

        # Dump visible elements that appeared
        result = await page.evaluate("""
        () => {
            const out = [];
            document.querySelectorAll('button, a, [role=menuitem]').forEach(el => {
                if (el.offsetParent !== null) {
                    const text = el.textContent.trim();
                    if (text) out.push({
                        tag: el.tagName,
                        text: text.substring(0, 80),
                        cls: el.className.substring(0, 100),
                        ariaLabel: el.getAttribute('aria-label'),
                        href: el.getAttribute('href'),
                        xy: [el.getBoundingClientRect().x, el.getBoundingClientRect().y]
                    });
                }
            });
            return out;
        }
        """)
        print(f"\nVisible interactive elements after Export click ({len(result)} total):")
        for el in result:
            txt = el["text"]
            if any(kw in txt.lower() for kw in ["zip", "export", "download"]):
                print(f"  *** ZIP/EXPORT MATCH: [{el['tag']}] text={repr(txt)} href={el['href']} xy={el['xy']}")
                print(f"      class={el['cls']}")

        # Also capture screenshot
        await page.screenshot(path="/tmp/export_menu.png")
        print("\nScreenshot saved to /tmp/export_menu.png")

        await ctx.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
