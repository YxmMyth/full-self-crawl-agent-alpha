"""Merger: combine multi-signal results into SiteIntelligence.

Scoring weights:
    search  → 0.6  (search engine ranking is the strongest quality signal)
    sitemap → 0.3  (structured but not quality-ranked)
    probe   → 0.1  (existence only, no quality signal)
"""

import logging
from typing import Literal
from urllib.parse import urlparse

from .types import ScoredURL, SiteIntelligence

logger = logging.getLogger("discovery.merger")

# Weight constants
W_SEARCH = 0.6
W_SITEMAP = 0.3
W_PROBE = 0.1

# Keywords that strongly indicate a listing/entry page
_ENTRY_KEYWORDS = {
    "search", "topic", "topics", "tag", "tags", "browse",
    "explore", "trending", "popular", "new", "top", "latest",
    "feed", "index", "directory", "collections", "library",
    "category", "categories", "list", "listing",
}


def classify_url(url: str) -> Literal["entry_point", "content"]:
    """Classify a URL as an entry point (listing/search) or content (detail) page.

    Rules (no LLM):
    1. Path contains entry keywords → entry_point
    2. Query string has pagination/filter params → entry_point
    3. 3+ path segments with slug-like final segment → content
    4. Default → entry_point (short paths are more likely listing pages)
    """
    parsed = urlparse(url)
    path = parsed.path.lower().rstrip("/")
    query = parsed.query.lower()

    # Rule 1: entry keywords anywhere in path
    path_parts = [p for p in path.split("/") if p]
    for part in path_parts:
        if part in _ENTRY_KEYWORDS:
            return "entry_point"

    # Rule 2: pagination/filter query params
    for param in ("q=", "page=", "sort=", "filter=", "search=", "query="):
        if param in query:
            return "entry_point"

    # Rule 3: 3+ segments → likely a detail/content page
    if len(path_parts) >= 3:
        return "content"

    return "entry_point"


def merge(
    search_results: list[dict],      # [{url, score, ...}] from search_signal
    sitemap_results: list[dict],     # [{url, score}] from sitemap_signal
    probe_paths: list[str],          # ["/search", "/topics", ...] from probe_signal
    domain: str,
    top_n: int = 30,
) -> SiteIntelligence:
    """Merge all signals into a SiteIntelligence object.

    Deduplication: canonical URL used as key (lowercased, trailing slash stripped).
    """
    scores: dict[str, float] = {}  # canonical_url → weighted score
    sources: dict[str, str] = {}   # canonical_url → primary source label

    def _canonical(url: str) -> str:
        return url.lower().rstrip("/")

    # S1: search results
    for item in search_results:
        url = item.get("url", "")
        if not url:
            continue
        k = _canonical(url)
        s1 = item.get("score", 0.0) * W_SEARCH
        scores[k] = scores.get(k, 0.0) + s1
        sources.setdefault(k, "search")

    # S2: sitemap candidates
    for item in sitemap_results:
        url = item.get("url", "")
        if not url:
            continue
        k = _canonical(url)
        s2 = item.get("score", 0.0) * W_SITEMAP
        scores[k] = scores.get(k, 0.0) + s2
        sources.setdefault(k, "sitemap")

    # S3: probe paths → build full URLs
    base = f"https://{domain}"
    for path in probe_paths:
        url = f"{base}{path}"
        k = _canonical(url)
        scores[k] = scores.get(k, 0.0) + W_PROBE
        sources.setdefault(k, "probe")

    # Sort by score descending
    sorted_items = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    entry_points: list[ScoredURL] = []
    direct_content: list[ScoredURL] = []

    for k, score in sorted_items[:top_n]:
        # Recover original-case URL (use canonical as fallback)
        url = k
        url_type = classify_url(url)
        scored = ScoredURL(url=url, score=score, source=sources.get(k, "unknown"), url_type=url_type)
        if url_type == "entry_point":
            entry_points.append(scored)
        else:
            direct_content.append(scored)

    logger.info(
        f"Merger: {len(entry_points)} entry_points, {len(direct_content)} direct_content "
        f"from {len(search_results)} search + {len(sitemap_results)} sitemap + {len(probe_paths)} probe"
    )
    return SiteIntelligence(entry_points=entry_points, direct_content=direct_content)
