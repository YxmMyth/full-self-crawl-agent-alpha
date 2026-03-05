"""
Browser helper for the crawl agent.

Mode 1 - Save cookies (CF-safe, uses real Chrome via subprocess):
    python scripts/save_login.py https://codepen.io

Mode 2 - Launch persistent browser for Docker to connect:
    python scripts/save_login.py --serve https://codepen.io

Mode 1: Opens a real Chrome window (no automation flags → no CF challenge).
        You log in, press Enter, cookies extracted via CDP and saved to
        states/auth_state.json in Playwright storage_state format.

Mode 2: Opens Chrome with remote debugging on port 9222.
        Log in, then leave it running. Docker connects via BROWSER_CDP_URL.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def find_chrome() -> str | None:
    chrome = shutil.which("google-chrome") or shutil.which("chrome")
    if not chrome and os.name == "nt":
        for path in [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]:
            if os.path.exists(path):
                return path
    return chrome


def read_cookies_via_cdp(port: int, target_domain: str) -> list[dict]:
    """Use CDP HTTP API to extract all cookies for a domain. No automation flags needed."""
    import urllib.request, json

    # Get list of tabs
    with urllib.request.urlopen(f"http://localhost:{port}/json/list") as r:
        tabs = json.loads(r.read())

    if not tabs:
        return []

    # Use websocket CDP to call Network.getAllCookies on first tab
    import asyncio

    async def _get():
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return []
        async with async_playwright() as p:
            # Connect via CDP *just to read cookies* — doesn't inject automation flags
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{port}")
            cookies = []
            for ctx in browser.contexts:
                for c in await ctx.cookies():
                    if target_domain.lstrip(".") in c.get("domain", ""):
                        cookies.append(c)
            await browser.close()
            return cookies

    return asyncio.run(_get())


def save_cookies(url: str):
    """Open real Chrome (no automation flags), user logs in, save cookies via CDP."""
    output = Path("states/auth_state.json")
    output.parent.mkdir(parents=True, exist_ok=True)

    chrome = find_chrome()
    if not chrome:
        print("❌ Chrome not found. Install Google Chrome first.")
        sys.exit(1)

    port = 9223  # separate port so it doesn't clash with serve mode
    user_data = Path("states/save_login_profile")
    user_data.mkdir(parents=True, exist_ok=True)

    cmd = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data.absolute()}",
        "--no-first-run",
        "--no-default-browser-check",
        # NO --enable-automation, NO --disable-blink-features — real Chrome
        url,
    ]

    print(f"\n🚀 Opening Chrome (real browser, no automation flags)...")
    print(f"   Profile: {user_data}")
    proc = subprocess.Popen(cmd)
    time.sleep(2)

    print(f"\n🌐 Browser opened at {url}")
    print("👉 Complete any security verification / login (GitHub, Google, 2FA, etc.)")
    print("👉 When fully logged in and on the site, press Enter.\n")
    input("Press Enter when login is complete...")

    # Extract cookies via CDP
    from urllib.parse import urlparse
    domain = urlparse(url).netloc
    print(f"\n🍪 Extracting cookies for {domain}...")

    try:
        cookies = read_cookies_via_cdp(port, domain)
        if not cookies:
            print("⚠️  No cookies found — are you sure you logged in?")
        else:
            state = {"cookies": cookies, "origins": []}
            output.write_text(json.dumps(state, indent=2))
            print(f"✅ {len(cookies)} cookies saved to {output}")
    except Exception as e:
        print(f"❌ Failed to read cookies via CDP: {e}")
        print("   Try: pip install playwright && playwright install chromium")
    finally:
        proc.terminate()
        print("🛑 Chrome closed.")


async def serve_browser(url: str):
    """Launch Chrome with remote debugging for Docker to connect."""
    port = 9222
    chrome = find_chrome()
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
        save_cookies(url)

