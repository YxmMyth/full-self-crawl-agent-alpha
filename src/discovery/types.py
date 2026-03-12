"""Discovery types: data structures for multi-signal site intelligence."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ScoredURL:
    url: str
    score: float
    source: str  # "search" | "sitemap" | "probe" | "nav"
    url_type: Literal["entry_point", "content"]


@dataclass
class SiteIntelligence:
    """Output of Phase 0 discovery. Passed to Phase 1 agent as initial context.

    This is a briefing, not instructions. The agent decides what to do with it.
    """
    entry_points: list[ScoredURL] = field(default_factory=list)
    direct_content: list[ScoredURL] = field(default_factory=list)
    live_endpoints: list[str] = field(default_factory=list)   # paths confirmed via HTTP HEAD
    sitemap_sample: list[str] = field(default_factory=list)   # raw URLs from sitemap
    robots_txt: str = ""
    search_degraded: bool = False  # True when DDG search failed after retries
