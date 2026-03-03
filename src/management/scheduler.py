"""
Management: Scheduler — URL frontier for multi-page crawling.

Manages the queue of URLs to visit, with priority, depth tracking,
and duplicate filtering.
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("management.scheduler")


@dataclass
class CrawlTask:
    """A URL task in the frontier queue."""
    url: str
    depth: int = 0
    priority: float = 1.0
    category: str = "other"  # detail, list, other
    parent_url: str = ""
    added_at: str = field(default_factory=lambda: datetime.now().isoformat())


class CrawlFrontier:
    """URL frontier for multi-page crawl scheduling.

    Features:
    - Priority queue (higher priority first)
    - Depth tracking
    - Duplicate URL filtering
    - Domain scoping
    """

    def __init__(self, max_depth: int = 3, max_urls: int = 1000):
        self.max_depth = max_depth
        self.max_urls = max_urls
        self._queue: list[CrawlTask] = []
        self._visited: set[str] = set()
        self._all_urls: set[str] = set()
        self._base_domain: str = ""

    def set_base_domain(self, url: str) -> None:
        """Set the base domain for same-domain filtering."""
        self._base_domain = urlparse(url).netloc

    def add(self, url: str, depth: int = 0, priority: float = 1.0,
            category: str = "other", parent_url: str = "") -> bool:
        """Add URL to frontier. Returns False if duplicate or filtered."""
        # Normalize
        url = url.split("#")[0].rstrip("/")

        if url in self._all_urls:
            return False
        if len(self._all_urls) >= self.max_urls:
            return False
        if depth > self.max_depth:
            return False

        # Same-domain filter
        if self._base_domain:
            domain = urlparse(url).netloc
            if domain and domain != self._base_domain:
                return False

        task = CrawlTask(
            url=url, depth=depth, priority=priority,
            category=category, parent_url=parent_url,
        )
        self._queue.append(task)
        self._all_urls.add(url)
        # Sort by priority (descending)
        self._queue.sort(key=lambda t: t.priority, reverse=True)
        return True

    def add_batch(self, links: list[dict], depth: int = 0,
                  parent_url: str = "") -> int:
        """Add multiple links. Returns count of newly added."""
        added = 0
        for link in links:
            url = link.get("url", link) if isinstance(link, dict) else str(link)
            cat = link.get("category", "other") if isinstance(link, dict) else "other"
            # Detail pages get higher priority
            priority = 2.0 if cat == "detail" else (1.5 if cat == "list" else 1.0)
            if self.add(url, depth, priority, cat, parent_url):
                added += 1
        return added

    def next(self) -> CrawlTask | None:
        """Get next URL to visit (highest priority, not yet visited)."""
        while self._queue:
            task = self._queue.pop(0)
            if task.url not in self._visited:
                self._visited.add(task.url)
                return task
        return None

    def mark_visited(self, url: str) -> None:
        self._visited.add(url.split("#")[0].rstrip("/"))

    @property
    def pending_count(self) -> int:
        return sum(1 for t in self._queue if t.url not in self._visited)

    @property
    def visited_count(self) -> int:
        return len(self._visited)

    @property
    def total_count(self) -> int:
        return len(self._all_urls)

    def get_stats(self) -> dict:
        return {
            "pending": self.pending_count,
            "visited": self.visited_count,
            "total": self.total_count,
            "max_depth": self.max_depth,
            "base_domain": self._base_domain,
        }
