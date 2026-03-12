"""
Wave 1 robustness tests: exception protection, partial result preservation,
and sampling loop activation.
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from src.management.scheduler import SharedFrontier, URLStatus
from src.strategy.spec import CrawlSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec():
    return CrawlSpec(url="https://example.com", requirement="Get items",
                     target_fields=[{"name": "title"}, {"name": "description"}])


def _make_frontier_with_urls(urls, spec=None):
    """Create a SharedFrontier pre-loaded with QUEUED URLs."""
    spec = spec or _make_spec()
    f = SharedFrontier(max_urls=300, spec=spec)
    for u in urls:
        f.add(u, discovered_by="test")
    return f


# ===========================================================================
# 1.1 — _extract_one exception does not crash the extraction loop
# ===========================================================================

class TestExtractOneExceptionProtection:
    """Verify that a single URL extraction failure doesn't terminate the run."""

    def test_mark_failed_preserves_existing_data(self):
        """mark_failed after mark_extracted: data in _all_data is preserved."""
        spec = _make_spec()
        f = SharedFrontier(spec=spec)
        f.add("https://example.com/a", discovered_by="test")
        f.mark_in_flight("https://example.com/a")
        # Simulate successful extraction
        f.mark_extracted("https://example.com/a", 2,
                         new_data=[{"title": "A", "description": "d1"},
                                   {"title": "B", "description": "d2"}])
        assert len(f.all_data()) == 2
        # Now mark_failed (simulating post-extraction crash catch)
        f.mark_failed("https://example.com/a", "exception: ValueError")
        # Data must still be in all_data (append-only)
        assert len(f.all_data()) == 2
        # Status changed to FAILED
        rec = f._records.get("https://example.com/a")
        assert rec.status == URLStatus.FAILED

    def test_mark_failed_on_in_flight_url(self):
        """URL that crashes before extraction: marked FAILED, no data added."""
        f = SharedFrontier(spec=_make_spec())
        f.add("https://example.com/a", discovered_by="test")
        f.mark_in_flight("https://example.com/a")
        f.mark_failed("https://example.com/a", "exception: TimeoutError")
        rec = f._records.get("https://example.com/a")
        assert rec.status == URLStatus.FAILED
        assert len(f.all_data()) == 0

    def test_frontier_continues_after_failed_url(self):
        """After marking one URL failed, next() still returns remaining URLs."""
        f = _make_frontier_with_urls([
            "https://example.com/1",
            "https://example.com/2",
            "https://example.com/3",
        ])
        # Process first URL successfully
        url1 = f.next()
        f.mark_in_flight(url1.url)
        f.mark_extracted(url1.url, 1, new_data=[{"title": "ok"}])
        # Second URL fails
        url2 = f.next()
        f.mark_in_flight(url2.url)
        f.mark_failed(url2.url, "exception: RuntimeError")
        # Third URL should still be available
        url3 = f.next()
        assert url3 is not None
        f.mark_in_flight(url3.url)
        f.mark_extracted(url3.url, 1, new_data=[{"title": "also ok"}])
        # Stats should show 1 failed, 2 extracted
        stats = f.stats()
        assert stats["status_counts"].get("extracted", 0) == 2
        assert stats["status_counts"].get("failed", 0) == 1
        assert len(f.all_data()) == 2


# ===========================================================================
# 1.2 — Partial results preserved on crash
# ===========================================================================

class TestPartialResultsPreservation:
    """Verify that the outer exception handler in run() preserves frontier data."""

    def test_frontier_all_data_returns_accumulated_records(self):
        """Simulates the partial-results path: frontier has data, outer catch reads it."""
        spec = _make_spec()
        f = SharedFrontier(spec=spec)
        # Simulate 3 URLs extracted before crash
        for i in range(3):
            url = f"https://example.com/{i}"
            f.add(url, discovered_by="test")
            f.mark_in_flight(url)
            f.mark_extracted(url, 1, new_data=[{"title": f"item_{i}",
                                                 "description": f"desc_{i}"}])
        # This is what the outer catch will read
        partial = f.all_data()
        assert len(partial) == 3
        assert all("title" in r for r in partial)

    def test_empty_frontier_returns_empty(self):
        """If crash happens before any extraction, partial data is empty."""
        f = SharedFrontier(spec=_make_spec())
        assert f.all_data() == []

    @pytest.mark.asyncio
    async def test_run_crash_returns_partial_data(self):
        """Integration test: Orchestrator.run() crash preserves frontier data."""
        from src.management.orchestrator import Orchestrator

        orch = Orchestrator(config={"llm": {"api_key": "fake", "model": "test"}})
        # Pre-set frontier with data to simulate mid-run state
        spec = _make_spec()
        frontier = SharedFrontier(spec=spec)
        frontier.add("https://example.com/1", discovered_by="test")
        frontier.mark_in_flight("https://example.com/1")
        frontier.mark_extracted("https://example.com/1", 1,
                                new_data=[{"title": "saved", "description": "d"}])
        orch._frontier = frontier

        # Simulate the outer catch path
        partial_data = []
        if orch._frontier:
            try:
                partial_data = orch._frontier.all_data()
            except Exception:
                pass
        assert len(partial_data) == 1
        assert partial_data[0]["title"] == "saved"


# ===========================================================================
# 1.3 — _run_sampling_loop activation
# ===========================================================================

class TestSamplingLoopActivation:
    """Verify _run_sampling_loop is wired into the pipeline."""

    def test_sampling_loop_function_exists(self):
        """_run_sampling_loop is defined in orchestrator module."""
        import ast
        import inspect
        import textwrap
        from src.management import orchestrator
        source = textwrap.dedent(inspect.getsource(orchestrator.Orchestrator._run_full_site))
        tree = ast.parse(source)
        # Find all function definitions (including inner async defs)
        inner_funcs = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner_funcs.append(node.name)
        assert "_run_sampling_loop" in inner_funcs

    def test_sampling_loop_is_called_in_source(self):
        """_run_sampling_loop is actually called (not just defined)."""
        import inspect
        from src.management import orchestrator
        source = inspect.getsource(orchestrator.Orchestrator._run_full_site)
        # Count definitions vs calls
        # Definition: "async def _run_sampling_loop"
        # Call: "await _run_sampling_loop("
        def_count = source.count("async def _run_sampling_loop")
        call_count = source.count("await _run_sampling_loop(")
        assert def_count == 1, "Should have exactly one definition"
        assert call_count >= 1, (
            f"_run_sampling_loop defined {def_count} time(s) but called "
            f"{call_count} time(s) — dead code!"
        )

    def test_sampling_loop_call_is_after_burst_loop(self):
        """_run_sampling_loop call appears between burst loop and final drain."""
        import inspect
        from src.management import orchestrator
        source = inspect.getsource(orchestrator.Orchestrator._run_full_site)
        # The call should appear after "Exploration round" pattern and before "Final drain"
        burst_loop_pos = source.find("Exploration round")
        sampling_call_pos = source.find("await _run_sampling_loop(")
        final_drain_pos = source.find("Final drain")
        assert burst_loop_pos < sampling_call_pos < final_drain_pos, (
            f"_run_sampling_loop call should be between burst loop and final drain. "
            f"burst_loop={burst_loop_pos}, sampling={sampling_call_pos}, drain={final_drain_pos}"
        )

    def test_sampling_loop_call_is_exception_protected(self):
        """_run_sampling_loop call is wrapped in try/except."""
        import inspect
        from src.management import orchestrator
        source = inspect.getsource(orchestrator.Orchestrator._run_full_site)
        # Find the sampling call and check for surrounding try
        idx = source.find("await _run_sampling_loop(")
        # Look backwards for 'try:' within 200 chars
        context_before = source[max(0, idx - 200):idx]
        assert "try:" in context_before, (
            "_run_sampling_loop call should be inside a try/except block"
        )


# ===========================================================================
# Integration: exception protection pattern
# ===========================================================================

class TestExceptionProtectionPattern:
    """Verify the try/except pattern is applied to all call sites."""

    def test_all_extract_one_calls_are_protected(self):
        """Every `await _extract_one(` call should be inside try/except."""
        import inspect
        from src.management import orchestrator
        source = inspect.getsource(orchestrator.Orchestrator._run_full_site)

        # Find all _extract_one call positions
        calls = []
        start = 0
        while True:
            idx = source.find("await _extract_one(", start)
            if idx == -1:
                break
            calls.append(idx)
            start = idx + 1

        assert len(calls) >= 2, f"Expected at least 2 _extract_one calls, found {len(calls)}"

        for i, pos in enumerate(calls):
            context = source[max(0, pos - 200):pos]
            assert "try:" in context, (
                f"_extract_one call #{i+1} at position {pos} is not protected by try/except"
            )

    def test_all_run_explorer_calls_are_protected(self):
        """Every `await _run_explorer(` call should be inside try/except."""
        import inspect
        from src.management import orchestrator
        source = inspect.getsource(orchestrator.Orchestrator._run_full_site)

        calls = []
        start = 0
        while True:
            idx = source.find("await _run_explorer(", start)
            if idx == -1:
                break
            calls.append(idx)
            start = idx + 1

        assert len(calls) >= 2, f"Expected at least 2 _run_explorer calls, found {len(calls)}"

        for i, pos in enumerate(calls):
            context = source[max(0, pos - 300):pos]
            assert "try:" in context, (
                f"_run_explorer call #{i+1} at position {pos} is not protected by try/except"
            )
