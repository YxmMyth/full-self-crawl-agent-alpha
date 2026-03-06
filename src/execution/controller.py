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
        self._collected_files: list[dict] = []
        self._discovered_urls: list[str] = []

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
        new_links = self._discovered_urls  # shared reference for side-channel collection

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
                progress = self._compute_progress(task)
                messages = self.context.build(
                    task=task,
                    history=self.history,
                    tools_schema=self.tools.schemas(),
                    nudges=nudges,
                    progress=progress,
                )

                # 3. LLM decision
                decision = await self._get_decision(messages)
                if decision is None:
                    stop_reason = "LLM error: failed to get response"
                    break

                self.governor.record_llm_call(decision.total_tokens)

                # Log what LLM decided
                if decision.tool_calls:
                    tool_names = [tc.name for tc in decision.tool_calls]
                    logger.info(f"Step {self._step_number+1}: LLM calls {tool_names}")
                else:
                    logger.info(f"Step {self._step_number}: LLM returned text only (finish_reason={decision.finish_reason})")

                # 4. If LLM wants to stop (no tool calls)
                if decision.wants_to_stop:
                    stop_reason = "LLM completed"
                    summary = decision.content or ""
                    logger.info(f"LLM says done after {self.history.count} steps")
                    break

                # 5. Execute tool calls
                for tc in decision.tool_calls:
                    self._step_number += 1

                    # Auto-save accumulated data if save_data called without data
                    if tc.name == "save_data" and "data" not in tc.arguments and self._collected_data:
                        # Bypass normal tool execution — save directly without putting data in context
                        tc.arguments["data"] = self._collected_data
                        result = await self._execute_tool(tc)
                        # Replace the result to avoid huge data in context
                        result.content = json.dumps({
                            "saved": len(self._collected_data),
                            "path": "artifacts/data/extracted_data." + tc.arguments.get("format", "json"),
                            "format": tc.arguments.get("format", "json"),
                            "note": "auto-saved all records collected via save_records()"
                        })
                        # Remove data from arguments before recording in history
                        tc.arguments.pop("data", None)
                        tc.arguments["_auto_saved"] = len(self._collected_data)
                        logger.info(f"save_data: auto-saved {len(self._collected_data)} collected records")

                        self.history.record(self._step_number, tc, result)
                        if self.governor.monitor:
                            self.governor.monitor.record_action(success=result.success, action_name=tc.name)
                        continue

                    # Auto-inject collected data for verify_quality if no data provided
                    if tc.name == "verify_quality" and "data" not in tc.arguments and self._collected_data:
                        sample = self._collected_data[:10]
                        tc.arguments["data"] = sample
                        logger.info(f"verify_quality: auto-injected {len(sample)} sample records")

                    result = await self._execute_tool(tc)

                    # Track current URL after navigate so skill library can inject on next step
                    if tc.name == "navigate" and result.success:
                        try:
                            nav_data = json.loads(result.content)
                            if isinstance(nav_data, dict) and nav_data.get("url"):
                                task["current_url"] = nav_data["url"]
                        except (json.JSONDecodeError, TypeError):
                            pass

                    self.history.record(self._step_number, tc, result)

                    # Track for loop detection
                    self.governor.record_action(tc.name, tc.arguments)

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

                    # Collect records/urls/files from execute_code side-channel
                    if tc.name in ("execute_code", "bash") and result.success:
                        self._collect_side_channel(result, new_links)

                    # Collect records from js_extract_save side-channel (_records field)
                    if tc.name == "js_extract_save" and result.success:
                        self._collect_side_channel(result, new_links)

                    # Track files from download_file or click_download tool
                    if tc.name in ("download_file", "click_download") and result.success:
                        try:
                            data = json.loads(result.content)
                            if isinstance(data, dict) and data.get("success"):
                                self._collected_files.append(data)
                        except (json.JSONDecodeError, TypeError):
                            pass

                    # Collect data from save_data calls
                    if tc.name == "save_data" and result.success:
                        self._extract_saved_data(tc.arguments)

        except Exception as e:
            stop_reason = f"Exception: {str(e)}"
            logger.error(f"Controller error: {traceback.format_exc()}")

        return {
            "success": stop_reason in ("LLM completed", ""),
            "data": self._collected_data,
            "files": self._collected_files,
            "steps": self.history.count,
            "stop_reason": stop_reason,
            "summary": summary,
            "new_links": new_links,
            "successful_tools": self._extract_successful_tools(),
            "failed_tools": self._extract_failed_tools(),
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

    def _compute_progress(self, task: dict) -> dict:
        """Compute progress stats for context injection."""
        time_elapsed = round(self.governor.elapsed_seconds, 1)
        time_remaining = round(max(0, self.governor.max_time_seconds - self.governor.elapsed_seconds), 1)
        return {
            "role": task.get("role", "extraction"),
            "records_collected": len(self._collected_data),
            "files_collected": len(self._collected_files),
            "urls_found": len(self._discovered_urls),
            "fields": self._summarize_fields(),
            "steps_taken": self.history.count,
            "steps_remaining": self.governor.max_steps - self.history.count,
            "time_elapsed": time_elapsed,
            "time_remaining": time_remaining,
        }

    def _summarize_fields(self) -> str | None:
        """Summarize field coverage across collected records."""
        if not self._collected_data:
            return None
        all_keys: set[str] = set()
        for rec in self._collected_data:
            if isinstance(rec, dict):
                all_keys.update(rec.keys())
        if not all_keys:
            return None
        total = len(self._collected_data)
        parts = []
        for key in sorted(all_keys):
            filled = sum(1 for r in self._collected_data if isinstance(r, dict) and r.get(key))
            pct = int(filled / total * 100) if total else 0
            parts.append(f"{key}({pct}%)")
        return ", ".join(parts)

    def _extract_successful_tools(self) -> list[dict]:
        """Extract successful tool calls for cross-page context.

        execute_code/bash calls get their code preserved up to 2000 chars —
        this is the most valuable cross-page knowledge (extraction patterns).
        Other tools are included only if their args are short (<= 500 chars).
        """
        successful = []
        for step in self.history.steps:
            if step.succeeded:
                if step.tool_name in ("execute_code", "bash"):
                    # Always preserve code — silently dropping it was a bug
                    code = step.tool_call.arguments.get("code", "")
                    if code:
                        successful.append({
                            "tool": step.tool_name,
                            "code": code[:2000],
                            "language": step.tool_call.arguments.get("language", "python"),
                        })
                else:
                    args_str = json.dumps(step.tool_call.arguments, default=str)
                    if len(args_str) <= 500:
                        successful.append({
                            "tool": step.tool_name,
                            "args": step.tool_call.arguments,
                        })
        return successful

    def _extract_failed_tools(self) -> list[dict]:
        """Extract failed tool calls for cross-page learning.

        Returns failed steps with error info — the agent on the next page
        can learn from failures and avoid repeating them.
        """
        failed = []
        for step in self.history.steps:
            if not step.succeeded:
                error_msg = step.result.content[:200] if step.result else "unknown"
                failed.append({
                    "tool": step.tool_name,
                    "error": error_msg,
                })
        return failed

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
        """Extract records from save_data arguments into collected data."""
        data = args.get("data", [])
        if isinstance(data, list) and data:
            self._collected_data.extend(data)
            logger.debug(f"Collected {len(data)} records from save_data")

    def _collect_side_channel(self, result: ToolResult, new_links: list) -> None:
        """Collect records, URLs, and files from execute_code side-channel, strip from LLM context."""
        try:
            data = json.loads(result.content)
            if not isinstance(data, dict):
                return
            records = data.pop("_records", [])
            urls = data.pop("_urls", [])
            files = data.pop("_files", [])

            if records:
                self._collected_data.extend(records)
                data["_saved"] = f"{len(records)} records saved via save_records()"
                logger.info(f"Side-channel: collected {len(records)} records from execute_code")
            if urls:
                new_links.extend(urls)
                data["_reported"] = f"{len(urls)} URLs reported via report_urls()"
                logger.info(f"Side-channel: collected {len(urls)} URLs from execute_code")
            if files:
                self._collected_files.extend(files)
                data["_files_saved"] = f"{len(files)} files saved via save_file()"
                logger.info(f"Side-channel: collected {len(files)} file artifacts from execute_code")

            if records or urls or files:
                result.content = json.dumps(data, default=str, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pass
