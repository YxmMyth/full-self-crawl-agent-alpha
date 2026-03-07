"""S1: DDG search signal — site:domain scoped queries."""

import logging
from urllib.parse import urlparse

logger = logging.getLogger("discovery.search_signal")

_COMMON_QUERY_SUFFIXES = [
    "",          # base requirement only
    "examples",
    "tutorial",
    "best",
]


async def search_signal(domain: str, requirement: str, max_results: int = 20) -> list[dict]:
    """Search DDG with site:domain scope.

    Returns list of {url, title, snippet, rank} dicts sorted by rank (1=best).
    Non-fatal: returns [] on any error.
    """
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            logger.warning("ddgs/duckduckgo_search not installed; search signal disabled")
            return []

    query = f"site:{domain} {requirement}"
    results = []
    try:
        with DDGS() as ddg:
            raw = list(ddg.text(query, max_results=max_results))
        for rank, r in enumerate(raw, start=1):
            url = r.get("href", "")
            if not url:
                continue
            # Ensure result is actually on target domain
            netloc = urlparse(url).netloc.lstrip("www.")
            base = domain.lstrip("www.")
            if base not in netloc:
                continue
            results.append({
                "url": url,
                "title": r.get("title", ""),
                "snippet": r.get("body", ""),
                "rank": rank,
                "score": 1.0 / rank,
            })
    except Exception as e:
        logger.debug(f"Search signal error (non-fatal): {e}")

    logger.info(f"Search signal: {len(results)} results for '{query[:60]}'")
    return results
