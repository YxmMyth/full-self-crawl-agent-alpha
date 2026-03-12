"""
Wave 2 tests: URL normalization, pattern matching, content_filter retry,
and DataVerifier integration.
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.utils.url import normalize_url
from src.management.run_intelligence import RunIntelligence
from src.management.scheduler import SharedFrontier
from src.strategy.spec import CrawlSpec
from src.verification.verifier import DataVerifier


# ===========================================================================
# 2.1 — URL normalization utility
# ===========================================================================

class TestNormalizeUrl:
    def test_strips_fragment(self):
        assert normalize_url("https://x.com/page#section") == "https://x.com/page"

    def test_strips_trailing_slash(self):
        assert normalize_url("https://x.com/page/") == "https://x.com/page"

    def test_strips_both(self):
        assert normalize_url("https://x.com/page/#top") == "https://x.com/page"

    def test_empty_string(self):
        assert normalize_url("") == ""

    def test_no_change_needed(self):
        assert normalize_url("https://x.com/page") == "https://x.com/page"

    def test_preserves_query_params(self):
        assert normalize_url("https://x.com/page?id=1") == "https://x.com/page?id=1"

    def test_fragment_after_query(self):
        assert normalize_url("https://x.com/page?a=1#hash") == "https://x.com/page?a=1"

    def test_multiple_fragments(self):
        # Only the first # matters in URL spec
        assert normalize_url("https://x.com/page#a#b") == "https://x.com/page"

    def test_frontier_uses_normalize_url(self):
        """SharedFrontier dedup should work identically after the refactor."""
        f = SharedFrontier()
        f.add("https://x.com/a#section")
        assert not f.add("https://x.com/a")  # dedup catches it
        f.add("https://x.com/b/")
        assert not f.add("https://x.com/b")  # dedup catches it


# ===========================================================================
# 2.2 — URL pattern matching (lstrip + re.search double bug)
# ===========================================================================

class TestUrlPatternMatching:
    @pytest.fixture
    def ri(self, tmp_path):
        ri = RunIntelligence(str(tmp_path))
        ri.initialize()
        return ri

    def test_pattern_generation_has_leading_slash(self, ri):
        """_url_to_pattern should produce /segment/... format, not */segment/..."""
        pattern = ri._url_to_pattern("https://codepen.io/user1/pen/abc123")
        assert pattern.startswith("/"), f"Pattern should start with /, got: {pattern}"
        assert not pattern.startswith("*/"), f"Pattern should NOT start with */, got: {pattern}"

    def test_pattern_matches_same_structure(self, ri):
        """URL with same structure should match its own pattern."""
        url = "https://codepen.io/user1/pen/abc123"
        pattern = ri._url_to_pattern(url)
        assert ri._url_matches_pattern(url, pattern)

    def test_pattern_matches_different_slug(self, ri):
        """Different slugs in wildcard positions should match."""
        url1 = "https://codepen.io/user1/pen/abc123"
        url2 = "https://codepen.io/user2/pen/xyz789"
        pattern = ri._url_to_pattern(url1)
        assert ri._url_matches_pattern(url2, pattern)

    def test_pattern_rejects_extra_segments(self, ri):
        """URL with more segments than pattern should NOT match."""
        pattern = ri._url_to_pattern("https://codepen.io/user1/pen/abc123")
        # 4 segments vs pattern's 3 segments
        assert not ri._url_matches_pattern(
            "https://codepen.io/admin/extra/pen/xyz", pattern
        )

    def test_pattern_rejects_fewer_segments(self, ri):
        """URL with fewer segments than pattern should NOT match."""
        pattern = ri._url_to_pattern("https://codepen.io/user1/pen/abc123")
        assert not ri._url_matches_pattern(
            "https://codepen.io/user1", pattern
        )

    def test_pattern_rejects_different_fixed_segment(self, ri):
        """URL with different fixed (non-wildcardable) segment should NOT match."""
        # Use a segment that won't be wildcarded: >16 chars or has special chars
        pattern = ri._url_to_pattern("https://example.com/products/12345")
        # "products" (8 chars, alpha) → *, "12345" (5 digits) → stays as "12345"
        # pattern = /*/12345
        assert ri._url_matches_pattern("https://example.com/items/12345", pattern)  # * matches "items"
        assert not ri._url_matches_pattern("https://example.com/items/99999", pattern)  # 99999 ≠ 12345

    def test_pattern_rejects_substring_match(self, ri):
        """Anchored matching prevents cross-structure false positives."""
        # Use numeric segment that stays fixed in the pattern
        pattern = ri._url_to_pattern("https://example.com/products/12345")
        # pattern = /*/12345 — should NOT match paths with different structure
        assert not ri._url_matches_pattern(
            "https://example.com/products/category/12345", pattern
        )  # 3 segments vs 2

    def test_pattern_roundtrip(self, ri):
        """generate pattern → match original URL should always work."""
        urls = [
            "https://example.com/products/12345",
            "https://news.site/2024/03/article-slug",
            "https://codepen.io/user/pen/abcdef",
        ]
        for url in urls:
            pattern = ri._url_to_pattern(url)
            assert ri._url_matches_pattern(url, pattern), \
                f"Roundtrip failed: {url} → {pattern}"

    def test_get_script_for_url_uses_fixed_matching(self, ri):
        """End-to-end: proven script lookup uses anchored matching."""
        # Record a success for a specific URL pattern
        ri.record_success(
            "https://codepen.io/user1/pen/abc123",
            "() => document.title",
            1,
        )
        # Same structure should find the script
        script = ri.get_script_for_url("https://codepen.io/user2/pen/xyz789")
        assert script == "() => document.title"
        # Different structure should NOT find it
        script = ri.get_script_for_url("https://codepen.io/admin/settings")
        assert script is None


# ===========================================================================
# 2.3 — content_filter retry
# ===========================================================================

class TestContentFilterRetry:
    @pytest.mark.asyncio
    async def test_content_filter_retries_then_succeeds(self):
        """content_filter twice, then normal response → session continues."""
        from src.execution.controller import CrawlController
        from src.execution.actions import LLMDecision, ToolCall, ToolResult
        from src.execution.history import StepHistory
        from src.management.governor import Governor
        from src.management.context import ContextManager
        from src.verification.verifier import RiskMonitor

        # Mock LLM: first 2 calls → content_filter, 3rd → normal (wants to stop)
        mock_llm = AsyncMock()
        call_count = 0

        async def mock_chat(**kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            if call_count <= 2:
                resp.content = ""
                resp.tool_calls = []
                resp.finish_reason = "content_filter"
                resp.usage = {"total_tokens": 0}
                resp.total_tokens = 0
            else:
                resp.content = "Done"
                resp.tool_calls = []
                resp.finish_reason = "stop"
                resp.usage = {"total_tokens": 100}
                resp.total_tokens = 100
            return resp

        mock_llm.chat_with_tools = mock_chat

        gov = Governor(max_steps=10, max_llm_calls=10, max_time_seconds=30)
        ctx = ContextManager()
        tools = MagicMock()
        tools.schemas.return_value = []

        ctrl = CrawlController(mock_llm, tools, gov, ctx)
        spec = CrawlSpec(url="https://x.com", requirement="test")
        result = await ctrl.run({"url": "https://x.com", "spec": spec, "role": "extraction"})

        assert result["stop_reason"] == "LLM completed"
        assert call_count == 3  # 2 filter + 1 success
        # Only 1 LLM call should be recorded (the non-filter one)
        assert gov._llm_calls == 1

    @pytest.mark.asyncio
    async def test_content_filter_exhausted(self):
        """4 consecutive content_filters → session stops clearly."""
        from src.execution.controller import CrawlController
        from src.management.governor import Governor
        from src.management.context import ContextManager

        mock_llm = AsyncMock()

        async def mock_chat(**kwargs):
            resp = MagicMock()
            resp.content = ""
            resp.tool_calls = []
            resp.finish_reason = "content_filter"
            resp.usage = {"total_tokens": 0}
            resp.total_tokens = 0
            return resp

        mock_llm.chat_with_tools = mock_chat

        gov = Governor(max_steps=30, max_llm_calls=30, max_time_seconds=30)
        ctx = ContextManager()
        tools = MagicMock()
        tools.schemas.return_value = []

        ctrl = CrawlController(mock_llm, tools, gov, ctx)
        spec = CrawlSpec(url="https://x.com", requirement="test")
        result = await ctrl.run({"url": "https://x.com", "spec": spec, "role": "extraction"})

        assert "content_filter" in result["stop_reason"]
        # Should NOT have consumed all 30 LLM calls — only 4 retries
        assert gov._llm_calls == 0  # none counted (all were filter retries)

    @pytest.mark.asyncio
    async def test_content_filter_counter_resets(self):
        """Counter resets after successful response."""
        from src.execution.controller import CrawlController
        from src.management.governor import Governor
        from src.management.context import ContextManager

        mock_llm = AsyncMock()
        call_count = 0

        async def mock_chat(**kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            if call_count <= 2:
                # First 2: content_filter
                resp.content = ""
                resp.tool_calls = []
                resp.finish_reason = "content_filter"
                resp.total_tokens = 0
            elif call_count == 3:
                # 3rd: successful tool call (think)
                tc = MagicMock()
                tc.id = "tc1"
                tc.name = "think"
                tc.arguments = {"thought": "ok"}
                resp.content = ""
                resp.tool_calls = [tc]
                resp.finish_reason = "stop"
                resp.total_tokens = 100
            else:
                # 4th: stop
                resp.content = "Done"
                resp.tool_calls = []
                resp.finish_reason = "stop"
                resp.total_tokens = 50
            resp.usage = {"total_tokens": getattr(resp, 'total_tokens', 0)}
            return resp

        mock_llm.chat_with_tools = mock_chat

        gov = Governor(max_steps=10, max_llm_calls=10, max_time_seconds=60)
        ctx = ContextManager()
        tools = MagicMock()
        tools.schemas.return_value = []
        tools.execute = AsyncMock(return_value={"success": True, "result": {"thought": "ok"}})

        ctrl = CrawlController(mock_llm, tools, gov, ctx)
        spec = CrawlSpec(url="https://x.com", requirement="test")
        result = await ctrl.run({"url": "https://x.com", "spec": spec, "role": "extraction"})

        assert result["stop_reason"] == "LLM completed"
        assert ctrl._content_filter_retries == 0  # reset after success


# ===========================================================================
# 2.4 — DataVerifier integration
# ===========================================================================

class TestDataVerifierIntegration:
    def test_verifier_detects_duplicates(self):
        """DataVerifier finds exact duplicates."""
        data = [
            {"title": "A", "desc": "d1"},
            {"title": "B", "desc": "d2"},
            {"title": "A", "desc": "d1"},  # exact duplicate
        ]
        verifier = DataVerifier()
        report = verifier.verify(data)
        assert report["duplicate_count"] == 1
        assert any("duplicate" in i.lower() for i in report["issues"])

    def test_verifier_checks_field_completeness(self):
        """DataVerifier reports field fill rates."""
        data = [
            {"title": "A", "desc": "d1"},
            {"title": "B", "desc": ""},
            {"title": "C"},  # missing desc
        ]
        verifier = DataVerifier()
        report = verifier.verify(data)
        assert report["field_completeness"]["title"] == 1.0
        assert report["field_completeness"]["desc"] < 1.0

    def test_verifier_checks_against_spec(self):
        """DataVerifier reports missing spec fields."""
        spec = CrawlSpec(
            url="x", requirement="y",
            target_fields=[{"name": "title"}, {"name": "price"}]
        )
        data = [{"title": "Product A"}]  # missing price
        verifier = DataVerifier()
        report = verifier.verify(data, spec)
        assert any("price" in i for i in report["issues"])

    def test_verifier_returns_quality_score(self):
        """Quality score is between 0 and 1."""
        data = [{"title": "A"}, {"title": "B"}]
        verifier = DataVerifier()
        report = verifier.verify(data)
        assert 0 <= report["quality_score"] <= 1

    def test_verifier_on_empty_data(self):
        data = []
        verifier = DataVerifier()
        report = verifier.verify(data)
        assert report["quality_score"] == 0.0
        assert report["record_count"] == 0

    def test_quality_field_in_pipeline_output(self):
        """Verify the DataVerifier is called in _run_full_site by checking the source."""
        import inspect
        from src.management import orchestrator
        source = inspect.getsource(orchestrator.Orchestrator._run_full_site)
        assert "DataVerifier" in source
        assert "quality_report" in source
        assert '"quality"' in source or "'quality'" in source
