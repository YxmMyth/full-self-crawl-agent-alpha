"""
Execution: Action data types for the controller loop.

Defines ToolCall, ToolResult, Step, and LLMDecision — the core data
structures that flow through the LLM-as-Controller architecture.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ToolCall:
    """A tool invocation requested by the LLM."""
    id: str
    name: str
    arguments: dict[str, Any]

    def to_message(self) -> dict:
        """Convert to OpenAI assistant message tool_call format."""
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments),
            },
        }


@dataclass
class ToolResult:
    """Result of executing a tool."""
    tool_call_id: str
    content: str  # JSON-serialized result
    success: bool = True

    def to_message(self) -> dict:
        """Convert to OpenAI tool result message format."""
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "content": self.content,
        }


@dataclass
class Step:
    """A complete step record: tool call + result."""
    step_number: int
    tool_call: ToolCall
    result: ToolResult
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def tool_name(self) -> str:
        return self.tool_call.name

    @property
    def succeeded(self) -> bool:
        return self.result.success


@dataclass
class LLMDecision:
    """An LLM's response in one turn of the controller loop."""
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def wants_to_stop(self) -> bool:
        """LLM wants to stop (no tool calls and finish_reason is stop)."""
        return not self.tool_calls and self.finish_reason == "stop"

    @property
    def total_tokens(self) -> int:
        return self.usage.get("total_tokens", 0)
