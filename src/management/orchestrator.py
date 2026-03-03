"""
Management: Orchestrator — top-level entry point.

Receives user task → builds environment → runs LLM-as-Controller → returns results.

Two modes:
- full_site (default): Exploration → SiteMap → Extraction per page
- single_page: Direct extraction (testing/debugging only)
"""

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("management.orchestrator")


def _load_settings(config_path: str = "config/settings.json") -> dict:
    """Load settings.json and resolve env var placeholders like ${VAR}."""
    import re
    path = Path(config_path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    # Replace ${VAR} with env var values
    def _replace(m):
        return os.environ.get(m.group(1), "")
    text = re.sub(r"\$\{(\w+)\}", _replace, text)
    return json.loads(text)


class Orchestrator:
    """Top-level orchestrator for crawl tasks.

    Lifecycle:
    1. Load config → init LLM/Browser
    2. Infer CrawlSpec (if user only gave URL + text)
    3. Register all tools to ToolRegistry
    4. Execute (full_site or single_page)
    5. Compile and return results
    """

    def __init__(self, config: dict | None = None):
        # Auto-load .env file
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass  # dotenv optional; env vars can be set manually

        # Merge: file settings < explicit config
        file_settings = _load_settings()
        self.config = {**file_settings, **(config or {})}
        # Merge nested llm config
        if "llm" in file_settings and "llm" in (config or {}):
            self.config["llm"] = {**file_settings["llm"], **(config or {}).get("llm", {})}

        self._browser = None
        self._llm = None
        self._tools = None
        self._state_mgr = None

    async def run(self, start_url: str, requirement: str = "",
                  spec_dict: dict | None = None,
                  mode: str = "full_site") -> dict:
        """Main entry point.

        Args:
            start_url: Target URL
            requirement: Natural language requirement
            spec_dict: Optional pre-built spec dict
            mode: "full_site" (default) or "single_page"

        Returns:
            {success, data, metrics, summary, ...}
        """
        task_id = str(uuid.uuid4())[:8]
        logger.info(f"Task {task_id}: {start_url} mode={mode}")

        try:
            # 1. Initialize components
            await self._init_components()

            # 2. Build or infer spec
            spec = await self._build_spec(start_url, requirement, spec_dict)

            # 3. Register tools
            tools = self._build_tools()

            # 4. Initialize state
            from .state import StateManager
            self._state_mgr = StateManager()
            self._state_mgr.create(task_id, start_url)
            self._state_mgr.update(task_id, status="running")

            # 5. Execute
            if mode == "single_page":
                result = await self._run_single_page(start_url, spec, tools, task_id)
            else:
                result = await self._run_full_site(start_url, spec, tools, task_id)

            # 6. Update state
            status = "completed" if result.get("success") else "failed"
            self._state_mgr.update(task_id, status=status)

            return result

        except Exception as e:
            logger.error(f"Task {task_id} failed: {e}", exc_info=True)
            if self._state_mgr:
                self._state_mgr.update(task_id, status="failed")
                self._state_mgr.add_error(task_id, str(e))
            return {"success": False, "error": str(e), "data": []}

        finally:
            await self.cleanup()

    async def _init_components(self) -> None:
        """Initialize LLM client and browser."""
        from ..tools.llm import LLMClient
        from ..tools.browser import BrowserTool

        llm_config = self.config.get("llm", {})
        self._llm = LLMClient(
            api_key=llm_config.get("api_key", os.environ.get("LLM_API_KEY", "")),
            base_url=llm_config.get("api_base", llm_config.get("base_url", os.environ.get("LLM_BASE_URL", ""))),
            model=llm_config.get("model", os.environ.get("LLM_MODEL", "claude-opus-4-5")),
        )

        self._browser = BrowserTool()
        await self._browser.launch()

    def _build_tools(self):
        """Register all 20 tools into a ToolRegistry."""
        from ..tools.registry import ToolRegistry
        from ..tools.code_runner import execute_code
        from ..tools.extraction import extract_with_css, intercept_api
        from ..tools.analysis import analyze_page, analyze_links, search_page_tool
        from ..verification.verifier import verify_quality_tool

        registry = ToolRegistry()
        browser = self._browser

        # --- Browser tools (11) ---
        registry.register("navigate", browser.navigate,
            "Navigate to a URL and wait for page load",
            {"type": "object", "properties": {"url": {"type": "string", "description": "URL to navigate to"}}, "required": ["url"]})

        registry.register("go_back", browser.go_back,
            "Go back to the previous page",
            {"type": "object", "properties": {}})

        registry.register("get_html", browser.get_html,
            "Get page HTML or specific element HTML",
            {"type": "object", "properties": {"selector": {"type": "string", "description": "Optional CSS selector"}}})

        registry.register("get_text", browser.get_text,
            "Get visible text content of the page",
            {"type": "object", "properties": {}})

        registry.register("click", browser.click,
            "Click an element on the page",
            {"type": "object", "properties": {"selector": {"type": "string", "description": "CSS selector of element to click"}}, "required": ["selector"]})

        registry.register("fill", browser.fill,
            "Fill an input field with text",
            {"type": "object", "properties": {"selector": {"type": "string", "description": "CSS selector of input"}, "value": {"type": "string", "description": "Text to fill"}}, "required": ["selector", "value"]})

        registry.register("select_option", browser.select_option,
            "Select an option from a dropdown",
            {"type": "object", "properties": {"selector": {"type": "string", "description": "CSS selector of select element"}, "value": {"type": "string", "description": "Option value to select"}}, "required": ["selector", "value"]})

        registry.register("press_key", browser.press_key,
            "Press a keyboard key (Enter, Escape, Tab, etc.)",
            {"type": "object", "properties": {"key": {"type": "string", "description": "Key to press (e.g., Enter, Escape, Control+a)"}}, "required": ["key"]})

        registry.register("scroll", browser.scroll,
            "Scroll the page in a direction",
            {"type": "object", "properties": {"direction": {"type": "string", "enum": ["down", "up"], "description": "Scroll direction"}, "pages": {"type": "number", "description": "Number of pages to scroll (default 1)"}}, "required": ["direction"]})

        registry.register("screenshot", browser.screenshot,
            "Take a screenshot of the current page",
            {"type": "object", "properties": {"full_page": {"type": "boolean", "description": "Capture full page (default false)"}}})

        registry.register("evaluate_js", browser.evaluate_js,
            "Execute JavaScript in the page context and return result",
            {"type": "object", "properties": {"script": {"type": "string", "description": "JavaScript code to execute"}}, "required": ["script"]})

        # --- Extraction tools (2) ---
        registry.register("extract_css", lambda **kwargs: extract_with_css(browser, **kwargs),
            "Extract data using CSS selectors from the current page",
            {"type": "object", "properties": {"selectors": {"type": "object", "description": "Map of field_name -> CSS selector"}, "container": {"type": "string", "description": "Optional container selector for list extraction"}}, "required": ["selectors"]})

        registry.register("intercept_api", lambda **kwargs: intercept_api(browser, **kwargs),
            "Intercept API/XHR responses matching a URL pattern",
            {"type": "object", "properties": {"url_pattern": {"type": "string", "description": "URL pattern to intercept"}, "action": {"type": "string", "description": "Action during intercept: scroll, wait, click:selector"}, "timeout": {"type": "integer", "description": "Timeout in seconds (default 10)"}}, "required": ["url_pattern"]})

        # --- Analysis tools (3) ---
        registry.register("analyze_page", lambda: analyze_page(browser),
            "Analyze current page structure (type, SPA detection, containers)",
            {"type": "object", "properties": {}})

        registry.register("analyze_links", lambda **kwargs: analyze_links(browser, **kwargs),
            "Extract and categorize links on the current page",
            {"type": "object", "properties": {"goal": {"type": "string", "description": "Optional goal to help prioritize links"}}})

        registry.register("search_page", lambda **kwargs: search_page_tool(browser, **kwargs),
            "Search for text patterns on the current page",
            {"type": "object", "properties": {"pattern": {"type": "string", "description": "Text or regex pattern to search"}, "regex": {"type": "boolean", "description": "Use regex matching (default false)"}}, "required": ["pattern"]})

        # --- Execution (1) ---
        is_docker = os.path.exists("/.dockerenv")
        registry.register("execute_code", execute_code,
            "Execute code (Python always available; bash/JS in Docker)",
            {"type": "object", "properties": {"code": {"type": "string", "description": "Code to execute"}, "language": {"type": "string", "enum": ["python", "bash", "javascript"] if is_docker else ["python"], "description": "Programming language"}, "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"}}, "required": ["code"]})

        # --- Verification (1) ---
        registry.register("verify_quality", verify_quality_tool,
            "Verify extracted data quality. Returns quality score and issues.",
            {"type": "object", "properties": {"data": {"type": "array", "items": {"type": "object"}, "description": "Extracted data records to verify"}}, "required": ["data"]})

        # --- Storage (2) ---
        registry.register("save_data", self._save_data_tool,
            "Save extracted data records",
            {"type": "object", "properties": {"data": {"type": "array", "items": {"type": "object"}, "description": "Data records to save"}, "format": {"type": "string", "enum": ["json", "csv"], "description": "Output format (default json)"}}, "required": ["data"]})

        registry.register("download_file", self._download_file_tool,
            "Download a file from a URL",
            {"type": "object", "properties": {"url": {"type": "string", "description": "URL to download"}, "filename": {"type": "string", "description": "Optional filename"}}, "required": ["url"]})

        self._tools = registry
        return registry

    async def _build_spec(self, url: str, requirement: str,
                          spec_dict: dict | None):
        """Build CrawlSpec from user input."""
        from ..strategy.spec import CrawlSpec, SpecInferrer, SpecLoader

        if spec_dict:
            return SpecLoader.from_dict(url, spec_dict)

        if requirement:
            inferrer = SpecInferrer(self._llm)
            return await inferrer.infer(url, requirement)

        # Minimal spec
        return CrawlSpec(url=url, requirement="Extract main content from this page")

    async def _run_full_site(self, url: str, spec, tools, task_id: str) -> dict:
        """Full site mode: exploration → extraction.

        Phase 1: Exploration controller discovers site structure
        Phase 2: Extraction controller processes each target page
        """
        from ..execution.controller import CrawlController
        from ..strategy.gate import CompletionGate
        from ..verification.verifier import RiskMonitor
        from .context import ContextManager
        from .governor import Governor

        all_data = []

        # --- Phase 1: Exploration ---
        logger.info(f"Phase 1: Exploring {url}")
        explore_governor = Governor(
            max_steps=15, max_llm_calls=10, max_time_seconds=120,
            monitor=RiskMonitor(),
        )
        explore_context = ContextManager(max_history_steps=3)

        explore_task = {
            "url": url,
            "spec": spec,
            "role": "exploration",
        }

        explore_controller = CrawlController(
            llm_client=self._llm,
            tools=tools,
            governor=explore_governor,
            context_mgr=explore_context,
        )

        explore_result = await explore_controller.run(explore_task)
        target_urls = explore_result.get("new_links", [])

        # If exploration found no links, try the start URL itself
        if not target_urls:
            target_urls = [url]

        logger.info(f"Phase 1 complete: found {len(target_urls)} target pages")

        # --- Phase 2: Extraction ---
        from .scheduler import CrawlFrontier
        frontier = CrawlFrontier(max_depth=1, max_urls=50)
        frontier.set_base_domain(url)
        frontier.add_batch(
            [{"url": u, "category": "detail"} for u in target_urls],
            depth=0, parent_url=url,
        )

        site_context = explore_result.get("summary", "")
        pages_extracted = 0

        while True:
            task_item = frontier.next()
            if not task_item:
                break

            logger.info(f"Phase 2: Extracting {task_item.url} ({pages_extracted+1}/{len(target_urls)})")

            extract_governor = Governor(
                max_steps=30, max_llm_calls=20, max_time_seconds=300,
                gate=CompletionGate(),
                monitor=RiskMonitor(),
            )
            extract_context = ContextManager(max_history_steps=3)

            extract_task = {
                "url": task_item.url,
                "spec": spec,
                "role": "extraction",
                "site_context": site_context,
            }

            extract_controller = CrawlController(
                llm_client=self._llm,
                tools=tools,
                governor=extract_governor,
                context_mgr=extract_context,
            )

            result = await extract_controller.run(extract_task)
            page_data = result.get("data", [])
            all_data.extend(page_data)
            pages_extracted += 1

            self._state_mgr.record_page_visit(task_id, task_item.url)
            self._state_mgr.add_data(task_id, page_data)

            # Check completion gate
            gate = CompletionGate()
            decision = gate.check(all_data, spec)
            if decision.met:
                logger.info(f"Completion gate met: {decision.reason}")
                break

        return {
            "success": len(all_data) > 0,
            "data": all_data,
            "pages_extracted": pages_extracted,
            "summary": f"Extracted {len(all_data)} records from {pages_extracted} pages",
            "metrics": {
                "pages_found": len(target_urls),
                "pages_extracted": pages_extracted,
                "records": len(all_data),
            },
        }

    async def _run_single_page(self, url: str, spec, tools, task_id: str) -> dict:
        """Single page mode — skip exploration, extract directly."""
        from ..execution.controller import CrawlController
        from ..strategy.gate import CompletionGate
        from ..verification.verifier import RiskMonitor
        from .context import ContextManager
        from .governor import Governor

        governor = Governor(
            max_steps=30, max_llm_calls=20, max_time_seconds=300,
            gate=CompletionGate(),
            monitor=RiskMonitor(),
        )
        context_mgr = ContextManager(max_history_steps=3)

        task = {
            "url": url,
            "spec": spec,
            "role": "extraction",
        }

        controller = CrawlController(
            llm_client=self._llm,
            tools=tools,
            governor=governor,
            context_mgr=context_mgr,
        )

        return await controller.run(task)

    async def _save_data_tool(self, data: list, format: str = "json") -> dict:
        """Tool: save extracted data."""
        from ..tools.storage import DataExport
        exporter = DataExport()
        path = exporter.save(data, format=format)
        return {"saved": len(data), "path": str(path), "format": format}

    async def _download_file_tool(self, url: str, filename: str = "") -> dict:
        """Tool: download a file."""
        from ..tools.downloader import FileDownloader
        downloader = FileDownloader()
        path = await downloader.download(url, filename=filename or None)
        return {"downloaded": str(path), "url": url}

    async def cleanup(self) -> None:
        """Release resources."""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
