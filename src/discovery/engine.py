"""Discovery engine: orchestrates all signals into SiteIntelligence.

Phase 0 — deterministic, no LLM, runs in ~3s:
    S1: search_signal  (DDG site:domain)
    S2: sitemap_signal (robots.txt + sitemap)
    S3: probe_signal   (HTTP HEAD common endpoints)
    S4: nav_signal     (browser Nav render — conditional, only if 0 entry_points)

Output → SiteIntelligence passed to Phase 1 agent as initial briefing.
"""

import asyncio
import logging
from urllib.parse import urlparse

from .merger import merge
from .signals.probe_signal import probe_signal
from .signals.search_signal import search_signal
from .signals.sitemap_signal import sitemap_signal
from .types import SiteIntelligence

logger = logging.getLogger("discovery.engine")


async def discover(
    domain: str,
    requirement: str,
    scheme: str = "https",
    browser=None,  # Optional BrowserTool for S4 nav fallback
) -> SiteIntelligence:
    """Run all discovery signals and return merged SiteIntelligence.

    Args:
        domain:      Bare domain, e.g. "news.ycombinator.com"
        requirement: Natural language data need, e.g. "find popular tech stories"
        scheme:      "https" (default) or "http"
        browser:     Optional BrowserTool; if provided and S1-S3 yield no entry_points,
                     S4 nav analysis will be attempted.

    Returns:
        SiteIntelligence with entry_points, direct_content, live_endpoints,
        sitemap_sample, robots_txt populated.
    """
    # Run S1, S2, S3 in parallel
    logger.info(f"Phase 0 discovery starting: domain={domain}")
    search_task = search_signal(domain, requirement)
    sitemap_task = sitemap_signal(domain, requirement, scheme=scheme)
    probe_task = probe_signal(domain, scheme=scheme)

    search_results, sitemap_data, live_paths = await asyncio.gather(
        search_task, sitemap_task, probe_task,
        return_exceptions=True,
    )

    # Tolerate individual signal failures
    search_degraded = False
    if isinstance(search_results, Exception):
        logger.warning(f"Search signal failed: {search_results}")
        search_results = []
        search_degraded = True
    elif not search_results:
        search_degraded = True
    if isinstance(sitemap_data, Exception):
        logger.warning(f"Sitemap signal failed: {sitemap_data}")
        sitemap_data = {"robots_txt": "", "candidates": [], "sitemap_found": False}
    if isinstance(live_paths, Exception):
        logger.warning(f"Probe signal failed: {live_paths}")
        live_paths = []

    # Merge S1+S2+S3
    site_intel = merge(
        search_results=search_results,
        sitemap_results=sitemap_data.get("candidates", []),
        probe_paths=live_paths,
        domain=domain,
    )

    # Enrich with raw data for agent context
    site_intel.search_degraded = search_degraded
    site_intel.robots_txt = sitemap_data.get("robots_txt", "")
    site_intel.live_endpoints = live_paths
    site_intel.sitemap_sample = [
        c["url"] for c in sitemap_data.get("candidates", [])[:20]
    ]

    # S4: conditional nav analysis (only if no entry_points found)
    if not site_intel.entry_points and browser is not None:
        logger.info("S1-S3 found 0 entry_points; attempting S4 nav analysis")
        try:
            nav_entries = await _nav_signal(domain, scheme, browser)
            if nav_entries:
                from .merger import classify_url
                from .types import ScoredURL
                for path in nav_entries:
                    url = f"{scheme}://{domain}{path}"
                    site_intel.entry_points.append(
                        ScoredURL(url=url, score=0.5, source="nav", url_type="entry_point")
                    )
                logger.info(f"S4 nav signal added {len(nav_entries)} entry points")
        except Exception as e:
            logger.debug(f"S4 nav signal failed (non-fatal): {e}")

    logger.info(
        f"Phase 0 complete: {len(site_intel.entry_points)} entry_points, "
        f"{len(site_intel.direct_content)} direct_content, "
        f"{len(site_intel.live_endpoints)} live_endpoints"
    )
    return site_intel


async def _nav_signal(domain: str, scheme: str, browser) -> list[str]:
    """S4: render homepage and extract navigation links.

    Returns list of paths (e.g. ["/new", "/ask", "/show"]).
    Only called when S1-S3 yield no entry_points.
    """
    base_url = f"{scheme}://{domain}"
    await browser.navigate(base_url, wait_until="networkidle")

    # Extract nav links via JavaScript
    script = """() => {
        const nav = document.querySelector('nav, header, [role="navigation"], #nav, .nav, .navbar, .header');
        const container = nav || document.body;
        const links = Array.from(container.querySelectorAll('a[href]'));
        return links
            .map(a => a.getAttribute('href'))
            .filter(h => h && h.startsWith('/') && h.length > 1 && !h.includes('.'));
    }"""
    result = await browser.evaluate_js(script)
    if isinstance(result, list):
        # Deduplicate and filter
        seen = set()
        paths = []
        for p in result:
            if p not in seen and len(p) < 50:
                seen.add(p)
                paths.append(p)
        return paths[:20]
    return []
