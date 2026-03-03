"""
Execution: CrawlController — THE LLM-as-Controller loop.

This is the core innovation of the alpha architecture.
The LLM decides what to do, tools execute, results flow back.

Loop:
1. Governor: should we stop?
2. Context: build messages (system + task + history + nudges)
3. LLM: chat with tools → get response
4. If tool_calls: execute each, record in history → goto 1
5. If no tool_calls (LLM says done): compile results → return
"""

import json
import logging
import traceback
from typing import Any

from .actions import LLMDecision, ToolCall, ToolResult
from .history import StepHistory

logger = logging.getLogger("execution.controller")


class CrawlController:
    """LLM-as-Controller execution loop.

    Dependencies:
    - llm_client: LLM with function calling (chat_with_tools)
    - tools: ToolRegistry (all available tools)
    - governor: Governor (governance checks)
    - context_mgr: ContextManager (builds messages)
    """

    def __init__(self, llm_client, tools, governor, context_mgr):
        self.llm = llm_client
        self.tools = tools
        self.governor = governor
        self.context = context_mgr
        self.history = StepHistory()
        self._step_number = 0
        self._collected_data: list[dict] = []

    async def run(self, task: dict) -> dict:
        """Execute a crawl task with the LLM-as-Controller loop.

        Args:
            task: {
                url: str,
                spec: CrawlSpec,
                role: "exploration" | "extraction",
                site_context?: str,
            }

        Returns:
            {
                success: bool,
                data: list[dict],
                steps: int,
                stop_reason: str,
                summary: str,
                new_links: list[str],
                metrics: dict,
            }
        """
        self.governor.start()
        stop_reason = ""
        summary = ""
        new_links = []

        logger.info(f"Controller starting: {task.get('url', 'unknown')}, role={task.get('role', 'extraction')}")

        try:
            while True:
                # 1. Governor check
                reason = self.governor.should_stop(self.history)
                if reason:
                    stop_reason = f"Governor: {reason}"
                    logger.info(f"Governor force stop: {reason}")
                    break

                # 2. Build context
                nudges = self.governor.get_nudges(
                    self.history, self._collected_data, task.get("spec")
                )
                messages = self.context.build(
                    task=task,
                    history=self.history,
                    tools_schema=self.tools.schemas(),
                    nudges=nudges,
                )

                # 3. LLM decision
                decision = await self._get_decision(messages)
                if decision is None:
                    stop_reason = "LLM error: failed to get response"
                    break

                self.governor.record_llm_call(decision.total_tokens)

                # 4. If LLM wants to stop (no tool calls)
                if decision.wants_to_stop:
                    stop_reason = "LLM completed"
                    summary = decision.content or ""
                    logger.info(f"LLM says done after {self.history.count} steps")
                    break

                # 5. Execute tool calls
                for tc in decision.tool_calls:
                    self._step_number += 1

                    # Auto-inject accumulated data if save_data called without data
                    if tc.name == "save_data" and "data" not in tc.arguments and self._collected_data:
                        tc.arguments["data"] = self._collected_data
                        logger.info(f"Auto-injected {len(self._collected_data)} records into save_data")

                    result = await self._execute_tool(tc)

                    self.history.record(self._step_number, tc, result)

                    # Track risk
                    if self.governor.monitor:
                        self.governor.monitor.record_action(
                            success=result.success,
                            action_name=tc.name,
                            error=result.content if not result.success else None,
                        )

                    # Collect data from extract_css results
                    if tc.name == "extract_css" and result.success:
                        self._extract_css_data(result.content)

                    # Collect data from save_data calls
                    if tc.name == "save_data" and result.success:
                        self._extract_saved_data(tc.arguments)

                    # Collect links from analyze_links
                    if tc.name == "analyze_links" and result.success:
                        try:
                            link_data = json.loads(result.content)
                            if isinstance(link_data, dict):
                                for link in link_data.get("links", []):
                                    url = link.get("url", "") if isinstance(link, dict) else str(link)
                                    if url:
                                        new_links.append(url)
                        except (json.JSONDecodeError, TypeError):
                            pass

        except Exception as e:
            stop_reason = f"Exception: {str(e)}"
            logger.error(f"Controller error: {traceback.format_exc()}")

        return {
            "success": stop_reason in ("LLM completed", ""),
            "data": self._collected_data,
            "steps": self.history.count,
            "stop_reason": stop_reason,
            "summary": summary,
            "new_links": new_links,
            "metrics": {
                **self.history.compile_results(),
                **self.governor.get_stats(),
            },
        }

    async def _get_decision(self, messages: list[dict]) -> LLMDecision | None:
        """Call LLM and parse response into LLMDecision."""
        try:
            response = await self.llm.chat_with_tools(
                messages=messages,
                tools=self.tools.schemas(),
            )

            # Convert ChatResponse to LLMDecision
            tool_calls = []
            if response.tool_calls:
                for tc in response.tool_calls:
                    tool_calls.append(ToolCall(
                        id=tc.id,
                        name=tc.name,
                        arguments=tc.arguments if isinstance(tc.arguments, dict)
                                  else json.loads(tc.arguments) if isinstance(tc.arguments, str)
                                  else {},
                    ))

            return LLMDecision(
                content=response.content,
                tool_calls=tool_calls,
                finish_reason=response.finish_reason or "stop",
                usage={
                    "total_tokens": getattr(response, "total_tokens",
                                           response.usage.get("total_tokens", 0)
                                           if hasattr(response, "usage") and isinstance(response.usage, dict)
                                           else 0),
                },
            )
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return None

    async def _execute_tool(self, tool_call: ToolCall) -> ToolResult:
        """Execute a tool call and return the result."""
        try:
            result = await self.tools.execute(tool_call.name, tool_call.arguments)
            # Registry returns dict {success, result, error}
            success = result.get("success", True)
            if success:
                content = json.dumps(result.get("result"), default=str, ensure_ascii=False)
            else:
                content = json.dumps({"error": result.get("error", "Unknown error")})
            return ToolResult(
                tool_call_id=tool_call.id,
                content=content,
                success=success,
            )
        except Exception as e:
            logger.error(f"Tool execution error ({tool_call.name}): {e}")
            return ToolResult(
                tool_call_id=tool_call.id,
                content=json.dumps({"error": str(e)}),
                success=False,
            )

    def _is_error_result(self, result: str) -> bool:
        """Check if a tool result indicates an error."""
        try:
            data = json.loads(result)
            if isinstance(data, dict) and "error" in data:
                return True
        except (json.JSONDecodeError, TypeError):
            pass
        return False

    def _extract_css_data(self, result_content: str) -> None:
        """Extract records from extract_css result into collected data."""
        try:
            data = json.loads(result_content)
            if isinstance(data, dict):
                records = data.get("records", [])
                if records:
                    self._collected_data.extend(records)
                    logger.debug(f"Collected {len(records)} records from extract_css")
        except (json.JSONDecodeError, TypeError):
            pass

    def _extract_saved_data(self, args: dict) -> None:
        """Extract data from save_data tool arguments."""
        data = args.get("data", [])
        if isinstance(data, list):
            self._collected_data.extend(data)
        elif isinstance(data, dict):
            self._collected_data.append(data)
