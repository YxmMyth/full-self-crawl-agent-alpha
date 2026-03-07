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
        self._artifacts = None
        # URL source registry: tracks URLs the agent has actually seen on pages.
        # Used as a gate to prevent hallucinated URLs in download_file etc.
        self._seen_urls: set[str] = set()

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

            # 1.5. Initialize artifacts (clean start)
            from ..tools.artifacts import ArtifactManager
            self._artifacts = ArtifactManager()
            self._artifacts.init_run()

            # 2. Build spec + run Phase 0 discovery in parallel (no LLM needed for Phase 0)
            from urllib.parse import urlparse as _urlparse
            _domain = _urlparse(start_url).netloc
            spec, site_intel = await self._init_parallel(
                start_url, requirement, spec_dict, _domain
            )

            # 3. Register tools (domain passed for search_site + probe_endpoint)
            tools = self._build_tools(domain=_domain)

            # 4. Initialize state
            from .state import StateManager
            self._state_mgr = StateManager()
            self._state_mgr.create(task_id, start_url)
            self._state_mgr.update(task_id, status="running")

            # 5. Execute
            if mode == "single_page":
                result = await self._run_single_page(start_url, spec, tools, task_id)
            else:
                result = await self._run_full_site(start_url, spec, tools, task_id,
                                                   site_intel=site_intel)

            # 6. Update state
            status = "completed" if result.get("success") else "failed"
            self._state_mgr.update(task_id, status=status)

            # 7. Write artifacts manifest
            if self._artifacts:
                self._artifacts.add_records(result.get("data", []))
                self._artifacts.save_records_file()
                self._artifacts.write_manifest({
                    "task_id": task_id,
                    "url": start_url,
                    "mode": mode,
                    "success": result.get("success", False),
                    "summary": result.get("summary", ""),
                })
                result["artifacts_dir"] = str(self._artifacts.base_dir)
                result["files"] = self._artifacts.files

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

    def _build_tools(self, domain: str = ""):
        """Register all tools into a ToolRegistry.

        Args:
            domain: Target domain for domain-locked tools (search_site, probe_endpoint).
        """
        from ..tools.registry import ToolRegistry
        from ..tools.code_runner import execute_code
        from ..tools.extraction import extract_with_css, intercept_api
        from ..tools.analysis import analyze_page, analyze_links, search_page_tool
        from ..verification.verifier import verify_quality_tool

        registry = ToolRegistry()
        browser = self._browser

        # --- Browser tools (11) ---
        registry.register("navigate", self._navigate_tracking_wrapper,
            "Navigate to a URL. Returns page metadata: title, load_time_ms, element_count, text_content_length, and hints about blocked/empty pages. Use wait_until to control speed: 'domcontentloaded' (fast, 1-3s), 'load' (medium), 'networkidle' (slow, may timeout at 30s).",
            {"type": "object", "properties": {
                "url": {"type": "string", "description": "URL to navigate to"},
                "wait_until": {"type": "string", "enum": ["domcontentloaded", "load", "networkidle"], "description": "Page load strategy. Default 'networkidle' waits for all resources (slow). Use 'domcontentloaded' for faster loading when you don't need all resources."}
            }, "required": ["url"]})

        registry.register("go_back", browser.go_back,
            "Go back to the previous page",
            {"type": "object", "properties": {}})

        registry.register("get_html", self._get_clean_html,
            "Get page HTML (cleaned: no scripts/styles). Returns STATIC HTML only — for SPA/React pages, dynamically-loaded content will NOT appear here; use analyze_links() instead to get live DOM links.",
            {"type": "object", "properties": {"selector": {"type": "string", "description": "Optional CSS selector to scope HTML (e.g. 'body', '.main-content', '#results')"}}})

        registry.register("get_text", browser.get_text,
            "Get visible text content of the page",
            {"type": "object", "properties": {}})

        registry.register("get_page_state", browser.get_page_state,
            'Get ALL visible interactive elements as a numbered list — like browser-use.\n'
            'Returns [{index, tag, text, label, href, x, y}, ...] for every visible button/link/input.\n'
            'Use when you need to find a UI element without knowing its CSS selector.\n'
            'After calling this, use click_element(index) to interact with a specific element.\n'
            'Example: call get_page_state → see "[42] button \'Export\'" → call click_element(42)',
            {"type": "object", "properties": {}})

        registry.register("click_element", browser.click_element,
            'Click a numbered element from the last get_page_state() call.\n'
            'More reliable than click(selector) for dynamically discovered elements.\n'
            'Example: get_page_state returns index=42 for Export button → click_element(42)',
            {"type": "object",
             "properties": {"index": {"type": "integer", "description": "Element index from get_page_state()"}},
             "required": ["index"]})

        registry.register("download_url", self._download_url_tool,
            'Download a file from a URL using the authenticated browser session.\n'
            'Copies auth cookies + CF clearance from the current session so the download\n'
            'is authenticated and bypasses Cloudflare. Saves file to artifacts/files/.\n'
            'Returns {path, filename, size_bytes} on success or {error, url} on failure.\n'
            'After downloading a ZIP, call inspect_file(filename) to read its contents.\n'
            'Use for: CodePen ZIP export, PDF downloads, any file download requiring auth.',
            {"type": "object",
             "properties": {"url": {"type": "string", "description": "Full URL of the file to download"}},
             "required": ["url"]})

        registry.register("click", browser.click,
            "Click an element on the page by CSS selector. Use click_element(index) instead when you obtained the element from get_page_state().",
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

        registry.register("save_auth_state", browser.save_auth_state,
            "Save browser cookies and login state for reuse across runs. Call after logging in to persist the session.",
            {"type": "object", "properties": {}})
        registry.register("evaluate_js", browser.evaluate,
            'Execute JavaScript in the browser with live DOM access.\n'
            'REQUIRED: script must be an arrow function or expression — NOT a bare "return" statement.\n'
            '\n'
            'Correct syntax:\n'
            '  "() => document.title"                              — expression\n'
            '  "() => document.querySelectorAll(\'.item\').length"  — DOM query\n'
            '  "() => { const x = 1; return x + 2; }"            — block with return\n'
            '\n'
            'Wrong syntax (will error):\n'
            '  "return document.title"  — bare return not allowed\n'
            '\n'
            'Use for: SPA/dynamic content, JS object state (e.g. CodeMirror editors), triggering events.\n'
            'Returns the value of the expression/return statement.\n'
            'WARNING: If the result contains large strings (e.g., full source code), use js_extract_save instead to avoid polluting LLM context.',
            {"type": "object", "properties": {"script": {"type": "string", "description": "JavaScript arrow function or expression. Must NOT be a bare 'return' statement."}}, "required": ["script"]})

        registry.register("js_extract_save", self._js_extract_save,
            'Execute JavaScript and save the result DIRECTLY as extracted records — large data never enters LLM context.\n'
            'The script should return a dict (single record) or a list of dicts (multiple records).\n'
            '\n'
            'Use this instead of evaluate_js when:\n'
            '- The result contains large strings (source code, minified JS, full articles)\n'
            '- You want to save extracted data without the full content going through context\n'
            '\n'
            'Returns a summary: {"saved": N, "summary": [{field: "(N chars)", ...}]}\n'
            'The full content is saved to the records pipeline automatically.',
            {"type": "object", "properties": {"script": {"type": "string", "description": "JavaScript arrow function returning a dict or list of dicts to save as records."}}, "required": ["script"]})

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
            'Extract and categorize all links on the current live page (uses rendered DOM — works for SPA/React pages).\n'
            'Returns: {links: [{url, text, category}, ...], counts: {detail, list, nav, other}}\n'
            '\n'
            'category meanings:\n'
            '  "detail" = content pages you want (3+ path segments, e.g. /user/pen/slug)\n'
            '  "list"   = more listing/search pages to paginate through\n'
            '  "nav"    = site navigation — skip these\n'
            '  "other"  = check manually; URLs with 3+ segments are often detail pages\n'
            '\n'
            'Focus on "detail" links first, then check "other" for missed detail pages.\n'
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
            '- report_urls(urls): Report discovered target URLs for the extraction phase.\n'
            '  CRITICAL in exploration: URLs reported here become the targets for Phase 2.\n'
            '  If you don\'t call this, Phase 2 won\'t know where to extract data from.\n'
            '  URLs are validated against page content to prevent hallucination.\n'
            '- save_file(url, description=""): Download and save a file (PDF, image, dataset, etc.).\n'
            '  The file is saved as an artifact and tracked in the output manifest.\n'
            '  Use when you find valuable files to collect as samples.\n'
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
                'Site reconnaissance (exploration):\n'
                '  curl -sL "$PAGE_URL/../robots.txt" 2>/dev/null | grep -i sitemap\n'
                '  → finds Sitemap URL; then fetch it to see URL patterns and total item count\n'
                '  curl -sL SITEMAP_URL 2>/dev/null | grep -o "<loc>[^<]*</loc>" | head -40\n'
                '\n'
                'API discovery:\n'
                '  curl -s "https://api.site.com/data" | jq ".items[] | {name, price}"\n'
                '  cat "$PAGE_HTML_FILE" | grep -oP \'href="([^"]+)"\' | sort -u\n'
                '\n'
                'Use when you discover API endpoints, need sitemap reconnaissance, or Unix text processing.',
                {"type": "object", "properties": {"command": {"type": "string", "description": "Bash command(s) to execute"}}, "required": ["command"]})

        # --- Reasoning (1) ---
        async def _think_tool(thought: str) -> dict:
            """Think tool — zero side effects, just returns ok."""
            return {"status": "ok"}

        registry.register("think", _think_tool,
            'Use this tool to think and plan before taking action.\n'
            'It does NOT interact with the page or change any state — it just records your thought.\n'
            'Use it to:\n'
            '- Plan your approach before complex tasks\n'
            '- Analyze what you have learned so far\n'
            '- Reason about why something failed and what to try next\n'
            '- Decide between multiple strategies\n'
            'Returns: {"status": "ok"}',
            {"type": "object", "properties": {"thought": {"type": "string", "description": "Your reasoning, analysis, or plan"}}, "required": ["thought"]})

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
            'Download a file (PDF, image, dataset, archive, etc.) from a URL.\n'
            'File is saved to artifacts/files/ and tracked in the output manifest.\n'
            'Use when you already know the direct URL of the file.\n'
            'Returns: {"filename": "...", "size": N, "type": "pdf", "success": true}',
            {"type": "object", "properties": {"url": {"type": "string", "description": "URL of the file to download"}, "filename": {"type": "string", "description": "Optional filename (auto-detected from URL if omitted)"}, "description": {"type": "string", "description": "What this file is (e.g. 'sample research paper')"}}, "required": ["url"]})

        registry.register("click_download", self._click_download_tool,
            'Click an element that triggers a file download (e.g., Export button, Download link).\n'
            'Captures the browser download and saves it as an artifact.\n'
            'Use when a file can only be obtained by clicking a button, not via direct URL.\n'
            'Returns: {"filename": "...", "size": N, "type": "zip", "success": true}',
            {"type": "object", "properties": {"selector": {"type": "string", "description": "CSS selector of the element to click (e.g., 'button.export', 'a[download]')"}, "description": {"type": "string", "description": "What this file is (e.g., 'CodePen export zip')"}}, "required": ["selector"]})

        registry.register("inspect_file", self._inspect_file_tool,
            'Inspect a downloaded file and return metadata.\n'
            'For PDFs: pages, text extractability, metadata, first page preview.\n'
            'For images: dimensions, format, color mode.\n'
            'For CSV/JSON: row/item counts, headers.\n'
            'Use to assess file quality before deciding if it is a good sample.',
            {"type": "object", "properties": {"filename": {"type": "string", "description": "Name of the file in artifacts/files/ to inspect"}}, "required": ["filename"]})

        # --- Discovery tools: domain-locked, Phase 1 agent use ---
        if domain:
            from ..tools.search_tool import SearchSiteTool
            from ..tools.probe_tool import ProbeEndpointTool
            _search_tool = SearchSiteTool(domain)
            _probe_tool = ProbeEndpointTool(domain)
            registry.register(
                "search_site",
                _search_tool.run,
                f"Search within {domain} using a natural language query. "
                f"Calls site:{domain} {{query}} via DuckDuckGo. "
                "Returns {results: [{url, title, snippet}], total}. "
                "Use when you want to find content pages matching a specific topic. "
                "Can call multiple times with different keywords to refine discovery. "
                "IMPORTANT: Only searches within the target domain — cannot search outside.",
                {"type": "object",
                 "properties": {
                     "query": {"type": "string", "description": "Search query (domain prefix added automatically)"},
                     "max_results": {"type": "integer", "description": "Max results to return (default 10)"},
                 },
                 "required": ["query"]},
            )
            registry.register(
                "probe_endpoint",
                _probe_tool.run,
                f"Quick HTTP HEAD check to test if a URL path exists on {domain}. "
                "Returns {url, exists: bool, status: int}. "
                "Use to verify a hypothesis (e.g. does /topics/threejs exist?) "
                "before spending browser time navigating there. "
                "Much faster than navigate() — no page rendering needed.",
                {"type": "object",
                 "properties": {
                     "path": {"type": "string", "description": "URL path to probe, e.g. '/search' or '/topics/threejs'"},
                 },
                 "required": ["path"]},
            )

        self._tools = registry
        return registry

    async def _phase0_discover_legacy(self, url: str) -> dict:
        """Kept for reference only. Replaced by _phase0_discover + discovery.engine."""
        return {"robots_txt": "", "sitemap_candidates": [], "sitemap_found": False}

    async def _phase0_discover(self, domain: str, requirement: str):
        """Phase 0: multi-signal domain discovery. Non-LLM, runs in ~3s.

        Returns SiteIntelligence with entry_points, direct_content, live_endpoints,
        sitemap_sample, robots_txt. Passed to Phase 1 agent as initial briefing.
        """
        from ..discovery.engine import discover
        from urllib.parse import urlparse
        # Strip port if present to get clean domain
        netloc = domain.split(":")[0]
        return await discover(netloc, requirement, browser=self._browser)

    async def _init_parallel(self, url: str, requirement: str,
                             spec_dict, domain: str):
        """Run SpecInferrer and Phase 0 discovery in parallel.

        Returns (spec, site_intel). Both are ready when Phase 1 starts.
        """
        import asyncio
        spec_task = self._build_spec(url, requirement, spec_dict)
        discover_task = self._phase0_discover(domain, requirement)
        spec, site_intel = await asyncio.gather(spec_task, discover_task)
        return spec, site_intel


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

    async def _run_full_site(self, url: str, spec, tools, task_id: str,
                             site_intel=None) -> dict:
        """Full site mode: exploration → extraction → optional re-exploration.

        Implements a macro agent loop:
          explore → extract → quality-check → (re-explore if needed) → done

        Quality gate: 3 consecutive empty pages after at least 3 pages tried
        → build deterministic feedback dict → re-explore with that context.
        Max 2 exploration rounds (no infinite loops).

        experience_log accumulates cross-page context across the entire run
        (Reflexion pattern: reflections accumulate, never replaced).
        """
        from ..execution.controller import CrawlController
        from ..strategy.gate import CompletionGate
        from ..verification.verifier import RiskMonitor
        from .context import ContextManager
        from .governor import Governor

        all_data = []
        all_files = []
        # Deduplication: track content fingerprints (title + first 100 chars of js_code)
        _seen_fingerprints: set[str] = set()

        # Seed seen_urls with the start URL
        self._seen_urls.add(url)

        # Accumulated cross-page context — grows throughout the run, never replaced
        experience_log: list[str] = []
        exploration_feedback: dict | None = None
        total_pages_extracted = 0
        last_target_urls: list[str] = [url]

        for _explore_round in range(2):
            # --- Phase 1: Exploration ---
            logger.info(f"Phase 1: Exploring {url} (round {_explore_round + 1})")
            explore_governor = Governor(
                max_steps=50, max_llm_calls=50, max_time_seconds=300,
                monitor=RiskMonitor(),
            )
            explore_context = ContextManager(max_history_steps=3)

            explore_task: dict = {
                "url": url,
                "spec": spec,
                "role": "exploration",
                "site_intel": site_intel,
            }
            if exploration_feedback:
                explore_task["feedback"] = exploration_feedback

            explore_controller = CrawlController(
                llm_client=self._llm,
                tools=tools,
                governor=explore_governor,
                context_mgr=explore_context,
            )

            explore_result = await explore_controller.run(explore_task)
            target_urls = explore_result.get("new_links", [])
            last_target_urls = target_urls or [url]

            # Preserve Phase 1 records (exploration may collect metadata)
            phase1_data = explore_result.get("data", [])
            if phase1_data:
                for r in phase1_data:
                    fp = (r.get("title", ""), (r.get("js_code") or "")[:100])
                    if fp not in _seen_fingerprints:
                        _seen_fingerprints.add(fp)
                        all_data.append(r)
                logger.info(f"Phase 1 collected {len(phase1_data)} records")

            # If exploration found no links, try the start URL itself
            if not target_urls:
                target_urls = [url]

            logger.info(f"Phase 1 complete: found {len(target_urls)} target pages")

            # --- Phase 2: Extraction ---
            from .scheduler import CrawlFrontier
            frontier = CrawlFrontier(max_depth=1, max_urls=50)
            frontier.set_base_domain(url)

            # Phase 0 direct_content: search-validated pages, highest priority
            if site_intel and site_intel.direct_content:
                priority_urls = [{"url": u.url, "category": "detail"}
                                 for u in site_intel.direct_content]
                frontier.add_batch(priority_urls, depth=0, parent_url=url)
                logger.info(f"Phase 2: pre-seeded {len(priority_urls)} Phase 0 direct_content URLs")

            # Phase 1 report_urls() discoveries
            frontier.add_batch(
                [{"url": u, "category": "detail"} for u in target_urls],
                depth=0, parent_url=url,
            )

            site_context = explore_result.get("summary", "")
            pages_extracted = 0
            prior_experience = "\n---\n".join(experience_log) if experience_log else None
            zero_record_streak = 0
            needs_reexplore = False
            failed_pages_this_round: list[str] = []

            while True:
                task_item = frontier.next()
                if not task_item:
                    break

                logger.info(
                    f"Phase 2: Extracting {task_item.url} "
                    f"({total_pages_extracted + pages_extracted + 1}/{len(target_urls)})"
                )

                extract_governor = Governor(
                    max_steps=30, max_llm_calls=30, max_time_seconds=300,
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
                page_files = result.get("files", [])
                # Deduplicate: skip records with same title+js_code fingerprint
                new_records = []
                for r in page_data:
                    fp = (r.get("title", ""), (r.get("js_code") or "")[:100])
                    if fp not in _seen_fingerprints:
                        _seen_fingerprints.add(fp)
                        new_records.append(r)
                page_data = new_records
                all_data.extend(page_data)
                all_files.extend(page_files)
                pages_extracted += 1

                self._state_mgr.record_page_visit(task_id, task_item.url)
                self._state_mgr.add_data(task_id, page_data)

                # Track consecutive empty pages for quality gate
                if len(page_data) == 0:
                    zero_record_streak += 1
                    failed_pages_this_round.append(task_item.url)
                else:
                    zero_record_streak = 0

                # Build accumulated cross-page experience (Reflexion pattern)
                successful_tools = result.get("successful_tools", [])
                failed_tools = result.get("failed_tools", [])
                stop_reason = result.get("stop_reason", "")
                metrics = result.get("metrics", {})
                elapsed = metrics.get("elapsed_seconds", 0)
                steps = result.get("steps", 0)
                avg_time = round(elapsed / max(steps, 1), 1)

                entry = (
                    f"Page {total_pages_extracted + pages_extracted} ({task_item.url}): "
                    f"{len(page_data)} records, {len(all_data)} total so far.\n"
                    f"  stop={stop_reason}, {elapsed:.0f}s, {steps} steps (~{avg_time}s/step)\n"
                    f"  code_that_worked={json.dumps(successful_tools[-3:], default=str, ensure_ascii=False)}\n"
                    f"  failed={json.dumps(failed_tools[-3:], default=str, ensure_ascii=False)}\n"
                    f"  sample={json.dumps(page_data[0], default=str, ensure_ascii=False) if page_data else 'none'}"
                )
                experience_log.append(entry)
                if len(experience_log) > 3:
                    experience_log = experience_log[-3:]
                prior_experience = "\n---\n".join(experience_log)

                # Quality gate: 3 consecutive empty pages → trigger re-exploration
                # Only on round 0 (one re-exploration allowed)
                if zero_record_streak >= 3 and pages_extracted >= 3 and _explore_round == 0:
                    exploration_feedback = {
                        "failed_pages": failed_pages_this_round[-5:],
                        "pages_tried": pages_extracted,
                        "total_records": len(all_data),
                        "sample_record": all_data[0] if all_data else None,
                    }
                    needs_reexplore = True
                    logger.info(
                        f"Quality gate: {zero_record_streak} consecutive empty pages after "
                        f"{pages_extracted} tried. Triggering re-exploration."
                    )
                    break

                # Check completion gate
                gate = CompletionGate()
                decision = gate.check(all_data, spec)
                if decision.met:
                    logger.info(f"Completion gate met: {decision.reason}")
                    break

            total_pages_extracted += pages_extracted
            if not needs_reexplore:
                break
            logger.info(f"Round {_explore_round + 1} done. Re-exploring with feedback.")

        # Track files in artifact manager
        if self._artifacts and all_files:
            self._artifacts.add_files(all_files)

        file_summary = f", {len(all_files)} files" if all_files else ""
        return {
            "success": len(all_data) > 0 or len(all_files) > 0,
            "data": all_data,
            "files": all_files,
            "pages_extracted": total_pages_extracted,
            "summary": f"Extracted {len(all_data)} records{file_summary} from {total_pages_extracted} pages",
            "metrics": {
                "pages_found": len(last_target_urls),
                "pages_extracted": total_pages_extracted,
                "records": len(all_data),
                "files": len(all_files),
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
            max_steps=30, max_llm_calls=30, max_time_seconds=300,
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

    async def _navigate_tracking_wrapper(self, url: str, wait_until: str = "networkidle") -> dict:
        """Wrap navigate to track visited URLs and extract text content metrics."""
        result = await self._browser.navigate(url, wait_until=wait_until)
        # Track the final URL (after redirects) as a seen URL
        final_url = result.get("url", url)
        self._seen_urls.add(url)
        self._seen_urls.add(final_url)
        # Extract text content length for SPA detection
        try:
            text_len = await self._browser.page.evaluate(
                "document.body ? document.body.innerText.length : 0"
            )
            result["text_content_length"] = text_len
            elem_count = result.get("element_count", 0)
            # SPA shell detection: many elements but little text
            if elem_count > 50 and text_len < 200:
                # Wait briefly for SPA to finish rendering before agent acts on hint
                import asyncio
                await asyncio.sleep(2)
                # Re-measure after wait
                try:
                    text_len = await self._browser.page.evaluate(
                        "document.body ? document.body.innerText.length : 0"
                    )
                    result["text_content_length"] = text_len
                except Exception:
                    pass
                result["hint"] = (result.get("hint", "") +
                    " SPA detected (content still loading): do NOT navigate away. "
                    "Call analyze_links() to get all rendered links from the live DOM — "
                    "it reads the current rendered state, not the original server HTML.").strip()
        except Exception:
            result["text_content_length"] = -1
        return result

    async def _execute_code_with_context(self, code: str, language: str = "python", timeout: int = 30) -> dict:
        """Execute code with current browser state injected."""
        if not code or not code.strip():
            return {
                "error": "No code provided.",
                "hint": "Provide Python code to execute. Available helpers: save_records(records), report_urls(urls), save_file(url, description). Pre-loaded: page_html, page_url."
            }

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

            artifacts_dir = str(self._artifacts.files_dir) if self._artifacts else "./artifacts/files"

            if language == "python":
                preamble = (
                    f'import os as _os, json as _json\n'
                    f'_ctx = r"{ctx_dir}"\n'
                    f'_artifacts_files = r"{artifacts_dir}"\n'
                    f'_os.makedirs(_artifacts_files, exist_ok=True)\n'
                    f'with open(_os.path.join(_ctx, "page.html"), "r", encoding="utf-8") as _f:\n'
                    f'    page_html = _f.read()\n'
                    f'with open(_os.path.join(_ctx, "page_url.txt"), "r") as _f:\n'
                    f'    page_url = _f.read().strip()\n'
                    f'\n'
                    f'def save_records(records):\n'
                    f'    """Persist extracted records to the data pipeline. Call this to save your results."""\n'
                    f'    _recs = records if isinstance(records, list) else [records]\n'
                    f'    if len(page_html.strip()) < 500:\n'
                    f'        print(f"⚠️ Warning: page content is very small ({{len(page_html)}} chars). Page may be blocked or empty. Verify records are based on real content.")\n'
                    f'    # Content anchoring: check if record values appear in page content\n'
                    f'    _anchored = 0\n'
                    f'    _total_vals = 0\n'
                    f'    for _r in _recs[:3]:  # sample first 3 records\n'
                    f'        for _v in (list(_r.values()) if isinstance(_r, dict) else []):\n'
                    f'            _sv = str(_v).strip()\n'
                    f'            if len(_sv) > 5 and _sv not in ("True", "False", "None"):\n'
                    f'                _total_vals += 1\n'
                    f'                if _sv[:30] in page_html:\n'
                    f'                    _anchored += 1\n'
                    f'    if _total_vals > 0 and _anchored == 0:\n'
                    f'        print(f"⚠️ Content anchoring failed: none of the record values were found in page_html. Records may be hallucinated. Verify data comes from actual page content.")\n'
                    f'    with open(_os.path.join(_ctx, "records.jsonl"), "a", encoding="utf-8") as _rf:\n'
                    f'        for _r in _recs:\n'
                    f'            _rf.write(_json.dumps(_r, ensure_ascii=False, default=str) + "\\n")\n'
                    f'    _anchor_info = f" ({{_anchored}}/{{_total_vals}} values anchored in page)" if _total_vals > 0 else ""\n'
                    f'    print(f"Saved {{len(_recs)}} records{{_anchor_info}}")\n'
                    f'\n'
                    f'def report_urls(urls):\n'
                    f'    """Report discovered target URLs for extraction. URLs are validated by structure."""\n'
                    f'    import re as _re\n'
                    f'    from urllib.parse import urlparse as _urlparse\n'
                    f'    _urls = urls if isinstance(urls, list) else [urls]\n'
                    f'    # Common utility/nav paths — never content detail pages\n'
                    f'    _NAV = {{"login","signin","signup","register","logout","about","contact","help",\n'
                    f'            "faq","support","docs","documentation","pricing","terms","privacy",\n'
                    f'            "policy","legal","blog","news","press","careers","jobs","trending",\n'
                    f'            "explore","discover","popular","featured","settings","account",\n'
                    f'            "dashboard","search","results","notifications"}}\n'
                    f'    _accepted = []\n'
                    f'    _rejected = []\n'
                    f'    for _u in _urls:\n'
                    f'        _u = _u.strip()\n'
                    f'        if not _u:\n'
                    f'            continue\n'
                    f'        _parsed = _urlparse(_u)\n'
                    f'        _path = _parsed.path.rstrip("/")\n'
                    f'        _segs = [s for s in _path.split("/") if s]\n'
                    f'        _depth = len(_segs)\n'
                    f'        # Reject: root URLs (homepage)\n'
                    f'        if _depth == 0:\n'
                    f'            _rejected.append(_u); continue\n'
                    f'        # Reject: known nav/utility single-segment paths\n'
                    f'        if _depth == 1 and _segs[0].lower() in _NAV:\n'
                    f'            _rejected.append(_u); continue\n'
                    f'        # Reject: paths ending in common nav keywords\n'
                    f'        if _segs[-1].lower() in _NAV:\n'
                    f'            _rejected.append(_u); continue\n'
                    f'        # Reject: query-only or fragment-only URLs with no useful path\n'
                    f'        if _depth <= 1 and not _parsed.query and _segs and _segs[0].lower() in _NAV:\n'
                    f'            _rejected.append(_u); continue\n'
                    f'        # Accept: 3+ segment paths (user/type/id pattern — detail pages on content platforms)\n'
                    f'        if _depth >= 3:\n'
                    f'            _accepted.append(_u); continue\n'
                    f'        # Accept: known detail-page path keywords\n'
                    f'        _DETAIL = {{"pen","post","article","item","product","repo","project",\n'
                    f'                   "snippet","gist","video","watch","story","entry","question"}}\n'
                    f'        if any(s.lower() in _DETAIL for s in _segs):\n'
                    f'            _accepted.append(_u); continue\n'
                    f'        # Accept: paths with numeric ID segments\n'
                    f'        if any(_re.match(r"\\d{{4,}}", s) for s in _segs):\n'
                    f'            _accepted.append(_u); continue\n'
                    f'        _rejected.append(_u)\n'
                    f'    with open(_os.path.join(_ctx, "urls.txt"), "a", encoding="utf-8") as _uf:\n'
                    f'        for _u in _accepted:\n'
                    f'            _uf.write(_u + "\\n")\n'
                    f'    if _rejected:\n'
                    f'        print(f"⚠️ Rejected {{len(_rejected)}} non-detail URLs: {{_rejected[:3]}}")\n'
                    f'    print(f"Reported {{len(_accepted)}} target URLs ({{len(_rejected)}} rejected)")\n'
                    f'\n'
                    f'def save_file(url, description=""):\n'
                    f'    """Download and save a file (PDF, image, dataset, etc.) as an artifact.\n'
                    f'    The file is saved to the artifacts directory and tracked in the pipeline.\n'
                    f'    Args: url (str), description (str) - what this file is."""\n'
                    f'    import urllib.request, hashlib\n'
                    f'    _fname = url.split("/")[-1].split("?")[0]\n'
                    f'    if not _fname or len(_fname) > 200:\n'
                    f'        _fname = "file_" + hashlib.md5(url.encode()).hexdigest()[:8]\n'
                    f'    _fpath = _os.path.join(_artifacts_files, _fname)\n'
                    f'    urllib.request.urlretrieve(url, _fpath)\n'
                    f'    _size = _os.path.getsize(_fpath)\n'
                    f'    _ext = _os.path.splitext(_fname)[1].lower()\n'
                    f'    _type_map = {{".pdf":"pdf",".png":"image",".jpg":"image",".jpeg":"image",".gif":"image",".csv":"csv",".json":"json",".zip":"archive",".xlsx":"excel"}}\n'
                    f'    with open(_os.path.join(_ctx, "files.jsonl"), "a", encoding="utf-8") as _ff:\n'
                    f'        _ff.write(_json.dumps({{"url":url,"filename":_fname,"size":_size,"type":_type_map.get(_ext,"file"),"description":description}}, ensure_ascii=False) + "\\n")\n'
                    f'    print(f"Saved file: {{_fname}} ({{_size}} bytes)")\n'
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
                    # Track report_urls output as seen URLs
                    self._seen_urls.update(discovered)
                    logger.info(f"execute_code side-channel: {len(discovered)} URLs reported")

            # Read files side-channel (file metadata written by save_file())
            files_file = os.path.join(ctx_dir, "files.jsonl")
            if os.path.exists(files_file):
                file_metas = []
                with open(files_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                file_metas.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                if file_metas:
                    result["_files"] = file_metas
                    logger.info(f"execute_code side-channel: {len(file_metas)} files saved")

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
        """Tool: save extracted data to artifacts directory."""
        if self._artifacts:
            return self._artifacts.save_export(data, format)
        # Fallback if artifacts not initialized
        import os
        from ..tools.storage import DataExport
        exporter = DataExport()
        os.makedirs("./artifacts/data", exist_ok=True)
        filepath = f"./artifacts/data/extracted_data.{format}"
        if format == "csv":
            exporter.to_csv(data, filepath)
        else:
            exporter.to_json(data, filepath)
        return {"saved": len(data), "path": filepath, "format": format}

    async def _download_file_tool(self, url: str, filename: str = "", description: str = "") -> dict:
        """Tool: download a file to artifacts. Validates URL against seen sources."""
        from urllib.parse import urlparse

        # Gate: validate URL was seen on a page the agent actually visited
        parsed = urlparse(url)
        url_domain = parsed.netloc
        # Check if URL (or its base) is in seen_urls, or if URL path segments appear in any seen URL
        url_grounded = False
        if url in self._seen_urls:
            url_grounded = True
        else:
            # Check if the domain matches a visited page
            for seen in self._seen_urls:
                seen_domain = urlparse(seen).netloc
                if seen_domain and seen_domain == url_domain:
                    url_grounded = True
                    break
        if not url_grounded:
            # Also check if URL was found in current page HTML
            try:
                html = await self._get_clean_html()
                path_part = parsed.path.rstrip("/")
                segments = [s for s in path_part.split("/") if len(s) > 3]
                if any(seg in html for seg in segments):
                    url_grounded = True
            except Exception:
                pass

        if not url_grounded:
            logger.warning(f"download_file URL not grounded in any visited page: {url}")
            return {
                "success": False,
                "error": f"URL not found in any visited page. This URL may be hallucinated. "
                         f"First navigate to a page containing this URL, or use click_download to download via browser interaction."
            }

        from ..tools.downloader import FileDownloader
        dl_dir = str(self._artifacts.files_dir) if self._artifacts else "./artifacts/files"
        os.makedirs(dl_dir, exist_ok=True)
        downloader = FileDownloader(download_dir=dl_dir)
        path = parsed.path
        ext = path.rsplit(".", 1)[-1] if "." in path else "bin"
        result = await downloader.download(url, file_type=ext, filename=filename or None)
        # Track in artifact manager
        if result.get("success") and self._artifacts:
            self._artifacts.add_file({
                "url": url,
                "filename": result.get("name", ""),
                "size": result.get("size", 0),
                "type": ext,
                "description": description,
            })
        return result

    async def _click_download_tool(self, selector: str, description: str = "") -> dict:
        """Tool: click an element and capture the browser-triggered download."""
        if not self._browser:
            return {"success": False, "error": "Browser not available"}

        result = await self._browser.click_download(selector)
        if not result.get("success"):
            return result

        # Save the captured download to artifacts
        download = result.pop("_download", None)
        if not download:
            return {"success": False, "error": "Download object not captured"}

        dl_dir = str(self._artifacts.files_dir) if self._artifacts else "./artifacts/files"
        os.makedirs(dl_dir, exist_ok=True)

        suggested = download.suggested_filename or "download"
        save_path = os.path.join(dl_dir, suggested)
        await download.save_as(save_path)

        size = os.path.getsize(save_path) if os.path.exists(save_path) else 0
        ext = suggested.rsplit(".", 1)[-1] if "." in suggested else "bin"

        # Track in artifact manager
        if self._artifacts:
            self._artifacts.add_file({
                "url": result.get("url", ""),
                "filename": suggested,
                "size": size,
                "type": ext,
                "description": description,
            })

        return {
            "success": True,
            "filename": suggested,
            "size": size,
            "type": ext,
        }

    async def _inspect_file_tool(self, filename: str) -> dict:
        """Tool: inspect a downloaded file and return metadata."""
        if self._artifacts:
            return self._artifacts.inspect_file(filename)
        return {"error": "Artifacts not initialized"}

    async def _download_url_tool(self, url: str) -> dict:
        """Tool: download a file via the authenticated browser, track it in artifacts."""
        save_dir = str(self._artifacts.files_dir) if self._artifacts else "./artifacts/files"
        result = await self._browser.download_url(url, save_dir=save_dir)
        if "error" not in result and self._artifacts:
            self._artifacts.add_file({
                "filename": result.get("filename", ""),
                "size": result.get("size_bytes", 0),
                "type": "zip" if result.get("filename", "").endswith(".zip") else "file",
                "url": url,
                "description": "Downloaded via download_url",
            })
        return result

    async def _js_extract_save(self, script: str) -> dict:
        """Evaluate JS, save result as records, return only a summary — large data never enters LLM context."""
        import uuid
        result = await self._browser.evaluate(script)
        if result is None:
            return {"error": "JS returned null/undefined"}

        records = result if isinstance(result, list) else [result]
        # Build summary: replace long string values with char-count placeholders
        SUMMARY_THRESH = 500
        summary_records = []
        for rec in records:
            if isinstance(rec, dict):
                summary_records.append({
                    k: (f"({len(v)} chars)" if isinstance(v, str) and len(v) > SUMMARY_THRESH else v)
                    for k, v in rec.items()
                })
            else:
                summary_records.append(rec)

        if self._artifacts:
            self._artifacts.add_records(records)
            self._artifacts.write_manifest()
        else:
            logger.warning("js_extract_save: artifacts not initialized, records not persisted")

        saved = len(records)
        logger.info(f"js_extract_save: saved {saved} record(s) directly from JS result")
        return {
            "saved": saved,
            "summary": summary_records,
            "_records": records,  # side-channel for controller._collect_side_channel
            "note": "Full data saved to records pipeline. LLM context shows summary only.",
        }

    async def cleanup(self) -> None:
        """Release resources."""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
