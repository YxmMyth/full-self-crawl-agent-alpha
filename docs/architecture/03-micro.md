# 微观架构文档 — 文件级实现细节与代码迁移

## 1. 旧代码 → 新代码映射

### 1.1 直接复用 (原样复制或微调)

#### tools/browser.py ← src/tools/browser.py (630 行)
- **复制整个文件**, 保留 `BrowserTool`, `Browser`, `BrowserTab`, `check_playwright_browsers()`, `with_retry()`
- **修改**: 新增方法 go_back(), select_option(), press_key(), get_text(), search_page()
- **注册到 ToolRegistry 的方法** (在 orchestrator._build_tools() 中):
  ```python
  # browser 是 BrowserTool 实例
  registry.register("navigate", browser.navigate, "Navigate to URL and wait for load", {"url": {"type": "string"}})
  registry.register("go_back", browser.go_back, "Go back to previous page", {})
  registry.register("get_html", browser.get_html, "Get page HTML source", {"selector": {"type": "string", "description": "Optional CSS selector for specific element"}})
  registry.register("get_text", browser.get_text, "Get visible text content (no HTML tags)", {})
  registry.register("click", browser.click, "Click element by CSS selector", {"selector": {"type": "string"}})
  registry.register("fill", browser.fill, "Fill input field", {"selector": {"type": "string"}, "value": {"type": "string"}})
  registry.register("select_option", browser.select_option, "Select dropdown option", {"selector": {"type": "string"}, "value": {"type": "string"}})
  registry.register("press_key", browser.press_key, "Send keyboard event", {"key": {"type": "string", "description": "Key name (Enter, Escape, Control+a)"}})
  registry.register("scroll", browser.scroll, "Scroll page", {"direction": {"type": "string", "enum": ["up", "down"]}, "pages": {"type": "number", "default": 1}})
  registry.register("screenshot", browser.take_screenshot, "Take page screenshot", {"full_page": {"type": "boolean", "default": False}})
  registry.register("evaluate_js", browser.evaluate, "Execute JavaScript in page context", {"script": {"type": "string"}})
  ```

#### tools/parser.py ← src/tools/parser.py (290 行)
- **原样复制**: `HTMLParser`, `SelectorBuilder`
- **无修改**

#### tools/storage.py ← src/tools/storage.py (289 行)
- **原样复制**: `EvidenceStorage`, `DataExport`, `StateStorage`, `ConfigStorage`
- **无修改**

#### tools/downloader.py ← src/tools/downloader.py (318 行)
- **原样复制**: `FileDownloader`, `DownloadManager`
- **无修改**

#### config/ ← src/config/ (全部 3 文件)
- **原样复制**: `loader.py`, `contracts.py`, `validator.py`
- **无修改** (内部 import 路径需检查: `from .xxx` → `from .xxx`)

#### utils/runtime.py ← src/utils/runtime.py
- **原样复制**: `is_docker()`, `get_runtime_info()`
- **无修改**

### 1.2 合并文件

#### tools/llm.py ← 合并 4 个文件

**来源**:
1. `src/tools/llm_client.py` (502 行) — 核心: LLMClient, LLMCache, CachedLLMClient, 异常类
2. `src/tools/api_gateway_client.py` (422 行) — APIGatewayClient, CachedAPIGatewayClient, APIGatewayConfig
3. `src/tools/llm_circuit_breaker.py` (231 行) — CircuitBreaker, CircuitBreakerWrapper
4. `src/tools/multi_llm_client.py` (577 行) — MultiLLMClient (路由+fallback)

**合并方案**:
```python
# tools/llm.py — 结构

# --- 异常类 (从 llm_client.py) ---
class ErrorType(Enum): ...
class LLMError: ...
class LLMException(Exception): ...
class NetworkException(LLMException): ...
class RateLimitException(LLMException): ...
class AuthException(LLMException): ...
class ServerException(LLMException): ...

# --- 核心 LLM 客户端 (从 llm_client.py) ---
class LLMClient:
    """OpenAI-compatible LLM 客户端
    
    关键方法:
    - generate(prompt, system_prompt, max_tokens, temperature) → str
    - chat(messages, tools=None, tool_choice=None) → ChatResponse  # 新增!
    
    chat() 是新架构核心：支持 function calling。
    generate() 保留向后兼容。
    """

# --- 新增: ChatResponse ---
@dataclass
class ChatResponse:
    """LLM chat 响应"""
    content: str | None          # 文本回复 (tool_calls 时可能为 None)
    tool_calls: list[ToolCall]   # function calling 结果
    finish_reason: str           # "stop" | "tool_calls"
    usage: dict                  # {"prompt_tokens": int, "completion_tokens": int}

# --- API Gateway (从 api_gateway_client.py) ---
class APIGatewayConfig: ...
class APIGatewayClient: ...
class CachedAPIGatewayClient: ...

# --- Circuit Breaker (从 llm_circuit_breaker.py) ---
class CircuitBreaker: ...
class CircuitBreakerWrapper: ...

# --- LLM 缓存 (从 llm_client.py) ---
class LLMCache: ...
class CachedLLMClient: ...
```

**关键新增: `LLMClient.chat()` 方法**:
```python
async def chat(self, messages: list[dict], tools: list[dict] = None,
               tool_choice: str = "auto", max_tokens: int = 4096,
               temperature: float = 0.3) -> ChatResponse:
    """
    OpenAI-compatible chat completion with function calling.
    
    这是新架构的核心 LLM 接口。
    
    Args:
        messages: OpenAI 格式的消息列表
        tools: OpenAI function calling 格式的工具列表
        tool_choice: "auto" | "none" | "required"
        
    Returns:
        ChatResponse with content and/or tool_calls
    """
    body = {
        "model": self.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice
    
    resp = await self.client.post(self.api_url, json=body, headers=self._headers())
    data = resp.json()
    
    choice = data["choices"][0]
    message = choice["message"]
    
    tool_calls = []
    if message.get("tool_calls"):
        for tc in message["tool_calls"]:
            tool_calls.append(ToolCall(
                id=tc["id"],
                name=tc["function"]["name"],
                arguments=json.loads(tc["function"]["arguments"])
            ))
    
    return ChatResponse(
        content=message.get("content"),
        tool_calls=tool_calls,
        finish_reason=choice["finish_reason"],
        usage=data.get("usage", {})
    )
```

#### tools/code_runner.py ← src/executors/executor.py (327 行)

**设计变更**: 去掉 `Sandbox` 类和 `strict_mode` 概念。Docker 容器本身就是隔离层。

**提取**: execute/execute_bash/execute_script 的核心逻辑 (subprocess + timeout + output capture)
**删除**: Sandbox 类、strict_mode、validate_code()、CrawlExecutor
**简化为**: 单个 `execute_code(code, language, timeout)` 异步函数

```python
# tools/code_runner.py — 无类，只有函数

async def execute_code(code: str, language: str = "python", 
                       timeout: int = 30) -> dict:
    """Execute code directly. Docker IS the sandbox.
    
    1. Route to interpreter (python/bash/node)
    2. Write to temp file
    3. Run subprocess with timeout
    4. Return {"success": bool, "stdout": str, "stderr": str}
    """
```

**安全边界不在这里，在 Orchestrator 的工具注册层**:
```python
# orchestrator._build_tools() 中:
if is_docker():
    # Docker: 全能力
    registry.register("execute_code", code_runner.execute_code,
        "Execute code. This is the agent's foundational capability.",
        {"code": {"type": "string"}, 
         "language": {"type": "string", "enum": ["python", "javascript", "bash"]}})
else:
    # 非 Docker: 仅 Python + 安全检查
    registry.register("execute_code", code_runner.execute_code_safe,
        "Execute Python code (restricted mode).",
        {"code": {"type": "string"},
         "language": {"type": "string", "enum": ["python"]}})
```

### 1.3 从 Agent 提取为 Tool

#### tools/extraction.py ← 从 agents/act.py + agents/spa_handler.py 提取

**核心函数** (从 ActAgent 方法变为独立函数):

```python
# --- CSS 提取 (从 ActAgent._extract_simple) ---
async def extract_with_css(browser, selectors: dict, container: str = None) -> list[dict]:
    """
    CSS 选择器提取
    
    从 ActAgent._extract_simple() 和 _sanitize_selector() 提取。
    保留完整的选择器清洗逻辑 (@attr, ::attr(x) 语法)。
    保留容器回退逻辑 (容器太宽泛时用首个选择器的父级)。
    保留 HTML 字段检测 (_should_extract_html)。
    """

# --- SPA API 拦截 (从 SPAHandler._intercept_api_responses) ---
async def intercept_api(browser, url_pattern: str = None, 
                        action: str = None, timeout: int = 10) -> list[dict]:
    """
    拦截 SPA 的 API 响应
    
    使用 Playwright page.on('response') 拦截。
    从 SPAHandler 提取，保留:
    - _is_json_content_type() 检测
    - _is_api_url() 过滤
    - _extract_list_from_json() 解包 (data/items/results/list/records)
    
    action 参数: 拦截期间执行的操作 ("scroll"/"wait"/"click:selector")
    """

# --- 辅助函数 (不注册为 tool，供内部使用) ---
def sanitize_selector(selector: str) -> tuple[str, str | None]:
    """从 ActAgent._sanitize_selector() 原样移植"""
    
def extract_list_from_json(obj) -> list[dict] | None:
    """从 SPAHandler._extract_list_from_json() 原样移植"""
```

**注意**: 以下旧功能不再作为独立工具注册，由 LLM 通过 `execute_code` 自主实现:
- `extract_with_llm` → LLM controller 直接在回复中提取，或用 execute_code 调用 LLM API
- `extract_with_pagination` → LLM 自行组合 extract_css + click + scroll
- `extract_from_zip` → 极少使用，LLM 可用 execute_code(bash) 实现

**注册为 Tool**:
```python
registry.register("extract_css", extraction.extract_with_css_tool, 
    "Extract structured data using CSS selectors. Most common extraction method.",
    {
        "selectors": {"type": "object", "description": "field_name → CSS selector mapping"},
        "container": {"type": "string", "description": "Container selector to scope extraction (optional)"}
    })

registry.register("intercept_api", extraction.intercept_api_tool,
    "Intercept SPA/AJAX API responses (JSON). Essential for React/Vue/Angular apps.",
    {
        "url_pattern": {"type": "string", "description": "URL pattern to match (regex)"},
        "action": {"type": "string", "description": "Action during intercept: scroll/wait/click:selector"},
        "timeout": {"type": "integer", "default": 10}
    })
```

#### tools/analysis.py ← 从 agents/sense.py + agents/explore.py + core/smart_router.py 提取

```python
# --- FeatureDetector (从 core/smart_router.py, ~400 行) ---
class FeatureDetector:
    """页面特征检测器 (程序快速分析, <50ms)
    
    原样保留 SmartRouter 中的 FeatureDetector 类。
    删除 SmartRouter (路由决策由 LLM 自己做)。
    删除 ProgressiveExplorer (不再需要)。
    """
    def analyze(self, html: str, url: str = None) -> dict:
        """返回 has_login, has_pagination, is_spa, anti_bot_level, 
        page_type, complexity, container_info"""

# --- 页面分析 tool (融合 FeatureDetector + SenseAgent 部分逻辑) ---
async def analyze_page(html: str = None, browser=None) -> dict:
    """
    分析页面结构和特征
    
    融合:
    - FeatureDetector.analyze() 的程序化分析
    - SenseAgent._extract_features() 的 SPA/分页检测
    
    不包含: SenseAgent 的 LLM 分析 (那由 CrawlController LLM 自己做)
    
    Returns:
        {
            "page_type": "list" | "detail" | "static",
            "has_pagination": bool,
            "is_spa": bool,
            "anti_bot_level": str,
            "complexity": str,
            "container_info": {...},
            "pagination_info": {...},
            "estimated_items": int
        }
    """

# --- 链接分析 tool (从 ExploreAgent) ---
async def analyze_links(html: str, base_url: str, goal: str = "") -> dict:
    """
    分析页面链接，分类和排序
    
    从 ExploreAgent 提取:
    - 链接发现 + 去重
    - 分类 (detail/list/other) — 规则方法
    - 静态资源过滤
    
    不包含: ExploreAgent 的 LLM 排序 (那由 Controller LLM 自己判断)
    
    Returns:
        {
            "links": [{"url": str, "text": str, "category": str}],
            "total": int,
            "by_category": {"detail": int, "list": int, "other": int}
        }
    """

# --- Sitemap 发现 (从 ExploreAgent) ---
async def discover_sitemap(base_url: str) -> dict:
    """
    发现并解析 sitemap.xml
    
    从 ExploreAgent._try_sitemap() 提取。
    """
```

### 1.4 全新实现

#### execution/actions.py — Action 数据类

```python
from dataclasses import dataclass, field
from typing import Any
import json

@dataclass
class ToolCall:
    """一次工具调用"""
    id: str                      # 唯一 ID
    name: str                    # 工具名
    arguments: dict[str, Any]    # 参数
    
@dataclass 
class ToolResult:
    """工具执行结果"""
    tool_call_id: str
    content: str                 # JSON 序列化结果
    success: bool = True
    
@dataclass
class Step:
    """一步完整记录"""
    step_number: int
    tool_call: ToolCall
    result: ToolResult
    timestamp: str               # ISO format
    
@dataclass
class LLMDecision:
    """LLM 的一次决策"""
    content: str | None          # 文本回复
    tool_calls: list[ToolCall]   # 工具调用
    finish_reason: str           # "stop" | "tool_calls"
    usage: dict                  # token 使用量
```

#### execution/history.py — StepHistory

```python
class StepHistory:
    """
    步骤历史记录器
    
    职责:
    1. 记录每步的 ToolCall + Result
    2. 编译最终结果 (从历史中提取数据)
    3. 提供压缩视图 (给 ContextManager 用)
    4. 统计信息 (步数、工具使用频率、错误率)
    """
    
    def __init__(self):
        self.steps: list[Step] = []
        self.task: dict = {}
        self._extracted_data: list[dict] = []  # 从工具结果中累积
    
    def set_task(self, task: dict) -> None: ...
    
    def add_step(self, call: ToolCall, result: ToolResult) -> None:
        """记录一步。如果是提取类工具，累积数据。"""
    
    def to_messages(self, last_n: int = None) -> list[dict]:
        """转换为 OpenAI messages 格式 (assistant + tool 对)"""
    
    def compile_result(self, stop_reason: str = None, 
                       llm_summary: str = None) -> dict:
        """编译最终结果"""
    
    @property
    def step_count(self) -> int: ...
    
    @property
    def tool_usage(self) -> dict[str, int]:
        """工具使用频率统计"""
    
    def last_n_actions(self, n: int) -> list[str]:
        """最近 N 步的 tool name (用于循环检测)"""
```

#### management/state.py ← 合并 3 个文件

```python
# 来源:
# - core/state_manager.py (364 行): StateManager, StateSnapshot
# - core/checkpoint.py (99 行): CrawlCheckpoint  
# - core/progress_tracker.py (177 行): CrawlProgressTracker, PageEvent

class StateManager:
    """全局状态管理 (原样复用 + 合并 checkpoint)
    
    增加:
    - save_checkpoint() 和 load_checkpoint() 从 CrawlCheckpoint 合入
    - record_page_event() 从 CrawlProgressTracker 合入
    """

class CrawlProgressTracker:
    """进度跟踪 (原样复用)"""
```

#### management/governor.py — 全新实现

**核心逻辑来源**:
- `core/meta_controller.py`: EscalationLevel, StrategyAdjustment, 滑动窗口监控
- `core/risk_monitor.py`: RiskLevel, RiskAlert, 阈值检查
- `pipeline.py`: max_retries, 错误计数, best_result 追踪

```python
class Governor:
    def __init__(self, max_steps=30, max_llm_calls=20, max_time_seconds=300,
                 gate=None):
        self.max_steps = max_steps
        self.max_llm_calls = max_llm_calls
        self.max_time_seconds = max_time_seconds
        self.gate = gate
        self._start_time = None
        self._llm_calls = 0
        self._llm_tokens = 0
    
    def should_stop(self, history: StepHistory) -> str | None:
        """
        检查强制停止条件:
        1. 步数超限: history.step_count >= max_steps
        2. LLM 调用超限: _llm_calls >= max_llm_calls
        3. 时间超限: elapsed >= max_time_seconds
        4. 循环检测: 最近 5 步 action name 全相同
        5. CompletionGate 通过 (如果有数据): gate.check(data) → complete
        """
    
    def get_nudges(self, history: StepHistory) -> str | None:
        """
        Generate soft nudges (injected into LLM context):
        - Budget > 60%: "Budget: X% used (N/M LLM calls). Wrap up soon."
        - Consecutive failures > 2: "N consecutive failures. Try a different approach."
        - Enough data collected: "N items collected. You may finish now."
        - Loop detected: "You are repeating the same action. Try something different."
        """
    
    def record_llm_call(self, tokens: int) -> None:
        self._llm_calls += 1
        self._llm_tokens += tokens
```

#### management/context.py — 全新实现

**设计核心**: 控制 LLM 每步看到什么。这是系统中最重要的工程。

```python
SYSTEM_PROMPT_TEMPLATE = """You are an autonomous web crawling agent. Your task is to extract structured data from websites.

## Your Capabilities
You can use the following tools:
{tool_descriptions}

Your foundational ability is execute_code — you can write Python/bash/JS to solve any problem.
The other tools are efficiency shortcuts for common operations.

## Workflow
1. Navigate to the target URL
2. Analyze page structure (use analyze_page for quick insights)
3. Choose extraction strategy (extract_css for structured pages, execute_code for complex cases, intercept_api for SPAs)
4. Extract data
5. Verify quality (use verify_quality)
6. If quality insufficient, try a different strategy
7. When done, respond with "DONE" + data summary

## Constraints
- Max {max_steps} steps
- Prefer execute_code (most flexible), then extract_css (fastest)
- For SPA pages, prefer intercept_api
- On failure, switch strategy — do not repeat the same failing approach

## Current Task
Target URL: {url}
Requirement: {requirement}
Understanding: {understanding}
Success Criteria: {success_criteria}
Target fields: {fields_or_none}
Min items: {min_items}
"""

class ContextManager:
    def build(self, task, history, tools, nudges=None) -> list[dict]:
        messages = []
        
        # 1. System prompt
        messages.append({
            "role": "system",
            "content": self._render_system_prompt(task, tools)
        })
        
        # 2. 历史消息 (最近 N 步完整)
        recent_steps = history.steps[-self.max_history_steps:]
        for step in recent_steps:
            # assistant (tool call)
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": step.tool_call.id,
                    "type": "function",
                    "function": {
                        "name": step.tool_call.name,
                        "arguments": json.dumps(step.tool_call.arguments)
                    }
                }]
            })
            # tool result
            messages.append({
                "role": "tool",
                "tool_call_id": step.tool_call.id,
                "content": self._truncate(step.result.content, max_chars=3000)
            })
        
        # 3. 更早历史的摘要 (如果有)
        if len(history.steps) > self.max_history_steps:
            old_steps = history.steps[:-self.max_history_steps]
            summary = self._summarize_old_steps(old_steps)
            messages.insert(1, {
                "role": "system", 
                "content": f"[History Summary] {summary}"
            })
        
        # 4. 治理注入
        if nudges:
            messages.append({
                "role": "system",
                "content": f"[Governor] {nudges}"
            })
        
        return messages
```

## 2. 关键实现注意事项

### 2.1 LLMClient.chat() 的 API 兼容性

我们的 API Gateway (http://45.78.224.156:3000/v1) 是 OpenAI-compatible。
需要确认:
- ✅ 支持 `tools` 参数 (function calling)
- ✅ 支持 `tool_choice` 参数
- ✅ 响应中有 `message.tool_calls`
- ❓ 需要测试: 不同模型 (claude-opus-4-5, gemini-2.5-flash, gpt-4.1) 是否都支持 function calling

**fallback 策略**: 如果某些模型不支持 function calling，用 JSON mode:
- 在 system prompt 中描述工具
- 让 LLM 返回 JSON: `{"tool": "name", "args": {...}}`
- 在 controller 中解析

### 2.2 extract_css 的选择器清洗

从 ActAgent._sanitize_selector() 必须完整保留:
- `a.card@href` → selector: `a.card`, attr: `href`
- `a::attr(href)` → selector: `a`, attr: `href` (Scrapy 风格)
- 移除残留的 `{}` 字符
- 这些是 LLM 生成 CSS 选择器时的常见错误格式

### 2.3 Docker 模式检测

```python
# 在 Orchestrator.__init__() 中:
from src.utils.runtime import is_docker
self.is_containerized = is_docker()
# 安全边界: 在工具注册时根据环境决定能力范围 (不在执行层)
```

Docker 模式影响 (工具注册层决定):
- Docker: execute_code 支持 bash/python/javascript (全能力，Docker 容器就是隔离层)
- 非 Docker: execute_code 仅支持 python + 危险模式检查

### 2.4 SPA 拦截的 Playwright 集成

从 SPAHandler 提取的 intercept_api 需要 browser.page 对象:
```python
async def intercept_api(browser, url_pattern=None, timeout=10):
    page = browser.page  # Playwright Page 对象
    responses = []
    
    async def on_response(response):
        if _is_json_content_type(response.headers.get('content-type', '')):
            if not url_pattern or re.search(url_pattern, response.url):
                try:
                    body = await response.json()
                    items = _extract_list_from_json(body)
                    if items:
                        responses.extend(items)
                except: pass
    
    page.on('response', on_response)
    # 等待一段时间收集响应
    await page.wait_for_timeout(timeout * 1000)
    page.remove_listener('response', on_response)
    
    return responses
```

### 2.5 Token 估算

ContextManager 需要粗略 token 估算:
```python
def _estimate_tokens(self, text: str) -> int:
    """粗略 token 估算
    中文: ~1.5 tokens per 字
    英文: ~0.3 tokens per word (~1.3 per 4 chars)
    混合: ~1 token per 2-3 chars (保守)
    """
    return len(text) // 3  # 粗略: 3 chars ≈ 1 token
```

## 3. 测试迁移清单

### 可直接迁移的测试 (改 import 路径即可)

| 旧测试 | 新测试 | 测什么 |
|--------|--------|--------|
| tests/unit/test_tools.py | tests/unit/test_tools.py | Browser, Parser, Storage |
| tests/unit/test_crawl_frontier.py | tests/unit/test_scheduler.py | CrawlFrontier → Scheduler |
| tests/unit/test_risk_monitor.py | tests/unit/test_governor.py (部分) | 阈值检查 |
| tests/unit/test_meta_controller.py | tests/unit/test_governor.py (部分) | 滑动窗口 |
| tests/unit/test_act_agent_selector_sanitize.py | tests/unit/test_extraction.py | _sanitize_selector |
| tests/unit/test_act_agent_zip_fallback.py | tests/unit/test_extraction.py | ZIP 回退 |
| tests/unit/test_feature_detector_v2.py | tests/unit/test_analysis.py | FeatureDetector |
| tests/unit/test_spa_handler.py | tests/unit/test_extraction.py | SPA 拦截 |
| tests/unit/test_spec_inferrer.py | tests/unit/test_strategy.py | SpecInferrer |
| tests/integration/test_completion_gate.py | tests/unit/test_strategy.py | CompletionGate |
| tests/integration/test_smart_router.py | tests/unit/test_analysis.py (部分) | FeatureDetector |

### 需要新写的测试

| 测试 | 测什么 |
|------|--------|
| tests/unit/test_registry.py | ToolRegistry: register, schemas(), execute() |
| tests/unit/test_controller.py | CrawlController: mock LLM + mock tools, 验证循环逻辑 |
| tests/unit/test_governor.py | Governor: should_stop, get_nudges, 循环检测 |
| tests/unit/test_context.py | ContextManager: build(), 历史压缩, HTML 截断 |
| tests/unit/test_history.py | StepHistory: add_step, compile_result, to_messages |
| tests/unit/test_actions.py | Action 数据类序列化/反序列化 |
| tests/integration/test_single_page.py | 单页完整流程 (mock LLM) |
| tests/integration/test_multi_page.py | 多页流程 (mock LLM + Scheduler) |

## 4. 配置文件

### config/settings.json — 需要新增字段

```json
{
    "llm": {
        "model": "claude-opus-4-5-20251101",
        "api_key": "sk-...",
        "api_base": "http://45.78.224.156:3000/v1",
        "timeout": 60,
        "max_tokens": 4096,
        "temperature": 0.3
    },
    "controller": {
        "max_steps": 30,
        "max_llm_calls": 20,
        "max_time_seconds": 300,
        "history_window": 3
    },
    "governor": {
        "budget_warning_threshold": 0.6,
        "loop_detection_window": 5,
        "nudge_on_consecutive_failures": 2
    }
}
```

## 5. 实施顺序 (依赖拓扑)

```
P0: scaffold ✅
 │
 ├── P1a: copy tools (browser, parser, storage, downloader) ← 无依赖
 ├── P1b: merge LLM ← 无依赖
 ├── P1c: code_runner ← 无依赖
 ├── P1d: extraction ← 无依赖
 ├── P1e: analysis ← 无依赖
 ├── P1f: registry ← 无依赖
 ├── P2: strategy ← 无依赖
 ├── P3: verification ← 无依赖
 ├── P4a: state ← 无依赖
 └── P4b: scheduler ← 无依赖
      │
      ├── P4c: context ← P1f (需要 registry schemas)
      ├── P4d: governor ← P3 (需要 verification)
      │    │
      │    └── P5a: actions ← P1f
      │         │
      │         └── P5b: history ← P5a
      │              │
      │              └── P5c: controller ← P5b, P4c, P4d, P1b
      │
      └── P4e: orchestrator ← P4c, P4d, P4a, P4b
               │
               └── P6: main ← P5c, P4e, P2
                    │
                    └── P7: tests ← P6
                         │
                         └── P8: docker & docs ← P7
```
