"""S1: DDG search signal — site:domain scoped queries."""

import asyncio
import logging
from urllib.parse import urlparse

from ...utils.url import is_same_domain

logger = logging.getLogger("discovery.search_signal")

_MAX_RETRIES = 3


async def search_signal(domain: str, requirement: str, max_results: int = 20) -> list[dict]:
    """Search DDG with site:domain scope.

    Returns list of {url, title, snippet, rank} dicts sorted by rank (1=best).
    Retries up to 3 times with exponential backoff on failure.
    Non-fatal: returns [] if all attempts fail.
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
    last_error = None

    for attempt in range(_MAX_RETRIES):
        try:
            with DDGS() as ddg:
                raw = list(ddg.text(query, max_results=max_results))
            for rank, r in enumerate(raw, start=1):
                url = r.get("href", "")
                if not url:
                    continue
                # Ensure result is actually on target domain
                netloc = urlparse(url).netloc
                if not is_same_domain(netloc, domain):
                    continue
                results.append({
                    "url": url,
                    "title": r.get("title", ""),
                    "snippet": r.get("body", ""),
                    "rank": rank,
                    "score": 1.0 / rank,
                })
            break  # success
        except Exception as e:
            last_error = e
            if attempt < _MAX_RETRIES - 1:
                delay = (attempt + 1) ** 2  # 1s, 4s
                logger.warning(
                    f"Search signal attempt {attempt + 1}/{_MAX_RETRIES} failed: {e}, "
                    f"retry in {delay}s"
                )
                await asyncio.sleep(delay)
            else:
                logger.warning(
                    f"Search signal failed after {_MAX_RETRIES} attempts: {last_error}"
                )

    logger.info(f"Search signal: {len(results)} results for '{query[:60]}'")
    return results
