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

Mission: find URLs of pages that contain the target data, then report them with report_urls() inside execute_code.

## Strategy: macro → micro

Start with the biggest picture first, then narrow down:
1. Read your Site Intelligence briefing (below) — it's pre-fetched reconnaissance, not instructions
2. Reason: what do the entry_points, live_endpoints, and sitemap_sample tell you about this site?
3. If the briefing is incomplete, use your tools to fill gaps:
   - search_site(query) — search within the domain. Call multiple times with different keywords
   - probe_endpoint(path) — HTTP HEAD check: does /topics/threejs exist? (fast, no browser)
   - navigate + analyze_links() — see what's actually rendered
   - bash — curl REST APIs, fetch sitemaps, hit JSON endpoints

## Tools decision guide

- search_site: best for finding content when you know what you're looking for
  → Can call multiple times! If first keywords miss, try different terms
- probe_endpoint: validate a hypothesis cheaply before navigating
  → "Does /tag/threejs exist?" → probe first, then navigate if it does
- analyze_links(): for SPA pages where JS renders the links
- bash: for REST APIs, non-browser accessible data

## Reading navigate() results

1. hint "SPA loaded" or "network still active" → call analyze_links() RIGHT NOW (don't re-navigate)
2. element_count < 20 → page empty/blocked → try bash or different URL
3. strategy "networkidle" → fully rendered → analyze_links() or get_html()

## Reporting URLs

Call report_urls([url1, url2, ...]) inside execute_code as SOON as you find promising URLs.
Do NOT accumulate URLs and report once at the end — report incrementally as you discover them.
This way, even if your exploration is cut short, your findings are preserved.

## Opportunistic extraction (optional, bounded)

You MAY extract a 1-record sample from a URL to validate content quality. This is optional but useful.
If you do extract a sample:
  1. Extract just 1 record (to confirm the page has the target data)
  2. Immediately ALSO call report_urls([current_url]) in the same execute_code call
  3. Do NOT spend many steps extracting all records — that is Phase 2's job

## Site Modeling (REQUIRED — write before finishing)

Before you finish, you MUST call write_run_knowledge('site_model', {...}) so every
Extractor starts with your understanding instead of from zero.

Required fields:
  {
    "structure": "e.g. listing: /tag/*, content: /pen/[slug]",
    "estimated_total": N,          # how many target items exist on the site
    "estimation_basis": "...",     # how you estimated (pagination, search count, sitemap)
    "content_url_pattern": "...",  # regex or glob for content page URLs
    "extraction_hint": "..."       # brief hint: js_extract_save? CSS selector? API?
  }

If you sampled any pages and extraction worked, also write the proven script:
  write_run_knowledge('proven_scripts', {
    '*/pen/*': {'script': '() => ({title: document.title, code: ...})'}
  })

## When to stop

Once you have enough target URLs reported AND you've written the site model, you're done.
If truly nothing found after 3+ different approaches, call report_urls([]) and still
write whatever partial site_model you could determine."""

            feedback = task.get("feedback")
            if feedback:
                role_text += (
                    f"\n\nPrevious exploration produced poor results:\n"
                    f"- These pages yielded no data: {feedback.get('failed_pages', [])}\n"
                    f"- Records collected so far: {feedback.get('total_records', 0)}\n"
                    f"- Try different sections, URL patterns, or listing pages this time."
                )

            frontier_summary = task.get("frontier_summary")
            if frontier_summary:
                role_text += f"\n\nQuality feedback from extraction phase:\n{frontier_summary}"
        else:
            role_text = """You are a goal-pursuing data agent.

Mission: find and extract the target data. Your starting URL may or may not be where the data lives — observe the page and follow the evidence toward your goal.

## Start with Run Knowledge

Call read_run_knowledge() as your FIRST action. It contains:
- proven_scripts: JS extraction scripts that already work for URL patterns like yours
- site_model: structure, estimated total items, content URL patterns
- golden_records (via golden_summary in task context): what good data looks like

If proven_scripts has a match for your URL pattern → use it directly. Skip trial-and-error.
After a successful extraction → call write_run_knowledge('proven_scripts', {pattern: {script: ...}})
so future agents don't have to rediscover it.

If the task context shows a ⚡ verified skill for this URL, call it first — it was verified to work.

## Observe before acting

After every navigate(), call think() to assess the situation relative to your goal:
- What is on this page? Does it directly contain the target data?
- If yes → choose the right extraction approach for the structure.
- If the data is elsewhere → what path leads there? Is it worth the steps?

<think_example>
I landed on /tag/threejs. I see 48 pen thumbnails linking to /pen/[slug]. My goal is pen
source code (JS/HTML/CSS). The code is on individual pen pages, not here.
Best use of my remaining steps: navigate to 2-3 representative pens, extract their code,
then report the remaining pen URLs via report_urls() for parallel processing.
</think_example>

<think_example>
I'm on /pen/xyz but the page title says "Discussion about xyz" — no editor visible.
My goal is pen code. I see a link to the actual pen. I should navigate there, not
attempt extraction from a discussion page.
</think_example>

## Navigation

You may navigate to follow leads. If the data is one level deeper, go there.
If you find more target URLs than your step budget can handle, call report_urls() —
the system picks them up for parallel processing.
Navigate efficiently: only when it genuinely moves you toward the goal.

When done: if you found a working extraction method that wasn't already in run_knowledge,
call write_run_knowledge('proven_scripts', {'URL_PATTERN': {'script': 'YOUR_SCRIPT'}})
before saying "TASK COMPLETE"."""

        site_context = task.get("site_context", "")
        if site_context:
            role_text += f"\n\nSite context from exploration phase:\n{site_context}"

        import os
        rules = """
Environment:
- You are running in a headless Chromium browser inside a Docker container.
- Some sites detect headless browsers and may block access or serve challenge pages.
- If browser navigation fails or pages appear empty, try bash(curl ...) for direct HTTP access or look for API endpoints.
- navigate() returns page metadata including load_time_ms and element_count - use this to detect blocked or empty pages.
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
- After receiving important tool results (navigate, search, analyze), call think() to reason about what you've seen before your next action.
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

        role = task.get("role", "extraction")
        url_label = "Starting URL" if role == "extraction" else "Target URL"
        parts = [f"{url_label}: {url}"]

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

        # Site intelligence: Phase 0 multi-signal discovery results (if available)
        site_intel = task.get("site_intel")
        if site_intel:
            intel_lines = ["\n## Site Intelligence (pre-fetched before your session)"]
            if site_intel.entry_points:
                ep_urls = [u.url for u in site_intel.entry_points[:8]]
                intel_lines.append(
                    f"Likely entry/listing pages ({len(site_intel.entry_points)} found): {ep_urls}"
                )
            if site_intel.direct_content:
                dc_urls = [u.url for u in site_intel.direct_content[:5]]
                intel_lines.append(
                    f"Search-validated content pages ({len(site_intel.direct_content)} found): {dc_urls}"
                    " — these may already have the data you need"
                )
            if site_intel.live_endpoints:
                intel_lines.append(f"Confirmed live endpoints (HTTP): {site_intel.live_endpoints}")
            if site_intel.sitemap_sample:
                intel_lines.append(f"Sitemap URL sample: {site_intel.sitemap_sample[:8]}")
            if site_intel.robots_txt:
                intel_lines.append(
                    f"robots.txt excerpt:\n{site_intel.robots_txt[:300]}"
                )
            intel_lines.append(
                "This is a briefing — use it as a starting point, not as instructions. "
                "You may search_site() with different keywords, probe_endpoint() new paths, "
                "or navigate elsewhere if the data requires it."
            )
            parts.extend(intel_lines)

        # Run intelligence: accumulated knowledge from Explorer + previous Extractors
        knowledge_summary = task.get("knowledge_summary", "")
        if knowledge_summary:
            parts.append(f"\n{knowledge_summary}")

        # Golden record summary: what verified good data looks like
        golden_summary = task.get("golden_summary", "")
        if golden_summary:
            parts.append(f"\n{golden_summary}")

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
