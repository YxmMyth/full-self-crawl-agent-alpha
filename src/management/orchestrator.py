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
import re
import shutil
import tempfile
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

        # Merge: file settings < explicit config (skip empty/falsy overrides)
        file_settings = _load_settings()
        self.config = {**file_settings, **(config or {})}
        # Merge nested llm config, but don't let empty strings override real values
        if "llm" in file_settings and "llm" in (config or {}):
            merged_llm = {**file_settings["llm"]}
            for k, v in (config or {}).get("llm", {}).items():
                if v:  # only override if value is truthy (non-empty)
                    merged_llm[k] = v
            self.config["llm"] = merged_llm

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
            api_base=llm_config.get("api_base", llm_config.get("base_url", os.environ.get("LLM_BASE_URL", ""))),
            model=llm_config.get("model", os.environ.get("LLM_MODEL", "claude-opus-4-5")),
        )

        self._browser = BrowserTool()
        await self._browser.start()

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

        registry.register("get_html", self._get_clean_html,
            "Get page HTML (cleaned: no scripts/styles). Use selector param to scope to a specific element for smaller output.",
            {"type": "object", "properties": {"selector": {"type": "string", "description": "Optional CSS selector to scope HTML (e.g. 'body', '.main-content', '#results')"}}})

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

        registry.register("screenshot", browser.take_screenshot,
            "Take a screenshot of the current page",
            {"type": "object", "properties": {"full_page": {"type": "boolean", "description": "Capture full page (default false)"}}})

        registry.register("evaluate_js", browser.evaluate,
            'Execute JavaScript in the browser with live DOM access.\n'
            'Use for: SPA/dynamic content, triggering JS events, extracting JS-rendered data.\n'
            'Returns the result of the last expression.\n'
            'Example: document.querySelectorAll(".item").length',
            {"type": "object", "properties": {"script": {"type": "string", "description": "JavaScript code to execute in the browser"}}, "required": ["script"]})

        # --- Extraction tools (2) ---
        registry.register("extract_css", lambda **kwargs: extract_with_css(browser, **kwargs),
            'Quick CSS-based extraction for well-structured pages.\n'
            'Returns: {"records": [...], "count": N}\n'
            '\n'
            'For repeating items (product lists, search results), set container:\n'
            '  selectors={"title": "h3 a", "price": ".price_color"}, container=".product_pod"\n'
            '\n'
            'For single values (page title, total count), omit container:\n'
            '  selectors={"page_title": "h1", "description": ".summary"}\n'
            '\n'
            'If the page structure is complex or nested, use execute_code with BeautifulSoup instead.',
            {"type": "object", "properties": {"selectors": {"type": "object", "description": 'Map field names to CSS selectors. Example: {"title": "h3 a", "price": ".price_color"}'}, "container": {"type": "string", "description": "CSS selector for repeating container. Each match becomes one record."}}, "required": ["selectors"]})

        registry.register("intercept_api", lambda **kwargs: intercept_api(browser, **kwargs),
            'Intercept API/XHR responses matching a URL pattern.\n'
            'Use when the page loads data via AJAX/fetch. Triggers an action (scroll, wait, or click)\n'
            'then captures matching network responses.\n'
            'Example: url_pattern="/api/products", action="scroll"',
            {"type": "object", "properties": {"url_pattern": {"type": "string", "description": "URL substring to match against network requests"}, "action": {"type": "string", "description": "Action to trigger: scroll, wait, click:selector"}, "timeout": {"type": "integer", "description": "Timeout in seconds (default 10)"}}, "required": ["url_pattern"]})

        # --- Analysis tools (3) ---
        registry.register("analyze_page", lambda: analyze_page(browser),
            'Analyze current page structure: page type, SPA detection, data containers.\n'
            'Use as a first step to understand what extraction approach to use.',
            {"type": "object", "properties": {}})

        registry.register("analyze_links", lambda **kwargs: analyze_links(browser, **{k: v for k, v in kwargs.items() if k in ('html', 'base_url')}),
            'Extract and categorize all links on the current page.\n'
            'Returns links grouped by type (navigation, pagination, detail, external).\n'
            'Takes no parameters — operates on the current page.',
            {"type": "object", "properties": {}})

        registry.register("search_page", lambda **kwargs: search_page_tool(browser, **kwargs),
            "Search for text patterns on the current page",
            {"type": "object", "properties": {"query": {"type": "string", "description": "Text or regex pattern to search"}, "regex": {"type": "boolean", "description": "Use regex matching (default false)"}}, "required": ["query"]})

        # --- Execution (1-2) ---
        is_docker = os.path.exists("/.dockerenv")
        registry.register("execute_code", self._execute_code_with_context,
            'Execute Python with access to the current page.\n'
            '\n'
            'Pre-loaded variables:\n'
            '- page_html (str): cleaned HTML of the current page\n'
            '- page_url (str): current page URL\n'
            '\n'
            'Pre-loaded functions:\n'
            '- save_records(records): Persist extracted records (list of dicts or single dict).\n'
            '  Data saved via this function is automatically collected by the pipeline.\n'
            '  ALWAYS call save_records() when your code produces data.\n'
            '- report_urls(urls): Report discovered target URLs for extraction.\n'
            '  Use during exploration to report pages that contain target data.\n'
            '\n'
            'Available libraries: bs4, json, re, lxml, csv, collections\n'
            '\n'
            'WHEN TO USE: Complex HTML parsing, data transformation, regex extraction,\n'
            'or when extract_css doesn\'t capture what you need. This is your most\n'
            'powerful extraction tool — write Python the way a developer would.\n'
            '\n'
            'Example (extraction):\n'
            '  from bs4 import BeautifulSoup\n'
            '  soup = BeautifulSoup(page_html, "html.parser")\n'
            '  items = [{"title": el.text.strip()} for el in soup.select(".product h3")]\n'
            '  save_records(items)\n'
            '\n'
            'Example (exploration):\n'
            '  from bs4 import BeautifulSoup\n'
            '  soup = BeautifulSoup(page_html, "html.parser")\n'
            '  urls = [a["href"] for a in soup.select("a[href]") if "/paper/" in a["href"]]\n'
            '  report_urls(urls)',
            {"type": "object", "properties": {"code": {"type": "string", "description": "Python source code to execute. page_html and page_url are pre-loaded."}, "language": {"type": "string", "enum": ["python", "bash", "javascript"] if is_docker else ["python"], "description": "Programming language (default: python)"}, "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"}}, "required": ["code"]})

        if is_docker:
            registry.register("bash", self._bash_with_context,
                'Run a bash command. Use for direct HTTP requests, JSON processing, text manipulation.\n'
                '\n'
                'Env vars auto-injected:\n'
                '- $PAGE_HTML_FILE: path to current page HTML file\n'
                '- $PAGE_URL: current page URL\n'
                '\n'
                'Examples:\n'
                '  curl -s "https://api.site.com/data" | jq ".items[] | {name, price}"\n'
                '  cat "$PAGE_HTML_FILE" | grep -oP \'href="([^"]+)"\' | sort -u\n'
                '\n'
                'Use when you discover API endpoints or need Unix text processing.',
                {"type": "object", "properties": {"command": {"type": "string", "description": "Bash command(s) to execute"}}, "required": ["command"]})

        # --- Verification (1) ---
        registry.register("verify_quality", verify_quality_tool,
            'Check extracted data quality. Returns a score (0-1) and specific issues.\n'
            'Pass the records you want to verify. Use to decide if extraction is good enough.',
            {"type": "object", "properties": {"data": {"type": "array", "items": {"type": "object"}, "description": "Extracted data records to verify"}}, "required": ["data"]})

        # --- Storage (2) ---
        registry.register("save_data", self._save_data_tool,
            'Save extracted data records to file.\n'
            'If called without data, automatically saves all records collected via save_records().\n'
            'Returns: {"saved": N, "path": "...", "format": "json"}',
            {"type": "object", "properties": {"data": {"type": "array", "items": {"type": "object"}, "description": "Data records to save. If omitted, saves all records collected so far."}, "format": {"type": "string", "enum": ["json", "csv"], "description": "Output format (default json)"}}})

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
            max_steps=50, max_llm_calls=30, max_time_seconds=300,
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
        prior_experience = None  # Accumulated cross-page context

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
                "prior_experience": prior_experience,
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

            # Build cross-page experience: raw facts, agent decides what's useful
            successful_tools = result.get("successful_tools", [])
            prior_experience = (
                f"Previous page ({task_item.url}): "
                f"{len(page_data)} records extracted, {len(all_data)} total so far.\n"
                f"Successful tool calls: {json.dumps(successful_tools[-5:], default=str, ensure_ascii=False)}\n"
                f"Sample record: {json.dumps(page_data[0], default=str, ensure_ascii=False) if page_data else 'none'}"
            )

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

    async def _execute_code_with_context(self, code: str, language: str = "python", timeout: int = 30) -> dict:
        """Execute code with current browser state injected."""
        from ..tools.code_runner import execute_code

        ctx_dir = tempfile.mkdtemp(prefix="crawl_ctx_")
        try:
            # Write browser state to temp files
            try:
                html = await self._get_clean_html()
            except Exception:
                html = ""
            Path(os.path.join(ctx_dir, "page.html")).write_text(html, encoding="utf-8")

            url = ""
            if self._browser:
                try:
                    url = await self._browser.current_url() if callable(getattr(self._browser, 'current_url', None)) else (self._browser.current_url or "")
                except Exception:
                    url = ""
            Path(os.path.join(ctx_dir, "page_url.txt")).write_text(url, encoding="utf-8")

            if language == "python":
                preamble = (
                    f'import os as _os, json as _json\n'
                    f'_ctx = r"{ctx_dir}"\n'
                    f'with open(_os.path.join(_ctx, "page.html"), "r", encoding="utf-8") as _f:\n'
                    f'    page_html = _f.read()\n'
                    f'with open(_os.path.join(_ctx, "page_url.txt"), "r") as _f:\n'
                    f'    page_url = _f.read().strip()\n'
                    f'\n'
                    f'def save_records(records):\n'
                    f'    """Persist extracted records to the data pipeline. Call this to save your results."""\n'
                    f'    _recs = records if isinstance(records, list) else [records]\n'
                    f'    with open(_os.path.join(_ctx, "records.jsonl"), "a", encoding="utf-8") as _rf:\n'
                    f'        for _r in _recs:\n'
                    f'            _rf.write(_json.dumps(_r, ensure_ascii=False, default=str) + "\\n")\n'
                    f'    print(f"Saved {{len(_recs)}} records")\n'
                    f'\n'
                    f'def report_urls(urls):\n'
                    f'    """Report discovered target URLs for extraction. Use during exploration."""\n'
                    f'    _urls = urls if isinstance(urls, list) else [urls]\n'
                    f'    with open(_os.path.join(_ctx, "urls.txt"), "a", encoding="utf-8") as _uf:\n'
                    f'        for _u in _urls:\n'
                    f'            _uf.write(_u.strip() + "\\n")\n'
                    f'    print(f"Reported {{len(_urls)}} URLs")\n'
                )
                code = preamble + "\n" + code
            elif language == "bash":
                preamble = (
                    f'export PAGE_HTML_FILE="{ctx_dir}/page.html"\n'
                    f'export PAGE_URL="$(cat "{ctx_dir}/page_url.txt")"\n'
                )
                code = preamble + code

            result = await execute_code(code, language, timeout)

            # Read side-channel files (records and urls written by helper functions)
            records_file = os.path.join(ctx_dir, "records.jsonl")
            if os.path.exists(records_file):
                collected = []
                with open(records_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                collected.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                if collected:
                    result["_records"] = collected
                    logger.info(f"execute_code side-channel: {len(collected)} records collected")

            urls_file = os.path.join(ctx_dir, "urls.txt")
            if os.path.exists(urls_file):
                discovered = []
                with open(urls_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            discovered.append(line)
                if discovered:
                    result["_urls"] = discovered
                    logger.info(f"execute_code side-channel: {len(discovered)} URLs reported")

            return result
        finally:
            shutil.rmtree(ctx_dir, ignore_errors=True)

    async def _bash_with_context(self, command: str, timeout: int = 30) -> dict:
        """Run bash command with browser state in env vars."""
        return await self._execute_code_with_context(command, language="bash", timeout=timeout)

    async def _get_clean_html(self, selector: str | None = None) -> str:
        """Get page HTML with noise removed (scripts, styles, comments)."""
        raw = await self._browser.get_html(selector)
        # Remove script, style, svg, noscript tags and their contents
        cleaned = re.sub(r'<(script|style|svg|noscript)[^>]*>.*?</\1>', '', raw, flags=re.DOTALL | re.IGNORECASE)
        # Remove HTML comments
        cleaned = re.sub(r'<!--.*?-->', '', cleaned, flags=re.DOTALL)
        # Remove common noise attributes (data-*, onclick, style, class with long hashes)
        cleaned = re.sub(r'\s+(data-[\w-]+|onclick|onload|onerror)="[^"]*"', '', cleaned)
        cleaned = re.sub(r'\s+style="[^"]*"', '', cleaned)
        # Collapse whitespace
        cleaned = re.sub(r'\s{2,}', ' ', cleaned)
        cleaned = re.sub(r'>\s+<', '>\n<', cleaned)
        return cleaned.strip()

    async def _save_data_tool(self, data: list, format: str = "json") -> dict:
        """Tool: save extracted data."""
        import os
        from ..tools.storage import DataExport
        exporter = DataExport()
        os.makedirs("./evidence", exist_ok=True)
        filepath = f"./evidence/extracted_data.{format}"
        if format == "csv":
            exporter.to_csv(data, filepath)
        else:
            exporter.to_json(data, filepath)
        return {"saved": len(data), "path": filepath, "format": format}

    async def _download_file_tool(self, url: str, filename: str = "") -> dict:
        """Tool: download a file."""
        from ..tools.downloader import FileDownloader
        downloader = FileDownloader()
        # Infer file_type from URL
        from urllib.parse import urlparse
        path = urlparse(url).path
        ext = path.rsplit(".", 1)[-1] if "." in path else "bin"
        result = downloader.download(url, file_type=ext, filename=filename or None)
        return result

    async def cleanup(self) -> None:
        """Release resources."""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
