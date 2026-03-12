"""
Management: Scheduler — URL frontier for multi-page crawling.

Two frontier classes:
- CrawlFrontier: simple priority queue for single_page mode (unchanged)
- SharedFrontier: full-run state machine shared between Explorer and Extractor
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from ..utils.url import normalize_url

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
        url = normalize_url(url)

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
        self._visited.add(normalize_url(url))

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


# ---------------------------------------------------------------------------
# SharedFrontier — full-run URL state machine (Explorer + Extractor share it)
# ---------------------------------------------------------------------------

class URLStatus(Enum):
    QUEUED     = "queued"      # ready to be extracted
    IN_FLIGHT  = "in_flight"   # currently being processed
    SAMPLED    = "sampled"     # Explorer extracted 1 record (Phase 2 skips)
    EXTRACTED  = "extracted"   # fully extracted
    FAILED     = "failed"      # extraction failed or yielded nothing


@dataclass
class URLRecord:
    url: str
    status: URLStatus = URLStatus.QUEUED
    url_type: str = "content"       # "content" | "listing"
    priority: float = 1.0
    records_count: int = 0
    failure_reason: str = ""
    discovered_by: str = "explorer" # "phase0_search" | "phase0_sitemap" | "explorer" | "seed"
    extraction_hint: str = ""


@dataclass
class QualitySignals:
    consecutive_failures: int = 0
    total_extracted: int = 0
    total_failed: int = 0
    empty_rate: float = 0.0

    def needs_reexplore(self) -> bool:
        """True when extraction results are poor enough to warrant more exploration."""
        return (
            self.consecutive_failures >= 3
            or (self.total_extracted >= 5 and self.empty_rate > 0.6)
        )


class SharedFrontier:
    """URL state machine shared between Explorer (Phase 1) and Extractor (Phase 2).

    Explorer writes discovered URLs via add()/add_batch().
    Orchestrator drives extraction via next()/mark_extracted()/mark_failed().
    Explorer can mark URLs SAMPLED when it extracts a validation record.
    Quality signals drive re-exploration decisions.
    """

    def __init__(self, max_urls: int = 300, spec=None):
        self._records: dict[str, URLRecord] = {}
        self._all_data: list[dict] = []
        self._seen_fingerprints: set[str] = set()
        self._max_urls = max_urls
        self._consecutive_failures = 0
        self._total_extracted = 0
        self._spec = spec  # CrawlSpec for spec-aware validation
        # Per-section coverage tracking (B1.4)
        self._section_records: dict[str, int] = {}
        self._url_to_section: dict[str, str] = {}

    def seed_from_intel(self, site_intel, start_url: str) -> int:
        """Pre-populate from Phase 0 SiteIntelligence + start URL.

        Only seeds direct_content (search-validated content pages) into the
        extraction queue. entry_points (listing/search pages) are intentionally
        excluded — the Explorer navigates them via site_intel context to discover
        more content URLs.
        """
        added = 0
        if site_intel and site_intel.direct_content:
            for scored in site_intel.direct_content:
                if self.add(scored.url, priority=2.5, discovered_by="phase0_search"):
                    added += 1
        self.add(start_url, priority=1.0, discovered_by="seed")
        return added

    def add(self, url: str, priority: float = 1.0, url_type: str = "content",
            discovered_by: str = "explorer", extraction_hint: str = "") -> bool:
        """Add URL. Returns False if duplicate or at capacity."""
        url = normalize_url(url)
        if not url or url in self._records:
            return False
        if len(self._records) >= self._max_urls:
            return False
        self._records[url] = URLRecord(
            url=url, status=URLStatus.QUEUED, url_type=url_type,
            priority=priority, discovered_by=discovered_by,
            extraction_hint=extraction_hint,
        )
        return True

    def add_batch(self, urls: list, priority: float = 1.0,
                  discovered_by: str = "explorer") -> int:
        """Add multiple URLs. Each item can be str or dict with 'url' key."""
        added = 0
        for item in urls:
            if isinstance(item, dict):
                url = item.get("url", "")
                p = item.get("priority", priority)
                hint = item.get("hint", "")
            else:
                url = str(item)
                p = priority
                hint = ""
            if url and self.add(url, priority=p, discovered_by=discovered_by,
                                extraction_hint=hint):
                added += 1
        return added

    def next(self) -> URLRecord | None:
        """Return highest-priority QUEUED URL, preferring under-covered sections.

        Sorting key: (section_boost, priority).
        section_boost = 1.0 if the URL belongs to a section with 0 records,
        0.5 if it has no section association, 0.0 otherwise.
        This ensures zero-record sections get extracted first without
        penalizing URLs that have no section association.
        """
        candidates = [r for r in self._records.values() if r.status == URLStatus.QUEUED]
        if not candidates:
            return None

        def _sort_key(r: URLRecord):
            section = self._url_to_section.get(r.url)
            if section is not None and section in self._section_records:
                section_boost = 1.0 if self._section_records[section] == 0 else 0.0
            else:
                section_boost = 0.5  # no association — neutral, don't penalize
            return (section_boost, r.priority)

        return max(candidates, key=_sort_key)

    def mark_in_flight(self, url: str) -> None:
        url = normalize_url(url)
        if url in self._records:
            self._records[url].status = URLStatus.IN_FLIGHT

    def mark_extracted(self, url: str, records_count: int,
                       new_data: list[dict] | None = None) -> None:
        """Mark URL as fully extracted. Appends deduplicated records to all_data."""
        url = normalize_url(url)
        if url not in self._records:
            self.add(url, discovered_by="extractor")
        rec = self._records[url]
        # Don't override a successful bypass-extraction with an empty dispatched result.
        if rec.status == URLStatus.EXTRACTED and rec.records_count > 0 and records_count == 0:
            return
        rec.status = URLStatus.EXTRACTED
        rec.records_count = records_count
        self._total_extracted += 1
        self._consecutive_failures = 0 if records_count > 0 else self._consecutive_failures + 1
        if new_data:
            self._ingest_data(new_data)
        # Update per-section coverage (B1.4)
        section = self._url_to_section.get(url)
        if section and section in self._section_records and records_count > 0:
            self._section_records[section] += records_count

    def mark_sampled(self, url: str, records_count: int = 1,
                     new_data: list[dict] | None = None) -> None:
        """Explorer extracted a validation sample — Phase 2 will skip this URL."""
        url = normalize_url(url)
        if url not in self._records:
            self.add(url, discovered_by="explorer")
        rec = self._records[url]
        rec.status = URLStatus.SAMPLED
        rec.records_count = records_count
        if new_data:
            self._ingest_data(new_data)

    def get_status(self, url: str) -> "URLStatus | None":
        """Public method to check URL status. Returns None if URL not in frontier."""
        norm = normalize_url(url)
        rec = self._records.get(norm)
        return rec.status if rec else None

    # ------------------------------------------------------------------
    # Per-section coverage tracking (B1.4)
    # ------------------------------------------------------------------

    def register_section(self, url: str) -> None:
        """Register a section URL for coverage tracking. Initializes count to 0."""
        url = normalize_url(url)
        if url and url not in self._section_records:
            self._section_records[url] = 0

    def associate_url_with_section(self, content_url: str, section_url: str) -> None:
        """Associate a content URL with its parent section for coverage counting."""
        content_url = normalize_url(content_url)
        section_url = normalize_url(section_url)
        if content_url and section_url:
            self._url_to_section[content_url] = section_url

    def section_coverage(self) -> dict[str, int]:
        """Return a copy of per-section record counts."""
        return dict(self._section_records)

    def mark_failed(self, url: str, reason: str = "") -> None:
        url = normalize_url(url)
        if url not in self._records:
            self.add(url, discovered_by="extractor")
        rec = self._records[url]
        rec.status = URLStatus.FAILED
        rec.failure_reason = reason
        self._consecutive_failures += 1

    def quality_signals(self) -> QualitySignals:
        extracted = [r for r in self._records.values() if r.status == URLStatus.EXTRACTED]
        failed_count = sum(1 for r in extracted if r.records_count == 0)
        empty_rate = failed_count / len(extracted) if extracted else 0.0
        return QualitySignals(
            consecutive_failures=self._consecutive_failures,
            total_extracted=self._total_extracted,
            total_failed=failed_count,
            empty_rate=empty_rate,
        )

    def get_failure_summary(self) -> str:
        """Human-readable summary of failed URLs for Explorer re-exploration context."""
        failed = [
            r for r in self._records.values()
            if r.status == URLStatus.FAILED
            or (r.status == URLStatus.EXTRACTED and r.records_count == 0)
        ]
        if not failed:
            return ""
        urls = [r.url for r in failed[:10]]
        return (
            f"{len(failed)} URLs yielded no data: {urls}. "
            "Try different sections, different URL patterns, or listing pages."
        )

    def all_data(self) -> list[dict]:
        """All deduplicated extracted records accumulated so far."""
        return list(self._all_data)

    def stats(self) -> dict:
        counts: dict[str, int] = {}
        for r in self._records.values():
            counts[r.status.value] = counts.get(r.status.value, 0) + 1
        return {
            "total_urls": len(self._records),
            "status_counts": counts,
            "total_records": len(self._all_data),
            "consecutive_failures": self._consecutive_failures,
        }

    # Field names that represent page location/identity, not extracted content.
    # These are always non-empty on any page, so counting them as "substantive"
    # would let empty listing-page records pass the quality check.
    # This list uses universal web metadata naming conventions, not site-specific knowledge.
    _STRUCTURAL_FIELD_NAMES = frozenset({
        "url", "page_url", "href", "link", "path", "slug",
        "id", "permalink", "canonical", "source_url",
    })

    @staticmethod
    def _is_substantive(record: dict, target_fields: list[str]) -> bool:
        """True if at least one CONTENT target field has real, non-placeholder content.

        Structural metadata fields (url, id, slug, href…) are excluded from the
        check — they are always non-empty and represent page location, not content.
        Only fields that represent actual extracted data count toward substantiveness.

        Falls back to checking all target_fields when every target_field happens
        to be structural (edge case: user explicitly collecting only URLs/IDs).
        """
        _EMPTY = {"", "n/a", "none", "null", "undefined", "unknown", "na"}

        # Separate content fields from structural-metadata fields
        content_fields = [
            f for f in target_fields
            if f.lower() not in SharedFrontier._STRUCTURAL_FIELD_NAMES
        ]

        # If spec only has structural fields (e.g. url-collection task), fall back to all
        check_fields = content_fields if content_fields else target_fields

        return any(
            str(record.get(f, "")).strip().lower() not in _EMPTY
            for f in check_fields
        )

    def _ingest_data(self, data: list[dict]) -> None:
        """Ingest records into all_data with dedup and spec-aware validation."""
        target_fields: list[str] = []
        if self._spec and self._spec.target_fields:
            target_fields = [
                f["name"] for f in self._spec.target_fields if isinstance(f, dict)
            ]

        for r in data:
            if not isinstance(r, dict):
                continue
            # Reject records with no substantive content across any target field.
            # With no spec (target_fields empty), accept all non-empty records.
            if target_fields and not self._is_substantive(r, target_fields):
                logger.debug("Dropping record: no substantive content in target fields")
                continue
            key_fields = (
                [f["name"] for f in self._spec.target_fields[:3]]
                if self._spec and self._spec.target_fields
                else list(r.keys())[:3]
            )
            fp = tuple(str(r.get(f, ""))[:120] for f in key_fields)
            if fp not in self._seen_fingerprints:
                self._seen_fingerprints.add(fp)
                self._all_data.append(r)
