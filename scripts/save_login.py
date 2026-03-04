"""
Browser helper for the crawl agent.

Mode 1 - Save cookies:
    python scripts/save_login.py https://codepen.io

Mode 2 - Launch persistent browser for Docker to connect:
    python scripts/save_login.py --serve https://codepen.io

Mode 1: Opens browser, you log in, cookies saved to states/auth_state.json.
Mode 2: Opens browser with remote debugging on port 9222.
        Log in, then leave it running. Docker connects via BROWSER_CDP_URL.
"""

import asyncio
import sys
from pathlib import Path


async def save_cookies(url: str):
    """Open browser, user logs in, save cookies."""
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
        print("👉 When done, press Enter.\n")

        input("Press Enter when login is complete...")

        await context.storage_state(path=str(output))
        print(f"\n✅ Auth state saved to {output}")
        await browser.close()


async def serve_browser(url: str):
    """Launch Chrome with remote debugging for Docker to connect."""
    import subprocess
    import shutil
    import os
    import time

    port = 9222
    # Find Chrome
    chrome = shutil.which("google-chrome") or shutil.which("chrome")
    if not chrome and os.name == "nt":
        for path in [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]:
            if os.path.exists(path):
                chrome = path
                break

    if not chrome:
        print("❌ Chrome not found. Install Google Chrome first.")
        return

    user_data = Path("states/chrome_profile")
    user_data.mkdir(parents=True, exist_ok=True)

    cmd = [
        chrome,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=0.0.0.0",
        f"--user-data-dir={user_data.absolute()}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]

    print(f"\n🚀 Launching Chrome with remote debugging on port {port}...")
    print(f"   Profile: {user_data.absolute()}")
    proc = subprocess.Popen(cmd)
    time.sleep(2)

    print(f"\n✅ Chrome running (PID {proc.pid})")
    print(f"   CDP URL: http://localhost:{port}")
    print(f"\n👉 Log in to the site in the browser window.")
    print(f"👉 Then run the agent with:")
    print(f'   docker run --rm --env-file .env \\')
    print(f'     -e BROWSER_CDP_URL=http://host.docker.internal:{port} \\')
    print(f'     -v "${{PWD}}/artifacts:/workspace/artifacts" \\')
    print(f'     crawl-agent-alpha:latest "https://codepen.io/trending" \\')
    print(f'     --requirement "..." --mode full_site')
    print(f"\n⏳ Browser stays open. Press Ctrl+C to stop.")

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        print("\n🛑 Chrome stopped.")


if __name__ == "__main__":
    args = sys.argv[1:]
    serve_mode = "--serve" in args
    args = [a for a in args if a != "--serve"]
    url = args[0] if args else "https://codepen.io"

    if serve_mode:
        asyncio.run(serve_browser(url))
    else:
        asyncio.run(save_cookies(url))
