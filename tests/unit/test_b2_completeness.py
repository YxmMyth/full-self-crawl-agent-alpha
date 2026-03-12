"""
B2: Exploration completeness + extraction priority tests.
"""

import json
import pytest
import tempfile
from unittest.mock import MagicMock

from src.management.scheduler import SharedFrontier, URLStatus
from src.management.context import ContextManager
from src.management.run_intelligence import RunIntelligence
from src.strategy.gate import StructuralCompletionGate, GateDecision
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


class _FakeStep:
    """Minimal step object for ContextManager tests."""
    def __init__(self, tool_name="think", content="ok", call_id="tc_1"):
        self.tool_call = _FakeToolCall(tool_name, call_id)
        self.result = _FakeResult(content, call_id)


class _FakeToolCall:
    def __init__(self, name, call_id):
        self.name = name
        self.call_id = call_id

    def to_message(self):
        return {
            "id": self.call_id,
            "type": "function",
            "function": {"name": self.name, "arguments": "{}"},
        }


class _FakeResult:
    def __init__(self, content, call_id):
        self.content = content
        self.success = True
        self.tool_call_id = call_id


class _FakeHistory:
    """Minimal history compatible with ContextManager._compress_history."""
    def __init__(self, steps):
        self._steps = steps
        self.count = len(steps)

    def summarize_old_steps(self, keep_recent):
        if len(self._steps) > keep_recent:
            return "Summary of older steps."
        return ""

    def recent(self, n):
        return self._steps[-n:]


# ===========================================================================
# B2.1 — StructuralCompletionGate tests
# ===========================================================================

class TestStructuralGateOverride:

    def test_gate_not_met_no_sections(self):
        """Gate should not be met when no sections discovered."""
        gate = StructuralCompletionGate()
        decision = gate.check(sections_found=0, sections_sampled=0, has_proven_script=False)
        assert not decision.met

    def test_gate_not_met_sections_unsampled(self):
        """Gate should not be met when sections exist but are unsampled."""
        gate = StructuralCompletionGate()
        decision = gate.check(sections_found=3, sections_sampled=1, has_proven_script=True)
        assert not decision.met
        assert "sections" in decision.reason.lower()

    def test_gate_not_met_no_proven_script(self):
        """Gate should not be met when all sections sampled but no proven script."""
        gate = StructuralCompletionGate()
        decision = gate.check(sections_found=2, sections_sampled=2, has_proven_script=False)
        assert not decision.met
        assert "proven" in decision.reason.lower()

    def test_gate_met_all_sampled_and_script(self):
        """Gate met when all sections sampled AND proven script exists."""
        gate = StructuralCompletionGate()
        decision = gate.check(sections_found=3, sections_sampled=3, has_proven_script=True)
        assert decision.met

    def test_gate_met_single_section(self):
        """Gate met with just one section, sampled, with proven script."""
        gate = StructuralCompletionGate()
        decision = gate.check(sections_found=1, sections_sampled=1, has_proven_script=True)
        assert decision.met


class TestStructuralGateIntegration:
    """Test StructuralCompletionGate with real frontier + run_intelligence data."""

    def test_gate_from_frontier_data(self):
        """Build gate inputs from frontier.section_coverage() + run_intelligence."""
        frontier = _make_frontier()
        ri = _make_run_intelligence()

        # Register 2 sections, sample only 1
        frontier.register_section("https://example.com/blog")
        frontier.register_section("https://example.com/news")
        frontier.add("https://example.com/blog/post-1")
        frontier.associate_url_with_section("https://example.com/blog/post-1",
                                            "https://example.com/blog")
        frontier.mark_in_flight("https://example.com/blog/post-1")
        frontier.mark_extracted("https://example.com/blog/post-1", 2,
                                new_data=[{"title": "A", "description": "B"}])

        # Write a proven script
        ri.write("proven_scripts", {"/blog/*": {"script": "() => ({})"}})

        sec_cov = frontier.section_coverage()
        sections_found = len(sec_cov)
        sections_sampled = sum(1 for c in sec_cov.values() if c > 0)
        proven = ri.read("proven_scripts")

        gate = StructuralCompletionGate()
        decision = gate.check(sections_found, sections_sampled, bool(proven))

        # news section has 0 records → not all sampled
        assert not decision.met

    def test_gate_overrides_explorer_task_complete(self):
        """Simulates the orchestrator logic: Explorer says done but gate disagrees."""
        frontier = _make_frontier()
        ri = _make_run_intelligence()

        frontier.register_section("https://example.com/a")
        frontier.register_section("https://example.com/b")
        # Only section /a has records
        frontier.add("https://example.com/a/1")
        frontier.associate_url_with_section("https://example.com/a/1",
                                            "https://example.com/a")
        frontier.mark_in_flight("https://example.com/a/1")
        frontier.mark_extracted("https://example.com/a/1", 1,
                                new_data=[{"title": "T", "description": "D"}])

        explorer_says_done = True  # Explorer declared TASK COMPLETE

        sec_cov = frontier.section_coverage()
        sections_found = len(sec_cov)
        sections_sampled = sum(1 for c in sec_cov.values() if c > 0)
        has_proven = bool(ri.read("proven_scripts"))

        structural_gate = StructuralCompletionGate().check(
            sections_found, sections_sampled, has_proven
        )

        # Orchestrator logic: gate NOT met + explorer_says_done → override
        if structural_gate.met:
            exploration_done = True
        elif explorer_says_done and not structural_gate.met and sections_found > 0:
            exploration_done = False
        else:
            exploration_done = explorer_says_done

        assert not exploration_done  # gate overrode Explorer


# ===========================================================================
# B2.2 — Section-aware extraction priority
# ===========================================================================

class TestSectionAwarePriority:

    def test_next_prefers_zero_record_section(self):
        """URLs from sections with 0 records should be chosen first."""
        f = _make_frontier()
        f.register_section("https://example.com/covered")
        f.register_section("https://example.com/empty")

        # Add URL in covered section (section has records)
        f.add("https://example.com/covered/p1", priority=2.0)
        f.associate_url_with_section("https://example.com/covered/p1",
                                     "https://example.com/covered")
        # Manually set section as having records
        f._section_records["https://example.com/covered"] = 5

        # Add URL in empty section (section has 0 records)
        f.add("https://example.com/empty/p1", priority=1.0)
        f.associate_url_with_section("https://example.com/empty/p1",
                                     "https://example.com/empty")

        picked = f.next()
        assert picked is not None
        assert picked.url == "https://example.com/empty/p1"

    def test_next_falls_back_to_priority_same_boost(self):
        """When both URLs have same section boost, use priority."""
        f = _make_frontier()
        f.register_section("https://example.com/a")
        f.register_section("https://example.com/b")

        f.add("https://example.com/a/p1", priority=1.0)
        f.associate_url_with_section("https://example.com/a/p1",
                                     "https://example.com/a")

        f.add("https://example.com/b/p1", priority=3.0)
        f.associate_url_with_section("https://example.com/b/p1",
                                     "https://example.com/b")

        # Both sections have 0 records → same section_boost → priority wins
        picked = f.next()
        assert picked is not None
        assert picked.url == "https://example.com/b/p1"

    def test_next_no_section_association_not_penalized(self):
        """URLs without section association get neutral boost (0.5), not 0."""
        f = _make_frontier()
        f.register_section("https://example.com/covered")
        f._section_records["https://example.com/covered"] = 10

        # URL in covered section (boost=0.0)
        f.add("https://example.com/covered/p2", priority=5.0)
        f.associate_url_with_section("https://example.com/covered/p2",
                                     "https://example.com/covered")

        # URL with no section association (boost=0.5)
        f.add("https://example.com/random/page", priority=1.0)

        picked = f.next()
        assert picked is not None
        # 0.5 > 0.0 → unassociated URL preferred over covered-section URL
        assert picked.url == "https://example.com/random/page"


# ===========================================================================
# B2.3 — ContextManager token budget
# ===========================================================================

class TestContextBudget:

    def _make_big_history(self, n_steps=10):
        """Create a history with n large steps."""
        steps = []
        for i in range(n_steps):
            step = _FakeStep(
                tool_name="execute_code",
                content="x" * 3000,  # large result
                call_id=f"tc_{i}",
            )
            steps.append(step)
        return _FakeHistory(steps)

    def test_build_trims_when_over_budget(self):
        """build() output should be trimmed when history is large."""
        ctx = ContextManager(max_history_steps=5, max_context_chars=5000)
        history = self._make_big_history(10)
        task = {"url": "https://example.com", "spec": _make_spec(), "role": "extraction"}

        messages = ctx.build(task, history, tools_schema=[])
        total_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)

        # Should be under budget (with some tolerance for system prompt)
        assert total_chars <= 8000  # generous bound; without trimming would be ~40k+

    def test_build_preserves_system_and_task(self):
        """Trimming should always keep system prompt and task context."""
        ctx = ContextManager(max_history_steps=5, max_context_chars=3000)
        history = self._make_big_history(10)
        task = {"url": "https://example.com", "spec": _make_spec(), "role": "extraction"}

        messages = ctx.build(task, history, tools_schema=[])

        # First message is system, second is task context
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_build_no_trim_when_under_budget(self):
        """Small history should not be trimmed."""
        ctx = ContextManager(max_history_steps=3, max_context_chars=50000)
        # 2 small steps
        steps = [
            _FakeStep("think", "short", "tc_0"),
            _FakeStep("think", "also short", "tc_1"),
        ]
        history = _FakeHistory(steps)
        task = {"url": "https://example.com", "spec": _make_spec(), "role": "extraction"}

        messages = ctx.build(task, history, tools_schema=[])

        # Should have system + task + 2*(assistant+tool) = 6 messages
        # (no summary because 2 steps <= max_history_steps=3)
        assert len(messages) == 6
