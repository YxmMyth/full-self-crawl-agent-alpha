"""ProbeEndpointTool: domain-locked HTTP HEAD endpoint prober for Phase 1 agent.

Allows the agent to quickly verify whether a URL path exists
without loading the full page in the browser.
"""

import logging
from urllib.parse import urlparse

logger = logging.getLogger("tools.probe_tool")


class ProbeEndpointTool:
    """Domain-locked HTTP HEAD probe for Phase 1 exploration agent.

    Usage:
        tool = ProbeEndpointTool("news.ycombinator.com")
        result = await tool.run("/new")
    """

    def __init__(self, domain: str):
        self.domain = domain.lstrip("www.")

    async def run(self, path: str, scheme: str = "https") -> dict:
        """HTTP HEAD check for a path on the target domain.

        Args:
            path: URL path to probe, e.g. "/search" or "/topics/threejs"

        Returns:
            {"url": str, "exists": bool, "status": int}
        """
        # Ensure path starts with /
        if not path.startswith("/"):
            path = "/" + path

        url = f"{scheme}://{self.domain}{path}"

        # Safety: ensure we stay on domain
        parsed = urlparse(url)
        netloc = parsed.netloc.lstrip("www.")
        if self.domain not in netloc:
            return {"url": url, "exists": False, "status": 0, "error": "Domain mismatch"}

        try:
            import httpx
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=5,
                headers={"User-Agent": "Mozilla/5.0 (compatible; SiteProbe/1.0)"},
            ) as client:
                r = await client.head(url)
                exists = r.status_code in (200, 301, 302, 307, 308)
                logger.debug(f"probe_endpoint {url} → {r.status_code}")
                return {"url": url, "exists": exists, "status": r.status_code}
        except Exception as e:
            logger.debug(f"probe_endpoint error for {url}: {e}")
            return {"url": url, "exists": False, "status": 0, "error": str(e)}
