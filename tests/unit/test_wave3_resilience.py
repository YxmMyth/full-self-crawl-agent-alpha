"""
Wave 3 tests: atomic writes, SPA DOM timeout, DDG retry, context budget removal.
"""

import asyncio
import json
import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.management.run_intelligence import RunIntelligence
from src.discovery.types import SiteIntelligence


# ===========================================================================
# 3.3 — Atomic writes
# ===========================================================================

class TestAtomicWrites:
    @pytest.fixture
    def ri(self, tmp_path):
        ri = RunIntelligence(str(tmp_path))
        ri.initialize()
        return ri

    def test_write_creates_valid_json(self, ri):
        """Normal write produces valid, readable JSON."""
        ri.write("test_key", "test_value")
        data = ri.read("test_key")
        assert data == "test_value"

    def test_write_survives_simulated_crash(self, ri):
        """If json.dump fails mid-write, original file is preserved."""
        ri.write("important", "original_value")
        assert ri.read("important") == "original_value"

        # Simulate crash: json.dump raises after file is opened
        with patch("src.management.run_intelligence.json.dump",
                    side_effect=IOError("simulated disk full")):
            with pytest.raises(IOError, match="simulated disk full"):
                ri.write("important", "corrupted_value")

        # Original value must survive
        assert ri.read("important") == "original_value"

    def test_golden_records_survive_crash(self, ri):
        """Golden records file preserved on write failure."""
        ri.save_golden_records([{"title": "original"}])
        original = ri.get_golden_records()
        assert len(original) == 1

        with patch("src.management.run_intelligence.json.dump",
                    side_effect=IOError("disk full")):
            with pytest.raises(IOError):
                ri.save_golden_records([{"title": "corrupted"}])

        # Original golden records preserved
        preserved = ri.get_golden_records()
        assert len(preserved) == 1
        assert preserved[0]["title"] == "original"

    def test_no_temp_file_left_on_crash(self, ri):
        """Temp file is cleaned up on failure."""
        with patch("src.management.run_intelligence.json.dump",
                    side_effect=IOError("fail")):
            with pytest.raises(IOError):
                ri.write("x", "y")

        # No .tmp files left
        tmp_files = list(Path(ri.base_dir).glob("*.tmp"))
        assert tmp_files == [], f"Temp files left behind: {tmp_files}"

    def test_concurrent_writes_dont_corrupt(self, ri):
        """Multiple sequential writes all produce valid JSON."""
        for i in range(20):
            ri.write("counter", i)
        assert ri.read("counter") == 19

    def test_proven_scripts_merge_with_atomic_write(self, ri):
        """proven_scripts merge + atomic write = no data loss."""
        ri.write("proven_scripts", {"pattern_a": {"script": "a", "success_count": 1}})
        ri.write("proven_scripts", {"pattern_b": {"script": "b", "success_count": 1}})
        proven = ri.read("proven_scripts")
        assert "pattern_a" in proven
        assert "pattern_b" in proven


# ===========================================================================
# 3.4 — SPA DOM stability timeout
# ===========================================================================

class TestSpaDomTimeout:
    def test_browser_uses_wait_for_in_spa_loop(self):
        """SPA DOM stability check uses asyncio.wait_for for timeout."""
        import inspect
        from src.tools import browser
        source = inspect.getsource(browser.BrowserTool.navigate)
        assert "wait_for" in source, (
            "SPA DOM stability loop should use asyncio.wait_for for timeout safety"
        )

    def test_browser_catches_timeout_error(self):
        """SPA loop catches TimeoutError from wait_for."""
        import inspect
        from src.tools import browser
        source = inspect.getsource(browser.BrowserTool.navigate)
        assert "TimeoutError" in source, (
            "SPA DOM loop should catch asyncio.TimeoutError"
        )


# ===========================================================================
# 3.2 — DDG search retry + degradation
# ===========================================================================

class TestDdgRetry:
    @pytest.mark.asyncio
    async def test_search_retries_on_failure(self):
        """search_signal retries up to 3 times before returning empty."""
        from src.discovery.signals.search_signal import search_signal

        call_count = 0
        original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        # Mock DDGS to fail twice, succeed third
        mock_ddg_instance = MagicMock()
        attempts = []

        class MockDDGS:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def text(self, query, max_results=20):
                attempts.append(1)
                if len(attempts) < 3:
                    raise Exception("rate limited")
                return [{"href": "https://example.com/page", "title": "Test", "body": "snippet"}]

        with patch.dict("sys.modules", {"ddgs": MagicMock(DDGS=MockDDGS)}):
            # Need to reimport to pick up the mock
            import importlib
            from src.discovery.signals import search_signal as ss_mod
            importlib.reload(ss_mod)
            results = await ss_mod.search_signal("example.com", "test data")

        assert len(attempts) == 3, f"Expected 3 attempts, got {len(attempts)}"
        assert len(results) >= 1

    def test_site_intelligence_has_search_degraded_field(self):
        """SiteIntelligence has search_degraded field."""
        si = SiteIntelligence()
        assert hasattr(si, "search_degraded")
        assert si.search_degraded is False

    def test_search_degraded_injected_into_context(self):
        """Context manager injects warning when search_degraded=True."""
        from src.management.context import ContextManager
        from src.strategy.spec import CrawlSpec

        si = SiteIntelligence(search_degraded=True)
        cm = ContextManager()
        spec = CrawlSpec(url="https://x.com", requirement="test")
        task = {
            "url": "https://x.com",
            "spec": spec,
            "role": "exploration",
            "site_intel": si,
        }
        from src.execution.history import StepHistory
        msgs = cm.build(task, StepHistory(), [])
        all_content = " ".join(m.get("content", "") or "" for m in msgs)
        assert "rate-limited" in all_content.lower() or "search_site" in all_content

    def test_search_not_degraded_no_warning(self):
        """No warning when search succeeded."""
        from src.management.context import ContextManager
        from src.strategy.spec import CrawlSpec

        si = SiteIntelligence(search_degraded=False)
        cm = ContextManager()
        spec = CrawlSpec(url="https://x.com", requirement="test")
        task = {
            "url": "https://x.com",
            "spec": spec,
            "role": "exploration",
            "site_intel": si,
        }
        from src.execution.history import StepHistory
        msgs = cm.build(task, StepHistory(), [])
        all_content = " ".join(m.get("content", "") or "" for m in msgs)
        assert "rate-limited" not in all_content.lower()


# ===========================================================================
# 3.1 — max_tokens removed
# ===========================================================================

class TestMaxTokensRemoved:
    def test_context_manager_no_max_tokens_param(self):
        """ContextManager no longer accepts max_tokens parameter."""
        from src.management.context import ContextManager
        cm = ContextManager()
        assert not hasattr(cm, "max_tokens")

    def test_context_manager_still_accepts_max_history_steps(self):
        """max_history_steps still works."""
        from src.management.context import ContextManager
        cm = ContextManager(max_history_steps=5)
        assert cm.max_history_steps == 5
