"""
Tool Registry — Central tool registration and schema generation.

The registry maps tool names to callables and generates OpenAI-compatible
function calling schemas for LLM consumption.
"""

import json
import logging
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

logger = logging.getLogger("tools.registry")


@dataclass
class ToolDef:
    """Definition of a registered tool."""
    name: str
    fn: Callable[..., Awaitable[Any]]
    description: str
    parameters: dict[str, Any]
    required: list[str] = field(default_factory=list)


class ToolRegistry:
    """
    Central registry for all agent tools.

    Responsibilities:
    1. Register tool name → async callable mapping
    2. Generate OpenAI function calling schemas
    3. Execute tools by name with argument dispatch
    """

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(
        self,
        name: str,
        fn: Callable[..., Awaitable[Any]],
        description: str,
        parameters: dict[str, Any] | None = None,
        required: list[str] | None = None,
    ) -> None:
        """Register a tool.

        Args:
            name: Tool name (used in function calling).
            fn: Async callable implementing the tool.
            description: Human-readable description for LLM.
            parameters: JSON Schema properties dict, e.g.
                {"url": {"type": "string", "description": "Target URL"}}
            required: List of required parameter names.
                If None, all parameters are considered required.
        """
        if name in self._tools:
            logger.warning(f"Tool '{name}' already registered, overwriting")

        params = parameters or {}
        if required is None:
            required = list(params.keys())

        self._tools[name] = ToolDef(
            name=name,
            fn=fn,
            description=description,
            parameters=params,
            required=required,
        )
        logger.debug(f"Registered tool: {name}")

    def unregister(self, name: str) -> None:
        """Remove a tool from the registry."""
        self._tools.pop(name, None)

    def get(self, name: str) -> ToolDef | None:
        """Get a tool definition by name."""
        return self._tools.get(name)

    @property
    def tool_names(self) -> list[str]:
        """List all registered tool names."""
        return list(self._tools.keys())

    def schemas(self) -> list[dict]:
        """Generate OpenAI function calling tool schemas.

        Returns:
            List of tool objects in OpenAI format:
            [{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}]
        """
        result = []
        for tool in self._tools.values():
            schema = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": {
                        "type": "object",
                        "properties": tool.parameters,
                        "required": tool.required,
                    },
                },
            }
            result.append(schema)
        return result

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool by name.

        Args:
            name: Tool name.
            arguments: Arguments dict from LLM function call.

        Returns:
            {"success": bool, "result": Any, "error": str | None}
        """
        tool = self._tools.get(name)
        if not tool:
            return {
                "success": False,
                "result": None,
                "error": f"Unknown tool: {name}",
            }

        try:
            result = await tool.fn(**arguments)
            return {"success": True, "result": result, "error": None}
        except TypeError as e:
            logger.error(f"Tool '{name}' argument error: {e}")
            return {
                "success": False,
                "result": None,
                "error": f"Invalid arguments for {name}: {e}",
            }
        except Exception as e:
            logger.error(f"Tool '{name}' execution error: {e}\n{traceback.format_exc()}")
            return {
                "success": False,
                "result": None,
                "error": f"{type(e).__name__}: {e}",
            }

    def describe(self) -> str:
        """Generate a human-readable tool list for system prompts.

        Returns a compact text block listing each tool and its description,
        suitable for embedding in the LLM system prompt (~50 tokens overhead).
        """
        lines = []
        for tool in self._tools.values():
            params = ", ".join(
                f"{k}: {v.get('type', 'any')}"
                for k, v in tool.parameters.items()
            )
            lines.append(f"- {tool.name}({params}) — {tool.description}")
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
