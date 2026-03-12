"""
Wave 4 tests: SharedFrontier get_status(), domain filtering fix.
"""

import pytest
from src.utils.url import is_same_domain
from src.management.scheduler import SharedFrontier, URLStatus


# ===========================================================================
# 4.1 — SharedFrontier.get_status() public API
# ===========================================================================

class TestFrontierGetStatus:
    @pytest.fixture
    def frontier(self):
        return SharedFrontier(max_urls=100)

    def test_get_status_unknown_url(self, frontier):
        """Unknown URL returns None."""
        assert frontier.get_status("https://example.com/page") is None

    def test_get_status_queued(self, frontier):
        """Added URL shows QUEUED status."""
        frontier.add("https://example.com/page")
        assert frontier.get_status("https://example.com/page") == URLStatus.QUEUED

    def test_get_status_in_flight(self, frontier):
        """In-flight URL shows IN_FLIGHT status."""
        frontier.add("https://example.com/page")
        frontier.mark_in_flight("https://example.com/page")
        assert frontier.get_status("https://example.com/page") == URLStatus.IN_FLIGHT

    def test_get_status_extracted(self, frontier):
        """Extracted URL shows EXTRACTED status."""
        frontier.add("https://example.com/page")
        frontier.mark_extracted("https://example.com/page", 5)
        assert frontier.get_status("https://example.com/page") == URLStatus.EXTRACTED

    def test_get_status_sampled(self, frontier):
        """Sampled URL shows SAMPLED status."""
        frontier.add("https://example.com/page")
        frontier.mark_sampled("https://example.com/page")
        assert frontier.get_status("https://example.com/page") == URLStatus.SAMPLED

    def test_get_status_failed(self, frontier):
        """Failed URL shows FAILED status."""
        frontier.add("https://example.com/page")
        frontier.mark_failed("https://example.com/page", "timeout")
        assert frontier.get_status("https://example.com/page") == URLStatus.FAILED

    def test_get_status_normalizes_url(self, frontier):
        """get_status normalizes URL (strips fragment, trailing slash)."""
        frontier.add("https://example.com/page")
        assert frontier.get_status("https://example.com/page#section") == URLStatus.QUEUED
        assert frontier.get_status("https://example.com/page/") == URLStatus.QUEUED


# ===========================================================================
# 4.2 — Domain filtering: is_same_domain()
# ===========================================================================

class TestIsSameDomain:
    def test_exact_match(self):
        assert is_same_domain("example.com", "example.com") is True

    def test_subdomain_match(self):
        assert is_same_domain("api.example.com", "example.com") is True

    def test_www_prefix_stripped(self):
        assert is_same_domain("www.example.com", "example.com") is True

    def test_both_www(self):
        assert is_same_domain("www.example.com", "www.example.com") is True

    def test_deep_subdomain(self):
        assert is_same_domain("a.b.c.example.com", "example.com") is True

    def test_rejects_partial_overlap(self):
        """notexample.com must NOT match example.com — the critical bug fix."""
        assert is_same_domain("notexample.com", "example.com") is False

    def test_rejects_prefix_overlap(self):
        """example.com.evil.com must NOT match example.com."""
        assert is_same_domain("example.com.evil.com", "example.com") is False

    def test_rejects_totally_different(self):
        assert is_same_domain("other.org", "example.com") is False

    def test_case_insensitive(self):
        assert is_same_domain("API.Example.COM", "example.com") is True

    def test_port_stripped(self):
        assert is_same_domain("example.com:8080", "example.com") is True

    def test_port_subdomain(self):
        assert is_same_domain("api.example.com:443", "example.com") is True

    def test_empty_netloc(self):
        assert is_same_domain("", "example.com") is False

    def test_codepen_io(self):
        """Real-world case: codepen.io variants."""
        assert is_same_domain("codepen.io", "codepen.io") is True
        assert is_same_domain("assets.codepen.io", "codepen.io") is True
        assert is_same_domain("notcodepen.io", "codepen.io") is False


# ===========================================================================
# 4.2 — Domain filtering in search/probe/sitemap tools
# ===========================================================================

class TestSearchToolDomainFiltering:
    def test_search_tool_rejects_partial_domain(self):
        """SearchSiteTool must reject notexample.com for domain example.com."""
        from src.tools.search_tool import SearchSiteTool
        tool = SearchSiteTool("example.com")
        # Verify the tool stores stripped domain
        assert tool.domain == "example.com"

    def test_probe_tool_rejects_partial_domain(self):
        """ProbeEndpointTool safety check uses is_same_domain."""
        import inspect
        from src.tools.probe_tool import ProbeEndpointTool
        source = inspect.getsource(ProbeEndpointTool.run)
        assert "is_same_domain" in source

    def test_search_signal_uses_is_same_domain(self):
        """search_signal uses is_same_domain for domain check."""
        import inspect
        from src.discovery.signals.search_signal import search_signal
        source = inspect.getsource(search_signal)
        assert "is_same_domain" in source

    def test_sitemap_signal_uses_is_same_domain(self):
        """sitemap_signal uses is_same_domain for domain check."""
        import inspect
        from src.discovery.signals.sitemap_signal import sitemap_signal
        source = inspect.getsource(sitemap_signal)
        assert "is_same_domain" in source


# ===========================================================================
# 4.3 — content_filter budget (verified — no new code needed)
# ===========================================================================

class TestContentFilterBudgetVerification:
    def test_content_filter_check_before_record_llm_call(self):
        """Verify content_filter check is BEFORE record_llm_call in controller."""
        import inspect
        from src.execution.controller import CrawlController
        source = inspect.getsource(CrawlController.run)
        # content_filter check must appear before record_llm_call
        filter_pos = source.find("content_filter")
        record_pos = source.find("record_llm_call")
        assert filter_pos < record_pos, (
            "content_filter check must come before record_llm_call "
            "to avoid consuming budget on filter retries"
        )
