"""
Management: ContextManager — builds LLM message arrays for each step.

This is critical engineering: controlling what the LLM sees each turn.
Token budget management ensures we stay within context limits while
giving the LLM maximum useful information.
"""

import json
import logging
from typing import Any

logger = logging.getLogger("management.context")


class ContextManager:
    """Build optimized message arrays for each controller step.

    Message structure per step:
    1. system prompt: role + capabilities + rules (~800 tokens)
    2. task context: spec + current progress (~300 tokens)
    3. compressed history: recent N steps full + older summarized (~2000 tokens)
    4. governance nudges: budget warnings / behavior corrections (~100 tokens)
    5. current observation: page state / last results (~2000 tokens)

    Total budget: ~5000 tokens/step, leaving room for LLM response + tool schemas.
    """

    def __init__(self, max_history_steps: int = 3, max_tokens: int = 6000):
        self.max_history_steps = max_history_steps
        self.max_tokens = max_tokens

    def build(self, task: dict, history, tools_schema: list[dict],
              nudges: str | None = None) -> list[dict]:
        """Build complete messages array for one LLM turn.

        Args:
            task: {url, spec (CrawlSpec), role, site_context?, ...}
            history: StepHistory instance
            tools_schema: OpenAI function calling schemas
            nudges: Optional governance nudges to inject

        Returns:
            List of message dicts for the LLM API.
        """
        messages = []

        # 1. System prompt
        messages.append(self._system_prompt(task))

        # 2. Task context
        messages.append(self._task_context(task))

        # 3. Compressed history as assistant/tool message pairs
        history_messages = self._compress_history(history)
        messages.extend(history_messages)

        # 4. Governance nudges (injected as system message)
        if nudges:
            messages.append({"role": "system", "content": nudges})

        return messages

    def _system_prompt(self, task: dict) -> dict:
        """Build system message with role definition and rules."""
        role = task.get("role", "extraction")

        if role == "exploration":
            role_text = """You are a web site exploration expert. Your goal is to understand the site structure and find pages containing the target data.

Navigate the site, analyze page structure and links, and build a map of relevant pages.
When you have found enough target pages, output your site map as a final message."""
        else:
            role_text = """You are a web data extraction expert. Your goal is to extract structured data from web pages according to the specification.

Use the available tools to navigate, analyze, and extract data. Save extracted data using save_data.
Verify quality before finishing. If extraction fails with one approach, try alternatives."""

        site_context = task.get("site_context", "")
        if site_context:
            role_text += f"\n\nSite context from exploration phase:\n{site_context}"

        rules = """
Rules:
- Call tools to interact with the page. Do not hallucinate data.
- If a tool fails, try a different approach (different selector, execute_code, etc.)
- execute_code is your escape hatch — if specialized tools don't work, write code.
- Save data incrementally. Don't wait until the end.
- When you believe the task is complete, stop calling tools and say "TASK COMPLETE" with a summary."""

        return {"role": "system", "content": role_text + rules}

    def _task_context(self, task: dict) -> dict:
        """Build task context message with spec and progress."""
        spec = task.get("spec")
        url = task.get("url", "")

        parts = [f"Target URL: {url}"]

        if spec:
            if hasattr(spec, "requirement"):
                parts.append(f"Requirement: {spec.requirement}")
                if spec.understanding:
                    parts.append(f"Understanding: {spec.understanding}")
                if spec.success_criteria:
                    parts.append(f"Success criteria: {spec.success_criteria}")
                if spec.exploration_hints:
                    parts.append(f"Hints: {spec.exploration_hints}")
                if spec.target_fields:
                    fields = ", ".join(f["name"] for f in spec.target_fields if isinstance(f, dict))
                    parts.append(f"Target fields: {fields}")
                parts.append(f"Min items: {spec.min_items}")
            elif isinstance(spec, dict):
                parts.append(f"Requirement: {spec.get('requirement', '')}")

        # Progress
        progress = task.get("progress")
        if progress:
            parts.append(f"\nProgress: {progress}")

        return {"role": "user", "content": "\n".join(parts)}

    def _compress_history(self, history) -> list[dict]:
        """Convert step history to message pairs.

        Recent steps: full tool_call + tool result messages.
        Older steps: compressed into a single summary message.
        """
        if not history or history.count == 0:
            return []

        messages = []

        # Older steps → summary
        summary = history.summarize_old_steps(self.max_history_steps)
        if summary:
            messages.append({"role": "system", "content": summary})

        # Recent steps → full messages
        for step in history.recent(self.max_history_steps):
            # Assistant message with tool_call
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [step.tool_call.to_message()],
            })
            # Tool result message
            result_content = step.result.content
            if len(result_content) > 15000:
                result_content = result_content[:15000] + "\n... (truncated)"
            messages.append({
                "role": "tool",
                "tool_call_id": step.result.tool_call_id,
                "content": result_content,
            })

        return messages

    def _truncate_html(self, html: str, max_chars: int = 5000) -> str:
        """Truncate HTML preserving structure hints."""
        if len(html) <= max_chars:
            return html
        head = html[:max_chars]
        # Count some structural elements
        tag_counts = {
            "links": html.count("<a "),
            "images": html.count("<img "),
            "tables": html.count("<table"),
            "forms": html.count("<form"),
            "lists": html.count("<ul") + html.count("<ol"),
        }
        structure = ", ".join(f"{k}:{v}" for k, v in tag_counts.items() if v > 0)
        return f"{head}\n... (truncated, total {len(html)} chars. Structure: {structure})"
