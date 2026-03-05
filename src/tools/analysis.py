"""
Analysis Tools — Page structure analysis, link discovery, and search.

Extracted from old project's sense.py, explore.py, and smart_router.py.
Key preserved logic:
- Page type detection (list/detail/static)
- SPA framework detection
- Pagination detection (structural, not text-based)
- Anti-bot indicator detection
- Link extraction, filtering, and categorization
- Sitemap discovery
"""

import logging
import re
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

logger = logging.getLogger("tools.analysis")


# Static resource extensions to filter out during link analysis
_STATIC_EXTENSIONS = {
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".mp3", ".mp4", ".avi",
    ".pdf", ".zip", ".tar", ".gz",
}


def _is_static_resource(url: str) -> bool:
    """Check if URL points to a static resource."""
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _STATIC_EXTENSIONS)


# ---------------------------------------------------------------------------
# Page Analysis (from SenseAgent._extract_features + SmartRouter.FeatureDetector)
# ---------------------------------------------------------------------------

async def analyze_page(browser=None, html: str | None = None) -> dict[str, Any]:
    """Analyze page structure and features.

    Combines programmatic analysis from SenseAgent and FeatureDetector.
    Does NOT use LLM — that's the controller's job.

    Args:
        browser: BrowserTool (will get HTML if html not provided).
        html: Raw HTML string (if already available).

    Returns:
        Dict with page_type, is_spa, has_pagination, anti_bot_level,
        complexity, container_info, pagination_info, estimated_items, etc.
    """
    from bs4 import BeautifulSoup

    if html is None:
        if browser is None:
            return {"error": "Either browser or html must be provided"}
        html = await browser.get_html()

    soup = BeautifulSoup(html, "html.parser")

    # --- Page type detection ---
    page_type = "static"
    nav = soup.find("nav") or soup.find(class_=lambda x: x and "nav" in x.lower() if x else False)
    article = soup.find("article") or soup.find(class_=lambda x: x and "content" in x.lower() if x else False)
    if nav:
        page_type = "list"
    elif article:
        page_type = "detail"

    # --- SPA detection ---
    scripts = soup.find_all("script", src=True)
    spa_patterns = [r"\.chunk\.js", r"vendor\.\w+\.js", r"app\.\w+\.js",
                    r"react", r"vue", r"angular", r"webpack"]
    is_spa = any(
        re.search(p, s.get("src", ""), re.IGNORECASE)
        for s in scripts for p in spa_patterns
    )

    # Detect specific framework
    spa_framework = "none"
    all_script_src = " ".join(s.get("src", "") for s in scripts)
    if re.search(r"react", all_script_src, re.IGNORECASE):
        spa_framework = "react"
    elif re.search(r"vue", all_script_src, re.IGNORECASE):
        spa_framework = "vue"
    elif re.search(r"angular", all_script_src, re.IGNORECASE):
        spa_framework = "angular"

    # --- Pagination detection (structural, not text-based) ---
    pagination_info = {"next_url": None, "has_next": False}
    has_pagination = False

    next_link = soup.find("a", rel=lambda r: r and "next" in r)
    if next_link:
        pagination_info["next_url"] = next_link.get("href")
        pagination_info["has_next"] = True
        has_pagination = True
    else:
        for sel in ['[class*="pager"]', '[class*="pagination"]',
                    '[id*="pager"]', '[id*="pagination"]']:
            if soup.select(sel):
                has_pagination = True
                pagination_info["has_next"] = True
                break

    # --- Main content selector ---
    main_content_selector = None
    for sel in ["main", '[role="main"]', ".main-content", ".content",
                "#content", ".post", ".article"]:
        if soup.select(sel):
            main_content_selector = sel
            break

    # --- Container / list item detection ---
    container_info = {"found": False, "selector": None, "count": 0}
    estimated_items = 0
    for tag in ["ul", "ol", "table"]:
        container = soup.find(tag)
        if container:
            child_tag = "li" if tag in ("ul", "ol") else "tr"
            items = container.find_all(child_tag)
            if len(items) >= 3:
                container_info = {"found": True, "selector": tag, "count": len(items)}
                estimated_items = len(items)
                if not main_content_selector:
                    main_content_selector = tag
                break

    # --- Complexity ---
    script_style_count = len(soup.find_all(["script", "style"]))
    text_diversity = len(set(soup.get_text())) // 100
    complexity_score = script_style_count + text_diversity
    complexity = "simple" if complexity_score < 20 else ("medium" if complexity_score < 50 else "complex")

    # --- Anti-bot detection ---
    anti_bot_keywords = [
        "captcha", "turnstile", "cloudflare", "rate limit", "access denied",
        "blocked", "challenge", "verify you are human",
    ]
    html_lower = html.lower()
    anti_bot_detected = any(kw in html_lower for kw in anti_bot_keywords)

    return {
        "page_type": page_type,
        "is_spa": is_spa,
        "spa_framework": spa_framework,
        "has_pagination": has_pagination,
        "pagination_info": pagination_info,
        "main_content_selector": main_content_selector,
        "estimated_items": estimated_items,
        "container_info": container_info,
        "complexity": complexity,
        "anti_bot_detected": anti_bot_detected,
        "anti_bot_level": "none" if not anti_bot_detected else "medium",
        "dom_size": len(html),
        "link_count": len(soup.find_all("a")),
        "image_count": len(soup.find_all("img")),
    }


# ---------------------------------------------------------------------------
# Link Analysis (from ExploreAgent)
# ---------------------------------------------------------------------------

async def analyze_links(
    browser=None,
    html: str | None = None,
    base_url: str = "",
) -> dict[str, Any]:
    """Analyze and categorize page links.

    Extracts links, filters static resources and off-domain URLs,
    and categorizes them (detail/list/other).

    Args:
        browser: BrowserTool (will get HTML and URL if not provided).
        html: Raw HTML (optional).
        base_url: Base URL for resolving relative links.

    Returns:
        {"links": [...], "total": int, "by_category": {...}}
    """
    from bs4 import BeautifulSoup

    if html is None:
        if browser is None:
            return {"links": [], "total": 0, "by_category": {}}
        html = await browser.get_html()
        if not base_url and hasattr(browser, "page") and browser.page:
            base_url = browser.page.url

    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc if base_url else ""

    links = []
    seen_urls = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        # Resolve relative URL
        full_url = urljoin(base_url, href) if base_url else href

        # Filter
        parsed = urlparse(full_url)
        if base_domain and parsed.netloc and parsed.netloc != base_domain:
            continue
        if _is_static_resource(full_url):
            continue
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        text = anchor.get_text(strip=True)[:100]
        category = _categorize_link(full_url, text)

        links.append({
            "url": full_url,
            "text": text,
            "category": category,
        })

    by_category: dict[str, int] = {}
    for link in links:
        cat = link["category"]
        by_category[cat] = by_category.get(cat, 0) + 1

    # Smart truncation: when many links, prioritise detail > list > other, drop nav
    # This keeps LLM context clean and signal-to-noise high
    MAX_LINKS = 40
    if len(links) > MAX_LINKS:
        priority_order = ["detail", "list", "other"]
        truncated: list[dict] = []
        for cat in priority_order:
            for lnk in links:
                if lnk["category"] == cat and len(truncated) < MAX_LINKS:
                    truncated.append(lnk)
        omitted = len(links) - len(truncated)
        nav_count = by_category.get("nav", 0)
        return {
            "links": truncated,
            "total": len(links),
            "shown": len(truncated),
            "omitted": omitted,
            "by_category": by_category,
            "note": (
                f"Showing {len(truncated)} of {len(links)} links (detail-first). "
                f"{nav_count} nav/utility links excluded. "
                "Focus on 'detail' category links for extraction targets."
            ),
        }

    return {
        "links": links,
        "total": len(links),
        "shown": len(links),
        "by_category": by_category,
    }


def _categorize_link(url: str, text: str) -> str:
    """Rule-based link categorization.

    Returns: "detail" | "list" | "nav" | "other"
    """
    path = urlparse(url).path.lower()
    path_segments = [s for s in path.split("/") if s]

    # Nav/utility paths — exclude from exploration targets (generic across all sites)
    _NAV_SEGMENTS = {
        "login", "signin", "signup", "register", "logout",
        "about", "contact", "help", "faq", "support", "docs", "documentation",
        "pricing", "terms", "privacy", "policy", "legal",
        "blog", "news", "press", "careers", "jobs",
        "trending", "explore", "discover", "popular", "featured",
        "settings", "account", "profile", "dashboard",
        "search", "results",
    }
    if path_segments and path_segments[-1] in _NAV_SEGMENTS:
        return "nav"
    if len(path_segments) == 1 and path_segments[0] in _NAV_SEGMENTS:
        return "nav"

    # Detail page patterns (content item pages)
    detail_patterns = [
        r"/\d{4,}",                  # Numeric ID
        r"/article/", r"/post/", r"/detail/", r"/item/",
        r"/product/", r"/news/",
        r"/pen/", r"/repo/", r"/project/", r"/snippet/", r"/gist/",
        r"/watch/", r"/video/", r"/track/", r"/episode/",
        r"/question/", r"/answer/",
        r"/story/", r"/entry/",
    ]
    for p in detail_patterns:
        if re.search(p, path):
            return "detail"

    # 3-segment paths typically mean user/type/id — detail pages on content platforms
    if len(path_segments) >= 3:
        return "detail"

    # List/index patterns
    list_patterns = [
        r"/page/\d+", r"\?page=", r"/category/", r"/tag/",
        r"/list", r"/archive", r"/index", r"/collection/",
        r"/search/",
    ]
    for p in list_patterns:
        if re.search(p, path) or re.search(p, url):
            return "list"

    return "other"


# ---------------------------------------------------------------------------
# Search Page (text search in visible content)
# ---------------------------------------------------------------------------

async def search_page_tool(
    browser,
    query: str,
) -> dict[str, Any]:
    """Search visible page text for a query string.

    Uses browser's search_page method.
    """
    if hasattr(browser, "search_page"):
        results = await browser.search_page(query)
        return {"success": True, "matches": results, "count": len(results)}
    return {"success": False, "matches": [], "count": 0, "error": "search not available"}
