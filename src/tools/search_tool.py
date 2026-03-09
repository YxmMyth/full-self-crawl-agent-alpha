"""SearchSiteTool: domain-locked DDG search for Phase 1 agent.

Allows the agent to iteratively search within the target domain
with different keywords. Cannot search outside the domain.
"""

import logging
from urllib.parse import urlparse

logger = logging.getLogger("tools.search_tool")


class SearchSiteTool:
    """Domain-locked search tool for Phase 1 exploration agent.

    Usage:
        tool = SearchSiteTool("news.ycombinator.com")
        results = await tool.run("best threejs demos")
    """

    def __init__(self, domain: str, known_urls_fn=None):
        self.domain = domain.lstrip("www.")
        self._known_urls_fn = known_urls_fn  # () -> frozenset[str] of already-extracted URLs

    async def run(self, query: str, max_results: int = 10) -> dict:
        """Search site:{domain} {query} via DDG.

        Args:
            query: Natural language search query (domain prefix added automatically)
            max_results: Maximum number of results (default 10)

        Returns:
            {"results": [{"url", "title", "snippet"}], "query": str, "total": int}
        """
        try:
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS
            except ImportError:
                return {
                    "error": "ddgs/duckduckgo_search not installed",
                    "results": [],
                    "query": query,
                    "total": 0,
                }

        full_query = f"site:{self.domain} {query}"
        results = []
        try:
            with DDGS() as ddg:
                raw = list(ddg.text(full_query, max_results=max_results))
            for r in raw:
                url = r.get("href", "")
                if not url:
                    continue
                # Domain safety check
                netloc = urlparse(url).netloc.lstrip("www.")
                if self.domain not in netloc:
                    continue
                results.append({
                    "url": url,
                    "title": r.get("title", ""),
                    "snippet": r.get("body", "")[:200],
                })
        except Exception as e:
            logger.debug(f"search_site error: {e}")
            return {"error": str(e), "results": [], "query": query, "total": 0}

        logger.info(f"search_site: {len(results)} results for '{full_query[:60]}'")

        # Filter URLs already extracted in this run so the agent sees only fresh pages.
        if self._known_urls_fn:
            known = self._known_urls_fn()
            before = len(results)
            results = [r for r in results if r["url"].split("#")[0].rstrip("/") not in known]
            filtered = before - len(results)
            if filtered:
                logger.info(f"search_site: filtered {filtered} already-extracted URL(s)")

        return {"results": results, "query": full_query, "total": len(results)}
