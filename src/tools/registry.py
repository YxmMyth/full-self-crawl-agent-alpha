"""
Tool Registry — Central tool registration and schema generation.

The registry maps tool names to callables and generates OpenAI-compatible
function calling schemas for LLM consumption.
"""

import asyncio
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
        # If caller passed a full JSON Schema object, extract properties/required
        if params.get("type") == "object" and "properties" in params:
            if required is None:
                required = params.get("required", list(params["properties"].keys()))
            params = params["properties"]
        elif required is None:
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

    def _adapt_arguments(self, tool: ToolDef, arguments: dict[str, Any]) -> dict[str, Any]:
        """Adapt LLM arguments to match tool schema.

        Handles two common LLM mistakes:
        1. Flattening nested objects: extract_css gets {"title": "h3 a"} instead of {"selectors": {"title": "h3 a"}}
        2. JSON parse failures: LLM sends malformed JSON → _raw fallback needs remapping
        """
        known_params = set(tool.parameters.keys())

        # Handle _raw fallback from JSON parse failure in LLM client
        if "_raw" in arguments and "_raw" not in known_params:
            raw_val = arguments["_raw"]
            # Try to re-parse as JSON (sometimes just whitespace/encoding issue)
            if isinstance(raw_val, str):
                try:
                    import json
                    parsed = json.loads(raw_val)
                    if isinstance(parsed, dict):
                        logger.info(f"Recovered _raw JSON for '{tool.name}'")
                        return parsed
                except (json.JSONDecodeError, TypeError):
                    pass
                # Map _raw to the first required string param
                for pname in tool.required:
                    pschema = tool.parameters.get(pname, {})
                    if pschema.get("type") == "string" and pname not in arguments:
                        logger.info(f"Mapped _raw to '{pname}' for '{tool.name}'")
                        adapted = {k: v for k, v in arguments.items() if k != "_raw"}
                        adapted[pname] = raw_val
                        return adapted

        unknown_args = {k: v for k, v in arguments.items() if k not in known_params}

        if not unknown_args:
            return arguments

        # Find if there's a required object-type parameter that's missing
        object_param = None
        for pname, pschema in tool.parameters.items():
            if (pschema.get("type") == "object"
                    and pname in tool.required
                    and pname not in arguments):
                object_param = pname
                break

        if object_param and unknown_args:
            adapted = {k: v for k, v in arguments.items() if k in known_params}
            adapted[object_param] = unknown_args
            logger.info(
                f"Auto-adapted flat args into '{object_param}': "
                f"{list(unknown_args.keys())}"
            )
            return adapted

        return arguments

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
            # Adapt arguments (fix common LLM flattening of nested objects)
            arguments = self._adapt_arguments(tool, arguments)

            # Filter arguments to only known parameters (LLMs sometimes hallucinate extra params)
            known_params = set(tool.parameters.keys())
            if known_params:
                filtered_args = {k: v for k, v in arguments.items() if k in known_params}
            else:
                filtered_args = arguments

            if asyncio.iscoroutinefunction(tool.fn):
                result = await tool.fn(**filtered_args)
            else:
                result = tool.fn(**filtered_args)
                if asyncio.iscoroutine(result):
                    result = await result
            return {"success": True, "result": result, "error": None}
        except TypeError as e:
            # Build a helpful error message for LLM self-correction
            missing_hint = ""
            if "missing" in str(e) and "argument" in str(e):
                required = tool.required
                provided = list(arguments.keys())
                missing = [r for r in required if r not in arguments]
                if missing:
                    missing_hint = f" Required params: {required}. Missing: {missing}. You provided: {provided}."
                    # Add usage hint for common tools
                    examples = {
                        "extract_css": ' Example: extract_css(selectors={"field1": "css_selector1", "field2": "css_selector2"}, container=".item")',
                        "save_data": ' Example: save_data(data=[{"key": "value", ...}], format="json")',
                    }
                    if name in examples:
                        missing_hint += examples[name]
            logger.error(f"Tool '{name}' argument error: {e}{missing_hint}")
            return {
                "success": False,
                "result": None,
                "error": f"Invalid arguments for {name}: {e}.{missing_hint}",
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
