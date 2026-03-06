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
              nudges: str | None = None, progress: dict | None = None) -> list[dict]:
        """Build complete messages array for one LLM turn.

        Args:
            task: {url, spec (CrawlSpec), role, site_context?, ...}
            history: StepHistory instance
            tools_schema: OpenAI function calling schemas
            nudges: Optional governance nudges to inject
            progress: Optional progress stats {records_collected, fields, steps_taken, steps_remaining}

        Returns:
            List of message dicts for the LLM API.
        """
        messages = []

        # 1. System prompt
        messages.append(self._system_prompt(task))

        # 2. Task context (includes progress)
        messages.append(self._task_context(task, progress))

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
            role_text = """You are a URL discovery agent.

Mission: find URLs of pages that contain the target data, then report them with report_urls() inside execute_code. That is your only output — do not extract data content, never fabricate URLs.

Before navigating, reason about what you already know:
- Skill library (see above): if a verified URL pattern exists for this site, use it directly
- Task hints: do they describe the URL structure or listing pages?

If you don't yet know the URL structure, discover it first — choose the approach that fits:
- bash: curl robots.txt to find a sitemap → fetch it to see URL patterns and scale instantly
- navigate + analyze_links(): see categorized links directly from the rendered page
- Both are valid; use whichever gives you the information faster

Reading navigate() results — act on these signals immediately:
1. hint contains "SPA loaded" or "network still active" → page IS loaded but rendering. Call analyze_links() RIGHT NOW. Do NOT navigate again — navigating resets the SPA render cycle.
2. element_count < 20 → page empty/blocked. May retry or try bash curl.
3. strategy = "networkidle" → fully rendered. Use analyze_links() or get_html().

If analyze_links() and evaluate_js() both return no target URLs:
- Try bash to hit a REST API or sitemap instead of the browser SPA
- Try paginating (add ?page=2) or a different listing URL
- If truly nothing found after 3 different approaches, report_urls([]) with a summary explaining why

Once you have candidate URLs, judge before reporting:
- Do they match the "detail" category pattern (3+ path segments, e.g. /user/name/slug)?
- Does the URL pattern match what the task is asking for?

Do NOT call save_records() during exploration.

When done, summarize: how many target URLs found and what kind of pages they are."""

            feedback = task.get("feedback")
            if feedback:
                role_text += (
                    f"\n\nPrevious exploration produced poor results:\n"
                    f"- These pages yielded no data: {feedback.get('failed_pages', [])}\n"
                    f"- Records collected so far: {feedback.get('total_records', 0)}\n"
                    f"- Try different sections, URL patterns, or listing pages this time."
                )
        else:
            role_text = """You are a data extraction agent.

Mission: extract high-quality data from the target URL and save it via save_records() in execute_code or js_extract_save.

If the task context shows a ⚡ verified skill for this URL, call it first — it was verified to work.

Assess: what fields exist, what's the structure, what's the quality? Extract a sample with variety.

Do NOT navigate away or call go_back — you are assigned this specific page to extract from.

When done, say "TASK COMPLETE" with a summary of fields and data volume."""

        site_context = task.get("site_context", "")
        if site_context:
            role_text += f"\n\nSite context from exploration phase:\n{site_context}"

        import os
        rules = """
Environment:
- You are running in a headless Chromium browser inside a Docker container.
- Some sites detect headless browsers and may block access or serve challenge pages.
- If browser navigation fails or pages appear empty, try bash(curl ...) for direct HTTP access or look for API endpoints.
- navigate() returns page metadata including load_time_ms and element_count — use this to detect blocked or empty pages.
"""
        # Inject credential awareness if available
        site_user = os.environ.get("SITE_USERNAME", "")
        if site_user:
            rules += f"""
Authentication:
- Login credentials are available. Username: "{site_user}"
- To get the password, use: evaluate_js with script "window.__SITE_PASSWORD__" (injected at browser start).
- If the site requires login for downloads or access, navigate to the login page and use fill() + click() to log in.
- After logging in successfully, call save_auth_state() to persist the session for future runs.
- For OAuth login (e.g. "Login with GitHub"), navigate through the OAuth flow using the credentials.
"""

        rules += """
Rules:
- Every URL, data point, and record you report must come from actual page content you observed.
  Never generate plausible-sounding URLs or data. If extraction fails, report the failure honestly.
- Call tools to interact with the page. Do not hallucinate data.
- If a tool fails, try a different approach (different selector, execute_code, etc.)
- Before complex tasks, use think() to plan your approach and reason about strategy.
- execute_code is your most powerful tool. It runs Python with page_html pre-loaded.
  Use save_records() to persist extracted data. Use report_urls() to report found URLs.
- Focus on understanding and representative quality, not exhaustive collection.
- extract_css is a shortcut for simple, well-structured pages. Switch to
  execute_code when structure is complex or nested.
- evaluate_js runs JavaScript in the browser for live DOM access (SPA, dynamic content).
- click_download clicks an element and captures the resulting file download (for Export buttons, etc.).
- navigate() can be slow (up to 30s). Check your time budget before navigating. Use wait_until='domcontentloaded' for faster loading when full resource loading isn't needed.
- When you believe the task is complete, stop calling tools and say "TASK COMPLETE" with a summary
  of what you learned about the site's data."""

        return {"role": "system", "content": role_text + rules}

    def _task_context(self, task: dict, progress: dict | None = None) -> dict:
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

        # Progress stats — let the agent see its own state
        if progress:
            min_items = getattr(spec, "min_items", 0) if spec else 0
            role = progress.get("role", "extraction")

            progress_lines = ["\nCurrent progress:"]

            if role == "exploration":
                urls_found = progress.get("urls_found", 0)
                progress_lines.append(f"- Target URLs found: {urls_found}")
            else:
                collected = progress.get("records_collected", 0)
                target_note = f" (target: ≥{min_items})" if min_items else ""
                status = " ✓" if min_items and collected >= min_items else ""
                progress_lines.append(f"- Records collected: {collected}{target_note}{status}")

                field_stats = progress.get("fields")
                if field_stats:
                    progress_lines.append(f"- Field coverage: {field_stats}")

            files_collected = progress.get("files_collected", 0)
            if files_collected:
                progress_lines.append(f"- Files collected: {files_collected}")

            steps_remaining = progress.get("steps_remaining")
            steps_taken = progress.get("steps_taken", 0)
            if steps_remaining is not None:
                progress_lines.append(f"- Steps: {steps_taken} taken, {steps_remaining} remaining")

            time_remaining = progress.get("time_remaining")
            time_elapsed = progress.get("time_elapsed")
            if time_remaining is not None:
                progress_lines.append(f"- Time: {time_elapsed}s elapsed, {time_remaining}s remaining")

            parts.extend(progress_lines)

        # Skill library: inject verified strategies for current/initial URL
        # Extraction mode: extraction-role skills (JS code)
        # Exploration mode: exploration-role skills (navigation guidance)
        role = task.get("role", "extraction")
        if role in ("extraction", "exploration"):
            try:
                from ..tools.skill_library import SkillLibrary
                _lib = SkillLibrary()
                _urls_to_check = [task.get("current_url", ""), task.get("url", "")]
                _skills: list = []
                _seen_ids: set = set()
                for _u in _urls_to_check:
                    for s in _lib.get_relevant_skills(_u, role=role):
                        if s.get("id") not in _seen_ids:
                            _skills.append(s)
                            _seen_ids.add(s.get("id"))
                if _skills:
                    parts.append(f"\n{_lib.format_for_prompt(_skills)}")
            except Exception:
                pass  # Skill library is optional; never block execution

        # Site reconnaissance: robots.txt excerpt and sitemap candidates (if available)
        site_recon = task.get("site_recon")
        if site_recon:
            recon_lines = []
            robots_txt = site_recon.get("robots_txt", "")
            if robots_txt:
                recon_lines.append(f"\nSite robots.txt (fetched before your session):\n{robots_txt[:500]}")
            sitemap_candidates = site_recon.get("sitemap_candidates", [])
            if sitemap_candidates:
                recon_lines.append(
                    f"\nSitemap pre-loaded {len(sitemap_candidates)} candidate URLs matching this task."
                    f" Sample: {sitemap_candidates[:5]}"
                )
                recon_lines.append(
                    "If these match the target pattern, report them with report_urls() directly"
                    " — no need to navigate to every one."
                )
            if recon_lines:
                parts.extend(recon_lines)

        # Prior experience from previous pages
        prior = task.get("prior_experience")
        if prior:
            parts.append(f"\nPrior experience: {prior}")

        # Legacy progress field
        legacy_progress = task.get("progress")
        if legacy_progress:
            parts.append(f"\nProgress: {legacy_progress}")

        return {"role": "user", "content": "\n".join(parts)}

    def _compress_history(self, history) -> list[dict]:
        """Convert step history to message pairs.

        Applies Anthropic-style tool result clearing:
        - Recent steps: full tool_call + cleared/capped tool results
        - Older steps: compressed into a single summary message
        - Large results (HTML, data dumps) that have been consumed by
          subsequent steps are replaced with concise summaries.
        """
        if not history or history.count == 0:
            return []

        messages = []

        # Older steps → summary
        summary = history.summarize_old_steps(self.max_history_steps)
        if summary:
            messages.append({"role": "system", "content": summary})

        # Recent steps → full messages with result clearing
        recent = history.recent(self.max_history_steps)
        for i, step in enumerate(recent):
            is_last = (i == len(recent) - 1)

            # Assistant message with tool_call
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [step.tool_call.to_message()],
            })

            # Tool result — apply clearing based on type and recency
            result_content = self._clear_tool_result(step, is_last)
            messages.append({
                "role": "tool",
                "tool_call_id": step.result.tool_call_id,
                "content": result_content,
            })

        return messages

    def _clear_tool_result(self, step, is_last: bool) -> str:
        """Clear/cap tool results to manage context budget.

        Inspired by Anthropic's tool result clearing: once a result has
        been processed, the raw output is no longer needed.

        Args:
            step: The step containing tool_call and result
            is_last: Whether this is the most recent step (preserve more)
        """
        content = step.result.content
        tool = step.tool_call.name
        success = step.result.success

        if not success:
            # Error messages are always valuable — keep but cap
            return content[:3000] if len(content) > 3000 else content

        # Tools whose raw output loses value after processing
        heavy_tools = {"get_html", "get_text", "analyze_page", "analyze_links", "screenshot"}

        if tool in heavy_tools and not is_last:
            # Already consumed by subsequent steps — summarize
            return self._summarize_result(tool, content)

        if tool in ("execute_code", "bash"):
            # Keep stdout but cap it — the important data is in side-channel
            cap = 4000 if is_last else 2000
            if len(content) > cap:
                return content[:cap] + "\n... (output truncated)"
            return content

        if tool == "save_data":
            # Never need to see saved data in context again
            return self._summarize_result(tool, content)

        # Default: cap at reasonable size
        cap = 8000 if is_last else 4000
        if len(content) > cap:
            return content[:cap] + "\n... (truncated)"
        return content

    def _summarize_result(self, tool: str, content: str) -> str:
        """Create a concise summary of a tool result."""
        import json
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                if "links" in data:
                    count = len(data["links"]) if isinstance(data["links"], list) else "?"
                    return f'{{"links": [{count} items], "summary": "Link analysis complete"}}'
                if "saved" in data:
                    return json.dumps({"saved": data["saved"], "path": data.get("path", "")})
                # Generic dict summary: show keys and value lengths
                summary = {k: f"({len(str(v))} chars)" if len(str(v)) > 100 else v
                           for k, v in data.items()}
                s = json.dumps(summary, ensure_ascii=False, default=str)
                return s[:1500] if len(s) > 1500 else s
        except (json.JSONDecodeError, TypeError):
            pass
        # Fallback: first N chars
        return content[:500] + "..." if len(content) > 500 else content

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
