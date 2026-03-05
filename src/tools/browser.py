"""
Browser Tool — Playwright-based browser automation.

Migrated from old project's browser.py with additional methods:
- select_option(): Select dropdown option
- press_key(): Send keyboard events
- search_page(): Text search within visible content
- get_text(): Get visible text (no HTML)
"""

from typing import Any, Dict, List, Optional, Tuple
try:
    from rebrowser_playwright.async_api import (
        async_playwright, Browser, Page, Error as PlaywrightError,
    )
except ImportError:
    from playwright.async_api import (
        async_playwright, Browser, Page, Error as PlaywrightError,
    )
import asyncio
import functools
import random
import logging
import subprocess
import shutil
from fnmatch import fnmatch

logger = logging.getLogger("tools.browser")


def check_playwright_browsers() -> Tuple[bool, str]:
    """Check if Playwright browsers are installed."""
    try:
        import os
        from pathlib import Path

        browser_paths = [
            Path.home() / ".cache" / "ms-playwright",
            Path.home() / "AppData" / "Local" / "ms-playwright" if os.name == "nt" else None,
        ]
        for path in browser_paths:
            if path and path.exists():
                if list(path.glob("chromium-*")):
                    return True, ""

        cmd = "python -m playwright install chromium" if os.name == "nt" else "playwright install chromium"
        return False, f"Playwright browsers not installed. Run: {cmd}"
    except Exception as e:
        logger.warning(f"Error checking Playwright browsers: {e}")
        return True, ""


def with_retry(max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 10.0):
    """Retry decorator with exponential backoff + jitter."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            last_error = None
            for attempt in range(max_retries):
                try:
                    return await func(self, *args, **kwargs)
                except (PlaywrightError, asyncio.TimeoutError, ConnectionError) as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)
                        logger.warning(
                            f"Operation failed (attempt {attempt + 1}/{max_retries}): {e}, "
                            f"retrying in {delay:.1f}s..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"Operation failed after {max_retries} attempts: {e}")
            raise last_error
        return wrapper
    return decorator


class BrowserTool:
    """Playwright-based browser automation tool.

    Provides all browser interaction capabilities needed by the crawl agent:
    navigation, content extraction, form interaction, screenshots, and
    JavaScript evaluation.
    """

    def __init__(self, headless: bool = True, check_browsers: bool = True):
        self.headless = headless
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self.context = None
        self._storage_state_path: Optional[str] = None

        if check_browsers:
            installed, message = check_playwright_browsers()
            if not installed:
                logger.warning(message)

    async def start(self) -> None:
        """Launch browser (local Chromium, Camoufox WS, or remote CDP)."""
        import os
        try:
            self.playwright = await async_playwright().start()

            # Mode 1: Camoufox WS endpoint (best CF bypass — Firefox + C++ fingerprint injection)
            ws_url = os.environ.get("BROWSER_WS_URL", "")
            if ws_url:
                self.browser = await self.playwright.firefox.connect(ws_url)
                storage_state = None
                state_path = os.environ.get("BROWSER_STORAGE_STATE", "")
                if state_path and os.path.exists(state_path):
                    storage_state = state_path
                    self._storage_state_path = state_path
                    logger.info(f"Loading browser auth state from {state_path}")
                self.context = await self.browser.new_context(storage_state=storage_state)
                self.page = await self.context.new_page()
                logger.info(f"Connected to Camoufox at {ws_url}")
                return

            # Mode 2: Remote browser via CDP (bypasses CF by using a real browser)
            cdp_url = os.environ.get("BROWSER_CDP_URL", "")
            if cdp_url:
                # Chrome rejects non-IP Host headers; resolve ws:// URL ourselves
                if cdp_url.startswith("http"):
                    import urllib.request, json
                    from urllib.parse import urlparse
                    parsed = urlparse(cdp_url)
                    try:
                        req = urllib.request.Request(
                            f"{cdp_url}/json/version",
                            headers={"Host": "localhost"}
                        )
                        resp = urllib.request.urlopen(req, timeout=5)
                        ws_url = json.loads(resp.read())["webSocketDebuggerUrl"]
                        # Build ws URL with correct host and port for Docker
                        ws_parsed = urlparse(ws_url)
                        port = parsed.port or 9222
                        cdp_url = f"ws://{parsed.hostname}:{port}{ws_parsed.path}"
                        logger.info(f"Resolved CDP ws URL: {cdp_url}")
                    except Exception as e:
                        logger.warning(f"Could not resolve ws URL, using original: {e}")

                self.browser = await self.playwright.chromium.connect_over_cdp(cdp_url)
                logger.info(f"Connected to remote browser at {cdp_url}")
                # Use existing context if available, otherwise create new
                contexts = self.browser.contexts
                if contexts:
                    self.context = contexts[0]
                    pages = self.context.pages
                    self.page = pages[0] if pages else await self.context.new_page()
                    logger.info(f"Reusing existing context with {len(pages)} pages")
                else:
                    self.context = await self.browser.new_context(
                        viewport={"width": 1920, "height": 1080},
                    )
                    self.page = await self.context.new_page()
            else:
                self.browser = await self.playwright.chromium.launch(
                    headless=self.headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                    ],
                )
                # Load saved auth state if available
                storage_state = None
                state_path = os.environ.get("BROWSER_STORAGE_STATE", "")
                if state_path and os.path.exists(state_path):
                    storage_state = state_path
                    self._storage_state_path = state_path
                    logger.info(f"Loading browser auth state from {state_path}")

                self.context = await self.browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    storage_state=storage_state,
                )
            self.page = await self.context.new_page()

            # Inject credentials into page JS context (agent retrieves via evaluate_js)
            site_pass = os.environ.get("SITE_PASSWORD", "")
            if site_pass:
                await self.context.add_init_script(
                    f"window.__SITE_PASSWORD__ = {repr(site_pass)};"
                )

            logger.info("Browser started")
        except Exception as e:
            logger.error(f"Browser launch failed: {e}")
            if "Executable doesn't exist" in str(e) or "chromium" in str(e).lower():
                installed, message = check_playwright_browsers()
                if not installed:
                    raise RuntimeError(f"Browser launch failed: {message}") from e
            raise

    async def save_auth_state(self, path: str = "") -> dict:
        """Save browser cookies and localStorage for reuse across runs."""
        import os
        if not self.context:
            return {"error": "No browser context"}
        if not path:
            path = os.environ.get("BROWSER_STORAGE_STATE", "/workspace/artifacts/auth_state.json")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        await self.context.storage_state(path=path)
        self._storage_state_path = path
        logger.info(f"Auth state saved to {path}")
        return {"saved": path, "hint": "Auth state will be loaded automatically on next run if BROWSER_STORAGE_STATE is set."}

    async def stop(self) -> None:
        """Close browser."""
        if self.page:
            await self.page.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def close(self) -> None:
        """Alias for stop()."""
        await self.stop()

    async def create_tab(self) -> "BrowserTab":
        """Create new tab sharing the same browser context."""
        if not self.context:
            await self.start()
        new_page = await self.context.new_page()
        return BrowserTab(new_page, self)

    # --- Navigation ---

    @with_retry(max_retries=3, base_delay=1.0, max_delay=15.0)
    async def navigate(self, url: str, wait_until: str = "networkidle",
                       timeout: int = 30000) -> dict:
        """Navigate to URL and return page metadata including timing."""
        import time as _time
        if self.page is None:
            await self.start()

        start = _time.time()
        actual_strategy = wait_until
        try:
            await self.page.goto(url, wait_until=wait_until, timeout=timeout)
        except Exception as e:
            if wait_until == "networkidle" and "Timeout" in str(e):
                logger.info(f"networkidle timeout, falling back to load: {url}")
                actual_strategy = "load (networkidle timed out)"
                await self.page.goto(url, wait_until="load", timeout=timeout)
            else:
                raise
        elapsed_ms = int((_time.time() - start) * 1000)

        # Collect page metadata for agent awareness
        title = await self.page.title() or ""
        final_url = self.page.url
        # Quick element count to detect empty/blocked pages
        elem_count = await self.page.evaluate("document.querySelectorAll('*').length")
        page_hint = ""
        if elem_count < 10:
            page_hint = "Page appears empty (SPA not loaded, or blocked by anti-bot)"
        elif "just a moment" in title.lower() or "cloudflare" in title.lower():
            page_hint = "Possible Cloudflare challenge page detected"

        return {
            "url": final_url,
            "title": title,
            "load_time_ms": elapsed_ms,
            "strategy": actual_strategy,
            "element_count": elem_count,
            "hint": page_hint,
        }

    async def go_back(self) -> None:
        """Go back to previous page."""
        await self.page.go_back()

    # --- Content extraction ---

    async def get_html(self, selector: str | None = None) -> str:
        """Get page HTML, optionally scoped to a CSS selector."""
        if selector:
            el = await self.page.query_selector(selector)
            if el:
                return await el.inner_html()
            return ""
        return await self.page.content()

    async def get_text(self) -> str:
        """Get visible text content of the page (no HTML tags)."""
        return await self.page.inner_text("body")

    async def take_screenshot(self, path: str | None = None,
                              full_page: bool = False) -> bytes:
        """Take screenshot, optionally saving to path."""
        if path:
            await self.page.screenshot(path=path, full_page=full_page)
            with open(path, "rb") as f:
                return f.read()
        return await self.page.screenshot(full_page=full_page)

    # --- Interaction ---

    @with_retry(max_retries=3, base_delay=0.5, max_delay=5.0)
    async def click(self, selector: str) -> None:
        """Click element by CSS selector."""
        await self.page.click(selector)

    async def click_download(self, selector: str, timeout: int = 15000) -> dict:
        """Click element and capture the resulting browser download.
        
        Uses Playwright expect_download to capture files triggered by button clicks.
        Returns dict with file path, name, size, and suggested_filename.
        """
        try:
            async with self.page.expect_download(timeout=timeout) as download_info:
                await self.page.click(selector)
            download = await download_info.value
            return {
                "success": True,
                "suggested_filename": download.suggested_filename,
                "url": download.url,
                "_download": download,  # internal: caller saves the file
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @with_retry(max_retries=3, base_delay=0.5, max_delay=5.0)
    async def fill(self, selector: str, value: str) -> None:
        """Fill input field."""
        await self.page.fill(selector, value)

    async def select_option(self, selector: str, value: str) -> list[str]:
        """Select dropdown option by value, label, or index."""
        # Try by value first, then by label
        try:
            return await self.page.select_option(selector, value=value)
        except Exception:
            try:
                return await self.page.select_option(selector, label=value)
            except Exception:
                return await self.page.select_option(selector, index=int(value) if value.isdigit() else 0)

    async def press_key(self, key: str) -> None:
        """Send keyboard event (e.g., 'Enter', 'Escape', 'Control+a')."""
        await self.page.keyboard.press(key)

    async def scroll(self, direction: str = "down", pages: int = 1) -> None:
        """Scroll page up or down by N viewport heights."""
        delta = 1080 * pages  # viewport height
        if direction == "up":
            delta = -delta
        await self.page.evaluate(f"window.scrollBy(0, {delta})")
        await asyncio.sleep(0.5)

    # --- Analysis helpers ---

    async def search_page(self, query: str) -> list[dict]:
        """Search visible text for query string, return matching elements with context.

        Returns list of {"text": str, "selector": str, "index": int}.
        """
        results = await self.page.evaluate("""(query) => {
            const results = [];
            const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_TEXT, null, false
            );
            let idx = 0;
            while (walker.nextNode()) {
                const text = walker.currentNode.textContent;
                if (text && text.toLowerCase().includes(query.toLowerCase())) {
                    const el = walker.currentNode.parentElement;
                    results.push({
                        text: text.trim().substring(0, 200),
                        tag: el ? el.tagName.toLowerCase() : 'unknown',
                        index: idx
                    });
                }
                idx++;
            }
            return results.slice(0, 20);
        }""", query)
        return results

    # --- JavaScript evaluation ---

    async def evaluate(self, script: str) -> Any:
        """Execute JavaScript in page context."""
        return await self.page.evaluate(script)

    # --- Smart scrolling ---

    async def smart_scroll(self, max_scrolls: int = 10, scroll_delay: float = 1.0,
                           detect_new_content: bool = True) -> Dict[str, Any]:
        """Scroll to load dynamic content, detecting when new content stops appearing."""
        if not self.page:
            await self.start()

        total_scrolls = 0
        last_height = await self.page.evaluate("document.body.scrollHeight")
        content_grew = False

        while total_scrolls < max_scrolls:
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(scroll_delay)
            total_scrolls += 1

            new_height = await self.page.evaluate("document.body.scrollHeight")
            if new_height > last_height:
                content_grew = True
                last_height = new_height
                continue
            if detect_new_content:
                break

        return {"scrolls": total_scrolls, "content_grew": content_grew, "final_height": last_height}

    async def dismiss_popups(self) -> int:
        """Try to close common popups, cookie banners, and overlays."""
        if not self.page:
            return 0

        selectors = [
            'button:has-text("Accept")', 'button:has-text("I agree")', 'button:has-text("Got it")',
            '[id*="cookie"] button', '[class*="cookie"] button',
            '[aria-label*="close" i]', '[class*="close" i]',
            ".modal button", ".popup button", ".overlay button",
        ]
        closed = 0
        for selector in selectors:
            try:
                elements = await self.page.query_selector_all(selector)
                for el in elements[:3]:
                    try:
                        await el.click(timeout=1000)
                        closed += 1
                    except Exception:
                        continue
            except Exception:
                continue
        return closed

    async def capture_api_responses(self, url_pattern: str = "*/api/*",
                                    duration: int = 5000) -> List[Dict[str, Any]]:
        """Capture JSON API responses matching URL pattern for a duration."""
        if not self.page:
            await self.start()

        captured: List[Dict[str, Any]] = []

        async def _on_response(response):
            try:
                url = response.url
                if not fnmatch(url, url_pattern):
                    return
                ct = response.headers.get("content-type", "")
                if "json" not in ct.lower():
                    return
                data = await response.json()
                captured.append({"url": url, "data": data})
            except Exception:
                pass

        self.page.on("response", _on_response)
        try:
            await asyncio.sleep(duration / 1000)
        finally:
            try:
                self.page.off("response", _on_response)
            except Exception:
                pass
        return captured

    # --- Utility ---

    def get_page(self) -> Page:
        return self.page

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()


class BrowserTab:
    """Lightweight tab proxy sharing parent browser context."""

    def __init__(self, page: Page, parent: BrowserTool):
        self.page = page
        self._parent = parent
        self.context = parent.context

    async def navigate(self, url: str, wait_until: str = "networkidle",
                       timeout: int = 30000) -> None:
        try:
            await self.page.goto(url, wait_until=wait_until, timeout=timeout)
        except Exception as e:
            if wait_until == "networkidle" and "Timeout" in str(e):
                await self.page.goto(url, wait_until="load", timeout=timeout)
            else:
                raise

    async def get_html(self) -> str:
        return await self.page.content()

    async def get_text(self) -> str:
        return await self.page.inner_text("body")

    async def take_screenshot(self, path: str | None = None,
                              full_page: bool = False) -> bytes:
        if path:
            await self.page.screenshot(path=path, full_page=full_page)
            with open(path, "rb") as f:
                return f.read()
        return await self.page.screenshot(full_page=full_page)

    async def close(self) -> None:
        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass
            self.page = None

    def get_page(self) -> Page:
        return self.page
