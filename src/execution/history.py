"""
Execution: StepHistory — records and compiles step results.

Tracks all tool calls and results during a controller loop execution.
Provides compressed summaries for context window management.
"""

import json
import logging
from typing import Any

from .actions import Step, ToolCall, ToolResult

logger = logging.getLogger("execution.history")


class StepHistory:
    """Record and manage step history for the controller loop.

    Used by:
    - ContextManager: to build compressed history for LLM context
    - Governor: to detect loops and count steps
    - Controller: to compile final results
    """

    def __init__(self):
        self._steps: list[Step] = []

    def record(self, step_number: int, tool_call: ToolCall,
               result: ToolResult) -> Step:
        """Record a completed step."""
        step = Step(
            step_number=step_number,
            tool_call=tool_call,
            result=result,
        )
        self._steps.append(step)
        return step

    @property
    def steps(self) -> list[Step]:
        return self._steps

    @property
    def count(self) -> int:
        return len(self._steps)

    @property
    def last(self) -> Step | None:
        return self._steps[-1] if self._steps else None

    def recent(self, n: int = 3) -> list[Step]:
        """Get the most recent N steps."""
        return self._steps[-n:]

    def older(self, n: int = 3) -> list[Step]:
        """Get steps older than the most recent N."""
        if len(self._steps) <= n:
            return []
        return self._steps[:-n]

    def success_rate(self) -> float:
        if not self._steps:
            return 1.0
        return sum(1 for s in self._steps if s.succeeded) / len(self._steps)

    def tool_usage(self) -> dict[str, int]:
        """Count how many times each tool was used."""
        counts: dict[str, int] = {}
        for step in self._steps:
            name = step.tool_name
            counts[name] = counts.get(name, 0) + 1
        return counts

    def last_n_same_tool(self, n: int = 3) -> bool:
        """Check if the last N steps used the same tool with same args."""
        if len(self._steps) < n:
            return False
        recent = self._steps[-n:]
        first = recent[0]
        return all(
            s.tool_call.name == first.tool_call.name
            and s.tool_call.arguments == first.tool_call.arguments
            for s in recent
        )

    def compile_results(self) -> dict[str, Any]:
        """Compile step history into a summary dict."""
        return {
            "total_steps": self.count,
            "success_rate": round(self.success_rate(), 3),
            "tool_usage": self.tool_usage(),
            "errors": [
                {
                    "step": s.step_number,
                    "tool": s.tool_name,
                    "error": s.result.content[:200],
                }
                for s in self._steps
                if not s.succeeded
            ],
        }

    def summarize_step(self, step: Step, max_result_len: int = 200) -> str:
        """Create a concise text summary of a step."""
        args_str = json.dumps(step.tool_call.arguments, ensure_ascii=False)
        if len(args_str) > 100:
            args_str = args_str[:100] + "..."
        result_str = step.result.content
        if len(result_str) > max_result_len:
            result_str = result_str[:max_result_len] + "..."
        status = "✓" if step.succeeded else "✗"
        return f"[{status}] {step.tool_name}({args_str}) → {result_str}"

    def summarize_old_steps(self, n_recent: int = 3) -> str:
        """Summarize older steps as compressed text.

        Used by ContextManager to save tokens.
        """
        old = self.older(n_recent)
        if not old:
            return ""
        lines = []
        for step in old:
            lines.append(self.summarize_step(step, max_result_len=80))
        return f"Previous {len(old)} steps:\n" + "\n".join(lines)

    def clear(self) -> None:
        self._steps.clear()
