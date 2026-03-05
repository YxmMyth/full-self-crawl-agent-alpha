"""Diagnose the CodePen Export button structure via Camoufox."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    try:
        from rebrowser_playwright.async_api import async_playwright
    except ImportError:
        from playwright.async_api import async_playwright

    ws_url = os.environ.get("BROWSER_WS_URL", "ws://localhost:1234/ws")
    auth_path = os.environ.get("BROWSER_STORAGE_STATE", "states/auth_state.json")

    async with async_playwright() as p:
        browser = await p.firefox.connect(ws_url)
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            storage_state=auth_path if os.path.exists(auth_path) else None,
        )
        page = await ctx.new_page()

        print("Navigating to pen page (networkidle)...")
        await page.goto(
            "https://codepen.io/inverser/pen/QWaLqPm",
            wait_until="networkidle",
            timeout=30000,
        )
        title = await page.title()
        print(f"Title: {title}")

        # Dump all buttons
        script = """
() => {
    const els = Array.from(document.querySelectorAll('button, [role=button], a'));
    return els.map(b => ({
        tag: b.tagName,
        text: b.textContent.trim().substring(0, 60),
        ariaLabel: b.getAttribute('aria-label'),
        title: b.getAttribute('title'),
        dataId: b.getAttribute('data-id'),
        dataTestid: b.getAttribute('data-testid'),
        className: b.className.substring(0, 120),
        visible: b.offsetParent !== null,
        x: b.getBoundingClientRect().x,
        y: b.getBoundingClientRect().y
    }));
}
"""
        buttons = await page.evaluate(script)

        print(f"\nTotal interactive elements: {len(buttons)}")
        print("\n--- Visible elements ---")
        for b in buttons:
            if b["visible"]:
                print(
                    f"  [{b['tag']}] text={repr(b['text'][:40])} "
                    f"aria={b['ariaLabel']} title={b['title']} "
                    f"xy=({b['x']:.0f},{b['y']:.0f})"
                )
                if b["className"]:
                    print(f"    class: {b['className']}")

        # Specifically look for export-related elements
        print("\n--- Export/Settings related ---")
        for b in buttons:
            combined = " ".join(
                filter(None, [b["text"], b["ariaLabel"], b["title"], b["className"]])
            ).lower()
            if any(kw in combined for kw in ["export", "setting", "menu", "action", "more", "download"]):
                print(f"  MATCH [{b['tag']}] text={repr(b['text'][:40])} "
                      f"aria={b['ariaLabel']} title={b['title']} visible={b['visible']}")
                print(f"    class: {b['className']}")

        await ctx.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
