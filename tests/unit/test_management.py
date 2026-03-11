"""
Unit tests for management layer: StateManager, CrawlFrontier, Governor, ContextManager.
"""

import pytest
import shutil
import time
from src.management.state import StateManager, CrawlState
from src.management.scheduler import CrawlFrontier, CrawlTask
from src.management.governor import Governor
from src.management.context import ContextManager
from src.execution.actions import ToolCall, ToolResult
from src.execution.history import StepHistory
from src.verification.verifier import RiskMonitor
from src.strategy.gate import CompletionGate
from src.strategy.spec import CrawlSpec


class TestStateManager:
    @pytest.fixture
    def state_mgr(self, tmp_path):
        return StateManager(checkpoint_dir=str(tmp_path / "states"))

    def test_create_and_get(self, state_mgr):
        state = state_mgr.create("t1", "https://example.com")
        assert state.task_id == "t1"
        assert state.status == "pending"
        assert state_mgr.get("t1") is state

    def test_update(self, state_mgr):
        state_mgr.create("t1")
        state_mgr.update("t1", status="running", current_url="https://x.com")
        state = state_mgr.get("t1")
        assert state.status == "running"
        assert state.current_url == "https://x.com"

    def test_add_data(self, state_mgr):
        state_mgr.create("t1")
        state_mgr.add_data("t1", [{"a": 1}, {"b": 2}])
        assert len(state_mgr.get("t1").data_collected) == 2

    def test_checkpoint_roundtrip(self, state_mgr):
        state_mgr.create("t1", "https://example.com")
        state_mgr.update("t1", status="running")
        state_mgr.add_data("t1", [{"x": 1}])
        state_mgr.save_checkpoint("t1")

        # Load in fresh instance
        sm2 = StateManager(checkpoint_dir=state_mgr._checkpoint_dir)
        loaded = sm2.load_checkpoint("t1")
        assert loaded.status == "running"
        assert len(loaded.data_collected) == 1


class TestCrawlFrontier:
    def test_basic_add_and_next(self):
        f = CrawlFrontier(max_depth=2)
        f.add("https://example.com/a")
        f.add("https://example.com/b", priority=2.0)
        t = f.next()
        assert t.url == "https://example.com/b"  # higher priority first

    def test_duplicate_filtering(self):
        f = CrawlFrontier()
        assert f.add("https://example.com/a")
        assert not f.add("https://example.com/a")

    def test_depth_limit(self):
        f = CrawlFrontier(max_depth=1)
        assert f.add("https://example.com/a", depth=1)
        assert not f.add("https://example.com/b", depth=2)

    def test_domain_filtering(self):
        f = CrawlFrontier()
        f.set_base_domain("https://example.com")
        assert f.add("https://example.com/a")
        assert not f.add("https://other.com/b")

    def test_add_batch(self):
        f = CrawlFrontier()
        count = f.add_batch([
            {"url": "https://a.com/1", "category": "detail"},
            {"url": "https://a.com/2", "category": "list"},
            {"url": "https://a.com/1"},  # duplicate
        ])
        assert count == 2

    def test_exhaustion(self):
        f = CrawlFrontier()
        f.add("https://example.com/a")
        f.next()
        assert f.next() is None


class TestGovernor:
    def _make_history(self, n_steps, same_tool=False, all_fail=False):
        h = StepHistory()
        for i in range(n_steps):
            name = "click" if same_tool else f"tool_{i}"
            args = {"s": "x"} if same_tool else {"s": f"x_{i}"}
            tc = ToolCall(id=f"tc{i}", name=name, arguments=args)
            content = '{"error": "fail"}' if all_fail else '{"ok": true}'
            tr = ToolResult(tool_call_id=f"tc{i}", content=content, success=not all_fail)
            h.record(i+1, tc, tr)
        return h

    def test_step_limit(self):
        g = Governor(max_steps=5)
        g.start()
        h = self._make_history(5)
        assert g.should_stop(h) is not None

    def test_llm_call_limit(self):
        g = Governor(max_llm_calls=3)
        g.start()
        h = self._make_history(1)
        for _ in range(3):
            g.record_llm_call(100)
        assert "LLM call limit" in g.should_stop(h)

    def test_loop_detection(self):
        g = Governor(max_steps=100)
        g.start()
        h = self._make_history(3, same_tool=True, all_fail=True)
        reason = g.should_stop(h)
        assert reason is not None and "loop" in reason.lower()

    def test_no_stop_normally(self):
        g = Governor(max_steps=30)
        g.start()
        h = self._make_history(2)
        assert g.should_stop(h) is None

    def test_nudge_budget_warning(self):
        g = Governor(max_llm_calls=10)
        g.start()
        for _ in range(8):
            g.record_llm_call(100)
        h = self._make_history(1)
        nudge = g.get_nudges(h)
        assert nudge and "Budget" in nudge

    def test_nudge_completion_met(self):
        gate = CompletionGate()
        g = Governor(gate=gate)
        g.start()
        spec = CrawlSpec(url="x", requirement="y", min_items=2)
        data = [{"a": 1}, {"a": 2}]
        h = self._make_history(1)
        nudge = g.get_nudges(h, data=data, spec=spec)
        assert nudge and "met" in nudge.lower()


class TestContextManager:
    def test_build_basic(self):
        cm = ContextManager()
        spec = CrawlSpec(url="https://x.com", requirement="Get products")
        task = {"url": "https://x.com", "spec": spec, "role": "extraction"}
        h = StepHistory()
        msgs = cm.build(task, h, [])
        assert msgs[0]["role"] == "system"
        assert "extraction" in msgs[0]["content"].lower() or "data" in msgs[0]["content"].lower()
        assert any("https://x.com" in m.get("content", "") for m in msgs)

    def test_build_with_history(self):
        cm = ContextManager(max_history_steps=2)
        task = {"url": "https://x.com", "role": "extraction",
                "spec": CrawlSpec(url="x", requirement="y")}
        h = StepHistory()
        for i in range(5):
            tc = ToolCall(id=f"tc{i}", name="click", arguments={"s": f"sel_{i}"})
            tr = ToolResult(tool_call_id=f"tc{i}", content='{"ok": true}')
            h.record(i+1, tc, tr)
        msgs = cm.build(task, h, [])
        # Should have summary of older steps + recent steps
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        assert len(tool_msgs) == 2  # only 2 recent

    def test_build_with_nudges(self):
        cm = ContextManager()
        task = {"url": "x", "role": "extraction",
                "spec": CrawlSpec(url="x", requirement="y")}
        msgs = cm.build(task, StepHistory(), [], nudges="⚠️ Budget low")
        assert any("Budget low" in m.get("content", "") for m in msgs)

    def test_exploration_role(self):
        cm = ContextManager()
        task = {"url": "x", "role": "exploration",
                "spec": CrawlSpec(url="x", requirement="y")}
        msgs = cm.build(task, StepHistory(), [])
        assert "exploration" in msgs[0]["content"].lower() or "exploring" in msgs[0]["content"].lower() or "reconnaissance" in msgs[0]["content"].lower()


class TestContextManagerSamplerRole:
    def test_sampler_role_has_distinct_prompt(self):
        cm = ContextManager()
        task = {"url": "x", "role": "sampler",
                "spec": CrawlSpec(url="x", requirement="y")}
        msgs = cm.build(task, StepHistory(), [])
        system_content = msgs[0]["content"]
        assert "Sampler" in system_content or "sampler" in system_content

    def test_sampler_prompt_no_navigate_instruction(self):
        cm = ContextManager()
        task = {"url": "x", "role": "sampler",
                "spec": CrawlSpec(url="x", requirement="y")}
        msgs = cm.build(task, StepHistory(), [])
        system_content = msgs[0]["content"]
        # Should tell agent NOT to call navigate() first
        assert "Do not call navigate" in system_content or "already navigated" in system_content

    def test_sampler_prompt_contains_sampling_complete(self):
        cm = ContextManager()
        task = {"url": "x", "role": "sampler",
                "spec": CrawlSpec(url="x", requirement="y")}
        msgs = cm.build(task, StepHistory(), [])
        system_content = msgs[0]["content"]
        assert "SAMPLING COMPLETE" in system_content
