"""
Phase 1.5: Listing Sampler — deterministic URL discovery from listing pages.
"""

import pytest
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

from src.management.orchestrator import Orchestrator
from src.management.scheduler import SharedFrontier
from src.management.run_intelligence import RunIntelligence
from src.discovery.types import ScoredURL
from src.strategy.spec import CrawlSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec():
    return CrawlSpec(url="https://example.com", requirement="Get items",
                     target_fields=[{"name": "title"}, {"name": "description"}])


def _make_frontier(spec=None):
    return SharedFrontier(max_urls=300, spec=spec or _make_spec())


def _make_run_intelligence():
    d = tempfile.mkdtemp()
    ri = RunIntelligence(d)
    ri.initialize()
    return ri


def _make_entry_points(urls):
    return [ScoredURL(url=u, score=1.0, source="search", url_type="entry_point") for u in urls]


def _make_orchestrator():
    """Create a minimal Orchestrator with a mocked browser."""
    orch = Orchestrator.__new__(Orchestrator)
    orch._browser = AsyncMock()
    orch._frontier = None
    orch._run_intelligence = None
    orch._seen_urls = set()
    return orch


def _link(url, category="detail"):
    return {"url": url, "text": "link", "category": category}


# ===========================================================================
# Tests
# ===========================================================================

class TestListingSamplerFilter:
    """Unit tests for _listing_sampler_filter (sync, no browser needed)."""

    def test_detail_links_included(self):
        orch = _make_orchestrator()
        ri = _make_run_intelligence()
        links = [
            _link("https://example.com/item/1", "detail"),
            _link("https://example.com/item/2", "detail"),
            _link("https://example.com/about", "nav"),
            _link("https://example.com/page/2", "list"),
        ]
        result = orch._listing_sampler_filter(links, None, ri)
        assert result == ["https://example.com/item/1", "https://example.com/item/2"]

    def test_nav_and_other_excluded(self):
        orch = _make_orchestrator()
        ri = _make_run_intelligence()
        links = [
            _link("https://example.com/about", "nav"),
            _link("https://example.com/misc", "other"),
        ]
        result = orch._listing_sampler_filter(links, None, ri)
        assert result == []

    def test_content_url_pattern_filtering(self):
        orch = _make_orchestrator()
        ri = _make_run_intelligence()
        links = [
            _link("https://example.com/user/pen/slug1", "detail"),
            _link("https://example.com/user/project/slug2", "detail"),
        ]
        # Pattern matches only /*/pen/*
        result = orch._listing_sampler_filter(links, "/*/pen/*", ri)
        assert result == ["https://example.com/user/pen/slug1"]

    def test_pattern_filter_fallback_when_empty(self):
        """If pattern filtering eliminates everything, keep unfiltered detail links."""
        orch = _make_orchestrator()
        ri = _make_run_intelligence()
        links = [
            _link("https://example.com/item/1", "detail"),
            _link("https://example.com/item/2", "detail"),
        ]
        # Pattern matches nothing in these URLs
        result = orch._listing_sampler_filter(links, "/nomatch/*", ri)
        assert result == ["https://example.com/item/1", "https://example.com/item/2"]


@pytest.mark.asyncio
class TestListingSampler:

    async def test_detail_links_injected(self):
        orch = _make_orchestrator()
        frontier = _make_frontier()
        ri = _make_run_intelligence()

        orch._browser.navigate = AsyncMock()
        orch._browser.get_html = AsyncMock(return_value="<html></html>")
        orch._browser.smart_scroll = AsyncMock(return_value={"content_grew": False})

        with patch("src.tools.analysis.analyze_links", new_callable=AsyncMock) as mock_al:
            mock_al.return_value = {
                "links": [
                    _link("https://example.com/item/1"),
                    _link("https://example.com/item/2"),
                    _link("https://example.com/about", "nav"),
                ],
                "total": 3,
            }
            eps = _make_entry_points(["https://example.com/list"])
            added = await orch._run_listing_sampler(eps, frontier, ri)

        assert added == 2
        assert frontier.get_status("https://example.com/item/1") is not None
        assert frontier.get_status("https://example.com/item/2") is not None
        assert frontier.get_status("https://example.com/about") is None

    async def test_pagination_followed(self):
        orch = _make_orchestrator()
        frontier = _make_frontier()
        ri = _make_run_intelligence()

        orch._browser.navigate = AsyncMock()
        orch._browser.get_html = AsyncMock(return_value="<html></html>")
        orch._browser.smart_scroll = AsyncMock(return_value={"content_grew": False})

        call_count = 0

        async def mock_analyze_links(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First page: detail links + pagination link
                return {
                    "links": [
                        _link("https://example.com/item/1"),
                        _link("https://example.com/page/2", "list"),
                    ],
                    "total": 2,
                }
            else:
                # Pagination page: more detail links
                return {
                    "links": [
                        _link("https://example.com/item/2"),
                    ],
                    "total": 1,
                }

        with patch("src.tools.analysis.analyze_links", side_effect=mock_analyze_links):
            eps = _make_entry_points(["https://example.com/list"])
            added = await orch._run_listing_sampler(eps, frontier, ri)

        assert added == 2
        assert frontier.get_status("https://example.com/item/1") is not None
        assert frontier.get_status("https://example.com/item/2") is not None

    async def test_scroll_discovers_new_links(self):
        orch = _make_orchestrator()
        frontier = _make_frontier()
        ri = _make_run_intelligence()

        orch._browser.navigate = AsyncMock()
        orch._browser.get_html = AsyncMock(return_value="<html></html>")
        orch._browser.smart_scroll = AsyncMock(return_value={"content_grew": True})

        call_count = 0

        async def mock_analyze_links(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"links": [_link("https://example.com/item/1")], "total": 1}
            else:
                # After scroll: original + new link
                return {
                    "links": [
                        _link("https://example.com/item/1"),
                        _link("https://example.com/item/2"),
                    ],
                    "total": 2,
                }

        with patch("src.tools.analysis.analyze_links", side_effect=mock_analyze_links):
            eps = _make_entry_points(["https://example.com/list"])
            added = await orch._run_listing_sampler(eps, frontier, ri)

        assert added == 2

    async def test_browser_exception_skips_entry_point(self):
        orch = _make_orchestrator()
        frontier = _make_frontier()
        ri = _make_run_intelligence()

        nav_calls = []

        async def mock_navigate(url, **kwargs):
            nav_calls.append(url)
            if url == "https://example.com/broken":
                raise Exception("Navigation failed")

        orch._browser.navigate = mock_navigate
        orch._browser.get_html = AsyncMock(return_value="<html></html>")
        orch._browser.smart_scroll = AsyncMock(return_value={"content_grew": False})

        with patch("src.tools.analysis.analyze_links", new_callable=AsyncMock) as mock_al:
            mock_al.return_value = {
                "links": [_link("https://example.com/item/1")],
                "total": 1,
            }
            eps = _make_entry_points([
                "https://example.com/broken",
                "https://example.com/working",
            ])
            added = await orch._run_listing_sampler(eps, frontier, ri)

        # Broken entry_point skipped, working one succeeded
        assert added == 1
        assert len(nav_calls) == 2

    async def test_empty_entry_points_returns_zero(self):
        orch = _make_orchestrator()
        frontier = _make_frontier()
        ri = _make_run_intelligence()

        added = await orch._run_listing_sampler([], frontier, ri)
        assert added == 0

    async def test_dedup_handled_by_frontier(self):
        orch = _make_orchestrator()
        frontier = _make_frontier()
        ri = _make_run_intelligence()
        # Pre-add a URL to frontier
        frontier.add("https://example.com/item/1", discovered_by="explorer")

        orch._browser.navigate = AsyncMock()
        orch._browser.get_html = AsyncMock(return_value="<html></html>")
        orch._browser.smart_scroll = AsyncMock(return_value={"content_grew": False})

        with patch("src.tools.analysis.analyze_links", new_callable=AsyncMock) as mock_al:
            mock_al.return_value = {
                "links": [
                    _link("https://example.com/item/1"),  # already in frontier
                    _link("https://example.com/item/2"),  # new
                ],
                "total": 2,
            }
            eps = _make_entry_points(["https://example.com/list"])
            added = await orch._run_listing_sampler(eps, frontier, ri)

        assert added == 1  # only /item/2 is new

    async def test_max_five_entry_points(self):
        orch = _make_orchestrator()
        frontier = _make_frontier()
        ri = _make_run_intelligence()

        nav_calls = []

        async def mock_navigate(url, **kwargs):
            nav_calls.append(url)

        orch._browser.navigate = mock_navigate
        orch._browser.get_html = AsyncMock(return_value="<html></html>")
        orch._browser.smart_scroll = AsyncMock(return_value={"content_grew": False})

        with patch("src.tools.analysis.analyze_links", new_callable=AsyncMock) as mock_al:
            mock_al.return_value = {"links": [], "total": 0}
            eps = _make_entry_points([f"https://example.com/list/{i}" for i in range(10)])
            await orch._run_listing_sampler(eps, frontier, ri)

        assert len(nav_calls) == 5
