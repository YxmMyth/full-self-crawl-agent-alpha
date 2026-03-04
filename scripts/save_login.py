"""
Save browser login state for use in Docker.

Usage:
    python scripts/save_login.py https://codepen.io

Opens a real browser window. Log in manually (OAuth, 2FA, etc.).
When done, press Enter in the terminal. Cookies saved to states/auth_state.json.
"""

import asyncio
import sys
from pathlib import Path


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "https://codepen.io"
    output = Path("states/auth_state.json")
    output.parent.mkdir(parents=True, exist_ok=True)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()
        await page.goto(url)

        print(f"\n🌐 Browser opened at {url}")
        print("👉 Log in manually (OAuth, 2FA, etc.)")
        print("👉 When you're fully logged in, come back here and press Enter.\n")

        input("Press Enter when login is complete...")

        await context.storage_state(path=str(output))
        print(f"\n✅ Auth state saved to {output}")
        print("   Docker usage:")
        print(f'   docker run --rm --env-file .env -e BROWSER_STORAGE_STATE=/workspace/states/auth_state.json \\')
        print(f'     -v "${{PWD}}/artifacts:/workspace/artifacts" -v "${{PWD}}/states:/workspace/states" \\')
        print(f'     crawl-agent-alpha:latest "https://codepen.io" --requirement "..." --mode full_site')

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
