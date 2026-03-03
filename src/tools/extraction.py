"""
Extraction Tools — CSS extraction and SPA API interception.

Extracted from old project's act.py and spa_handler.py.
Key preserved logic:
- _sanitize_selector: Handles LLM-generated @attr and ::attr() CSS patterns
- _extract_list_from_json: JSON envelope key extraction for SPA responses
- Container fallback logic for overly broad selectors
"""

import asyncio
import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urljoin

logger = logging.getLogger("tools.extraction")


# ---------------------------------------------------------------------------
# JSON envelope keys for SPA API responses (priority order)
# ---------------------------------------------------------------------------
_LIST_ENVELOPE_KEYS = [
    "data", "items", "results", "list", "records",
    "rows", "content", "entries", "products", "articles",
]

# Field names containing these hints should extract raw HTML, not text
_HTML_FIELD_HINTS = {"html", "code", "source", "markup", "template", "structure", "snippet"}


# ---------------------------------------------------------------------------
# Selector sanitization (from ActAgent._sanitize_selector)
# ---------------------------------------------------------------------------

def sanitize_selector(selector: str) -> tuple[str, str | None]:
    """Fix common LLM-generated CSS selector errors.

    LLM often generates formats like:
    - "a.card@href" → (selector="a.card", attr="href")
    - "img::attr(src)" → (selector="img", attr="src")  (Scrapy style)
    - "div.item{}" → (selector="div.item", attr=None)

    Returns:
        (clean_selector, target_attribute_or_None)
    """
    if not selector or not isinstance(selector, str):
        return (selector or "", None)

    selector = selector.strip()
    target_attr = None

    # Pattern 1: sel@attr — e.g. "a.card@href", "img@src"
    if "@" in selector:
        parts = selector.rsplit("@", 1)
        selector = parts[0].strip()
        target_attr = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None

    # Pattern 2: sel::attr(name) — Scrapy style, not valid CSS
    attr_match = re.match(r"^(.+?)::attr\(([^)]+)\)$", selector)
    if attr_match:
        selector = attr_match.group(1).strip()
        target_attr = attr_match.group(2).strip()

    # Remove stray {} characters
    selector = re.sub(r"[{}]", "", selector)

    return (selector, target_attr)


def _should_extract_html(field_name: str) -> bool:
    """Check if a field should extract raw HTML instead of text."""
    return any(hint in field_name.lower() for hint in _HTML_FIELD_HINTS)


# ---------------------------------------------------------------------------
# CSS Extraction (from ActAgent._extract_simple)
# ---------------------------------------------------------------------------

async def extract_with_css(
    browser,
    selectors: dict[str, str],
    container: str | None = None,
) -> list[dict[str, Any]]:
    """Extract structured data using CSS selectors.

    Preserves key logic from old ActAgent:
    - Selector sanitization (@attr, ::attr() patterns)
    - Container fallback (if container too broad, use first selector's parent)
    - HTML field detection (extract raw HTML for code/template fields)
    - Global fallback search when container-scoped search fails

    Args:
        browser: BrowserTool instance with active page.
        selectors: {field_name: css_selector} mapping.
        container: Optional container CSS selector to scope extraction.

    Returns:
        List of extracted records (dicts with field_name keys).
    """
    from bs4 import BeautifulSoup

    html = await browser.get_html()
    soup = BeautifulSoup(html, "html.parser")

    # Find container elements
    if container:
        container_sel, _ = sanitize_selector(container)
        items = soup.select(container_sel) if container_sel else [soup]
    else:
        items = [soup]

    # Container fallback: if too broad (single body/html/main), try first selector's parent
    if len(items) == 1 and items[0].name in ("html", "body", "main", "div") and selectors:
        raw_first = next(iter(selectors.values()), None)
        if raw_first:
            first_sel, _ = sanitize_selector(raw_first)
            sub_items = soup.select(first_sel) if first_sel else []
            if len(sub_items) > 1:
                parents = []
                seen = set()
                for el in sub_items:
                    if el.parent and id(el.parent) not in seen:
                        seen.add(id(el.parent))
                        parents.append(el.parent)
                if parents:
                    items = parents

    extracted = []
    for item in items:
        data = {}
        for field_name, selector in selectors.items():
            try:
                clean_sel, target_attr = sanitize_selector(selector)
                if not clean_sel:
                    data[field_name] = ""
                    continue

                element = item.select_one(clean_sel)
                # Fallback: search globally if not found in container
                if not element:
                    element = soup.select_one(clean_sel)

                if element:
                    if target_attr:
                        value = element.get(target_attr, "") or element.get_text().strip()
                    elif element.has_attr("href") or element.has_attr("src"):
                        value = element.get("href") or element.get("src") or element.get_text().strip()
                    elif _should_extract_html(field_name):
                        value = str(element)
                    else:
                        value = element.get_text().strip()
                else:
                    value = ""

                data[field_name] = value
            except Exception as e:
                logger.debug(f"Field '{field_name}' extraction failed: {e}")
                data[field_name] = ""

        # Only add records with at least one non-empty field
        if any(v for v in data.values() if v):
            extracted.append(data)

    return extracted


# Tool wrapper (called by registry with browser injected by orchestrator)
async def extract_css_tool(
    browser,
    selectors: dict[str, str],
    container: str | None = None,
) -> dict[str, Any]:
    """Tool-compatible wrapper for extract_with_css."""
    try:
        records = await extract_with_css(browser, selectors, container)
        return {
            "success": True,
            "records": records,
            "count": len(records),
        }
    except Exception as e:
        return {"success": False, "records": [], "count": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# SPA API Interception (from SPAHandler)
# ---------------------------------------------------------------------------

def extract_list_from_json(obj: Any, depth: int = 0) -> list[dict] | None:
    """Extract list of dicts from JSON response envelope.

    Checks common envelope keys (data, items, results, list, records...)
    in priority order. Only returns lists where all elements are dicts.
    """
    if depth > 3:
        return None

    if isinstance(obj, list):
        if obj and all(isinstance(item, dict) for item in obj):
            return obj
        return None

    if isinstance(obj, dict):
        for key in _LIST_ENVELOPE_KEYS:
            value = obj.get(key)
            if value is not None:
                result = extract_list_from_json(value, depth + 1)
                if result:
                    return result
        # Fallback: search all values
        for value in obj.values():
            result = extract_list_from_json(value, depth + 1)
            if result:
                return result

    return None


def _is_json_content_type(content_type: str) -> bool:
    return "json" in content_type.lower()


def _is_api_url(url: str) -> bool:
    """Heuristic: does URL look like an API endpoint?"""
    patterns = [
        r"/api/", r"/v\d+/", r"/rest/", r"/graphql",
        r"\.json", r"/data/", r"/feed", r"/search", r"/query",
    ]
    url_lower = url.lower()
    return any(re.search(p, url_lower) for p in patterns)


async def intercept_api(
    browser,
    url_pattern: str | None = None,
    action: str | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
    """Intercept SPA/AJAX API responses.

    Registers a Playwright response listener, optionally performs an action
    (scroll/wait/click), then collects JSON responses.

    Args:
        browser: BrowserTool with active page.
        url_pattern: Regex pattern to filter API URLs.
        action: Action during interception: "scroll", "wait", "click:<selector>".
        timeout: How long to listen for responses (seconds).

    Returns:
        {"success": bool, "records": list[dict], "api_urls": list[str]}
    """
    page = browser.page if hasattr(browser, "page") else browser.get_page()
    if not page:
        return {"success": False, "records": [], "api_urls": [], "error": "No active page"}

    collected_records: list[dict] = []
    api_urls: list[str] = []

    async def _on_response(response):
        try:
            url = response.url
            if "callback=" in url:
                return
            ct = response.headers.get("content-type", "")
            if not _is_json_content_type(ct):
                return
            if url_pattern and not re.search(url_pattern, url):
                if not _is_api_url(url):
                    return
            elif not _is_api_url(url) and not url_pattern:
                return

            body = await response.text()
            obj = json.loads(body)
            items = extract_list_from_json(obj)
            if items:
                collected_records.extend(items)
            if url not in api_urls:
                api_urls.append(url)
        except json.JSONDecodeError:
            pass
        except Exception as e:
            logger.debug(f"API intercept skipped {url if 'url' in dir() else '?'}: {e}")

    page.on("response", _on_response)
    try:
        # Perform action during intercept window
        if action:
            if action == "scroll":
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            elif action == "wait":
                pass  # Just wait for timeout
            elif action.startswith("click:"):
                selector = action[6:]
                try:
                    await page.click(selector, timeout=5000)
                except Exception:
                    logger.debug(f"Click action failed: {selector}")

        await asyncio.sleep(timeout)
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    return {
        "success": len(collected_records) > 0,
        "records": collected_records,
        "count": len(collected_records),
        "api_urls": api_urls,
    }
