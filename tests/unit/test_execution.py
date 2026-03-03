"""
Unit tests for execution layer: actions, history, registry.
"""

import json
import pytest
from src.execution.actions import ToolCall, ToolResult, Step, LLMDecision
from src.execution.history import StepHistory
from src.tools.registry import ToolRegistry


class TestToolCall:
    def test_to_message(self):
        tc = ToolCall(id="tc1", name="navigate", arguments={"url": "https://x.com"})
        msg = tc.to_message()
        assert msg["id"] == "tc1"
        assert msg["function"]["name"] == "navigate"
        assert json.loads(msg["function"]["arguments"]) == {"url": "https://x.com"}


class TestToolResult:
    def test_to_message(self):
        tr = ToolResult(tool_call_id="tc1", content='{"ok": true}')
        msg = tr.to_message()
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "tc1"


class TestLLMDecision:
    def test_wants_to_stop(self):
        dec = LLMDecision(content="I'm done")
        assert dec.wants_to_stop

    def test_wants_to_continue(self):
        tc = ToolCall(id="tc1", name="click", arguments={"s": "btn"})
        dec = LLMDecision(content=None, tool_calls=[tc])
        assert not dec.wants_to_stop

    def test_total_tokens(self):
        dec = LLMDecision(content="x", usage={"total_tokens": 500})
        assert dec.total_tokens == 500


class TestStepHistory:
    def _make_step(self, h, num, tool="click", args=None, success=True):
        tc = ToolCall(id=f"tc{num}", name=tool, arguments=args or {"s": f"x{num}"})
        content = '{"ok": true}' if success else '{"error": "fail"}'
        tr = ToolResult(tool_call_id=f"tc{num}", content=content, success=success)
        return h.record(num, tc, tr)

    def test_record_and_count(self):
        h = StepHistory()
        self._make_step(h, 1)
        self._make_step(h, 2)
        assert h.count == 2
        assert h.last.step_number == 2

    def test_success_rate(self):
        h = StepHistory()
        self._make_step(h, 1, success=True)
        self._make_step(h, 2, success=False)
        assert h.success_rate() == 0.5

    def test_tool_usage(self):
        h = StepHistory()
        self._make_step(h, 1, tool="navigate")
        self._make_step(h, 2, tool="click")
        self._make_step(h, 3, tool="click")
        assert h.tool_usage() == {"navigate": 1, "click": 2}

    def test_loop_detection(self):
        h = StepHistory()
        for i in range(3):
            tc = ToolCall(id=f"tc{i}", name="click", arguments={"s": "same"})
            tr = ToolResult(tool_call_id=f"tc{i}", content="ok")
            h.record(i+1, tc, tr)
        assert h.last_n_same_tool(3)

    def test_no_loop_different_args(self):
        h = StepHistory()
        for i in range(3):
            tc = ToolCall(id=f"tc{i}", name="click", arguments={"s": f"diff_{i}"})
            tr = ToolResult(tool_call_id=f"tc{i}", content="ok")
            h.record(i+1, tc, tr)
        assert not h.last_n_same_tool(3)

    def test_recent_and_older(self):
        h = StepHistory()
        for i in range(5):
            self._make_step(h, i+1)
        assert len(h.recent(2)) == 2
        assert len(h.older(2)) == 3

    def test_compile_results(self):
        h = StepHistory()
        self._make_step(h, 1, success=True)
        self._make_step(h, 2, success=False)
        r = h.compile_results()
        assert r["total_steps"] == 2
        assert len(r["errors"]) == 1

    def test_summarize_old_steps(self):
        h = StepHistory()
        for i in range(5):
            self._make_step(h, i+1, tool="navigate")
        summary = h.summarize_old_steps(2)
        assert "Previous 3 steps" in summary
        assert "navigate" in summary


class TestRegistryArgumentAdaptation:
    """Test that registry auto-adapts flattened LLM args into nested objects."""

    def _make_registry(self):
        reg = ToolRegistry()
        # Register extract_css-like tool with nested object param
        async def fake_extract(selectors: dict, container: str = None):
            return {"selectors": selectors, "container": container}
        reg.register("extract_css", fake_extract,
            "Extract data using CSS selectors",
            {"type": "object", "properties": {
                "selectors": {"type": "object", "description": "Field→selector mapping"},
                "container": {"type": "string", "description": "Container selector"},
            }, "required": ["selectors"]})
        return reg

    @pytest.mark.asyncio
    async def test_correct_args_pass_through(self):
        reg = self._make_registry()
        result = await reg.execute("extract_css", {
            "selectors": {"title": "h3 a", "price": ".price"},
            "container": ".product",
        })
        assert result["success"]
        assert result["result"]["selectors"] == {"title": "h3 a", "price": ".price"}

    @pytest.mark.asyncio
    async def test_flat_args_auto_adapted(self):
        """LLM sends flat args — registry should auto-wrap into selectors."""
        reg = self._make_registry()
        result = await reg.execute("extract_css", {
            "title": "h3 a",
            "price": ".price_color",
            "container": ".product",
        })
        assert result["success"]
        assert result["result"]["selectors"] == {"title": "h3 a", "price": ".price_color"}
        assert result["result"]["container"] == ".product"

    @pytest.mark.asyncio
    async def test_flat_args_without_container(self):
        """LLM sends only selector fields, no container."""
        reg = self._make_registry()
        result = await reg.execute("extract_css", {
            "title": "h3 a",
            "price": ".price_color",
        })
        assert result["success"]
        assert result["result"]["selectors"] == {"title": "h3 a", "price": ".price_color"}


class TestRegistryRawFallback:
    """Test _raw JSON parse failure recovery."""

    def _make_code_registry(self):
        reg = ToolRegistry()
        async def fake_execute(code: str, language: str = "python"):
            return {"code": code, "language": language}
        reg.register("execute_code", fake_execute,
            "Execute code",
            {"type": "object", "properties": {
                "code": {"type": "string", "description": "Source code"},
                "language": {"type": "string", "description": "Language"},
            }, "required": ["code"]})
        return reg

    @pytest.mark.asyncio
    async def test_raw_fallback_mapped_to_code(self):
        """JSON parse failure produces _raw — should map to first required string param."""
        reg = self._make_code_registry()
        result = await reg.execute("execute_code", {"_raw": "print('hello world')"})
        assert result["success"]
        assert result["result"]["code"] == "print('hello world')"

    @pytest.mark.asyncio
    async def test_raw_valid_json_recovered(self):
        """_raw contains valid JSON — should be parsed and used directly."""
        reg = self._make_code_registry()
        result = await reg.execute("execute_code", {
            "_raw": '{"code": "x = 1", "language": "python"}',
        })
        assert result["success"]
        assert result["result"]["code"] == "x = 1"
        assert result["result"]["language"] == "python"
