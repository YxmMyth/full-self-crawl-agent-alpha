# 中观架构文档 — 模块设计与层间交互

## 1. 模块总览

```
src/
├── strategy/           # 战略层 (3 modules)
│   ├── spec.py         #   SpecLoader + SpecInferrer
│   ├── policy.py       #   PolicyManager
│   └── gate.py         #   CompletionGate + GateDecision
│
├── management/         # 管理层 (5 modules)
│   ├── orchestrator.py #   顶层入口, 任务调度
│   ├── context.py      #   ContextManager: 构建 LLM 消息
│   ├── governor.py     #   Governor: 预算/循环/风险/强制停止
│   ├── state.py        #   StateManager + Checkpoint + ProgressTracker
│   └── scheduler.py    #   CrawlFrontier + 多页并发
│
├── execution/          # 执行层 (3 modules)
│   ├── controller.py   #   CrawlController: LLM 控制循环
│   ├── actions.py      #   Action/ToolCall 数据类
│   └── history.py      #   StepHistory: 步骤记录 + 结果编译
│
├── verification/       # 验证层 (3 modules)
│   ├── verifier.py     #   数据质量验证
│   ├── evidence.py     #   证据收集
│   └── monitor.py      #   风险监控
│
├── tools/              # 工具层 (9 modules)
│   ├── registry.py     #   ToolRegistry: 注册 + schema + 执行
│   ├── browser.py      #   BrowserTool (Playwright)
│   ├── extraction.py   #   CSS/Script/LLM 提取
│   ├── analysis.py     #   页面分析 + 链接分析 + SPA 检测
│   ├── llm.py          #   LLMClient + APIGateway + CircuitBreaker
│   ├── code_runner.py   #   execute_code(): 代码执行 (Docker=全能力, 本地=仅Python)
│   ├── parser.py       #   HTMLParser
│   ├── storage.py      #   EvidenceStorage + DataExport
│   └── downloader.py   #   FileDownloader
│
├── config/             # 配置 (3 modules, 原样复用)
│   ├── loader.py
│   ├── contracts.py
│   └── validator.py
│
└── utils/              # 工具函数 (2 modules)
    ├── runtime.py
    └── logging.py
```

## 2. 各模块详细接口

### 2.0 strategy/spec.py — SpecInferrer ⭐ 核心

**核心职责**: 将用户的自然语言需求 + URL 转化为结构化的提取规范。
这是 Agent 的"最高意志"——所有后续行为（探索、提取、验证）都服务于 Spec。

**Agent 的基本任务**: 根据给到的 URL 站点和自然语言描述的需求，自主探索站点，
获取与需求相关的数据信息，并爬取若干高质量样例（默认 10 个）。

```python
class SpecInferrer:
    """
    需求推断器
    
    输入:
    - url: str          — 目标站点 URL
    - requirement: str  — 用户自然语言需求描述 (唯一必填)
    - page_html?: str   — 首页 HTML (可选, 用于更精准推断)
    
    输出:
    - CrawlSpec: 以自然语言为核心的爬取规范
    
    推断过程 (LLM 驱动):
    1. 理解用户需求: 想要什么？什么形态？
    2. 分析目标站点: 首页结构、可能的数据分布
    3. 生成 CrawlSpec: 自然语言理解 + 软约束
    
    关键: CrawlSpec 以自然语言为主，结构化字段为辅。
    用户的需求和网站的实际数据形态可能完全不同。
    例: 用户要 "HTML格式的PPT" → 网站可能是 HTML+CSS 打包成 ZIP。
    SpecInferrer 需要尽可能理解意图，但不能过度约束数据格式。
    """
    
    def __init__(self, llm_client):
        self.llm = llm_client
    
    async def infer(self, url: str, requirement: str, 
                    page_html: str = None) -> 'CrawlSpec':
        """从自然语言需求推断 CrawlSpec。
        
        LLM 推断:
        - understanding: 对需求的理解 (自然语言)
        - success_criteria: 什么算成功 (自然语言)
        - exploration_hints: 给探索阶段的提示 (自然语言)
        - target_fields: 可能的目标字段 (可选，很多任务没有明确字段)
        """


class CrawlSpec:
    """
    爬取规范 — Agent 的最高意志
    
    核心是自然语言。结构化字段只是辅助，都是软约束。
    
    示例 1 (结构化数据):
    {
        "url": "https://example.com/products",
        "requirement": "获取所有电子产品的名称、价格和评分",
        "understanding": "用户需要电子产品列表数据，包含名称、价格、评分",
        "success_criteria": "获取 10+ 条有效产品数据，包含必要的名称和价格",
        "exploration_hints": "可能需要从分类页进入产品列表",
        "target_fields": [
            {"name": "product_name", "description": "产品名称"},
            {"name": "price", "description": "价格"},
            {"name": "rating", "description": "用户评分"}
        ],
        "min_items": 10,
        "quality_threshold": 0.7
    }
    
    示例 2 (非结构化/文件下载):
    {
        "url": "https://example.com/resources",
        "requirement": "找到并下载所有 HTML 格式的 PPT 演示文稿",
        "understanding": "用户需要 PPT 资源，但格式是 HTML (可能是 HTML+CSS 打包的 ZIP)",
        "success_criteria": "找到并下载 10+ 个可用的 PPT 资源文件",
        "exploration_hints": "可能在资源库/下载页面，文件可能是 ZIP 打包",
        "target_fields": None,  # 没有结构化字段，这是文件下载任务
        "min_items": 10
    }
    """
    url: str
    requirement: str
    understanding: str = ""
    success_criteria: str = ""
    exploration_hints: str = ""
    target_fields: list[dict] | None = None
    min_items: int = 10
    quality_threshold: float = 0.7
```

### 2.0b strategy/policy.py — PolicyManager

**核心职责**: 加载安全和行为策略。约束 Agent 的行为边界。

```python
class PolicyManager:
    """
    策略管理器
    
    从 config/policies.json 加载策略:
    - allowed_domains: 允许访问的域名范围
    - max_depth: 最大爬取深度
    - rate_limit: 请求频率限制
    - excluded_patterns: 排除的 URL 模式
    - respect_robots_txt: 是否遵守 robots.txt
    """
    
    def load(self, config_path: str = None) -> dict:
        """加载策略配置"""
    
    def check(self, action: str, context: dict) -> bool:
        """检查行为是否符合策略"""
```

### 2.0c strategy/gate.py — CompletionGate

**核心职责**: 判断任务是否完成。被 Governor 调用。

```python
class CompletionGate:
    """
    完成门控
    
    基于 CrawlSpec 的 min_items 和 quality_threshold 判断:
    - 数据量是否达标
    - 质量分数是否达标
    """
    
    def check(self, data: list[dict], spec: 'CrawlSpec') -> 'GateDecision':
        """检查是否满足完成条件。"""

class GateDecision:
    met: bool               # 是否达标
    reason: str             # 原因说明
    current_items: int      # 当前数据量
    current_quality: float  # 当前质量分
```

### 2.1 tools/registry.py — ToolRegistry

**核心职责**: 统一管理所有 LLM 可调用工具，生成 OpenAI function calling schema。

```python
class ToolRegistry:
    """
    工具注册表
    
    设计要点:
    - 每个工具注册时提供: name, func, description, parameters (JSON Schema)
    - schemas() 返回 OpenAI tools 格式 (type: "function")
    - execute() 按 name 查找并执行工具, 返回 JSON 字符串
    - 支持同步和异步函数
    - _adapt_arguments(): 智能参数适配层 (ACI 防呆设计):
      * 尝试原始参数调用 → 失败后检查 _raw fallback
      * 支持 flat→nested 参数自动转换 (如 name+type → selector:{name, type})
      * 错误信息包含自纠正提示
    """
    
    def register(self, name: str, func: Callable, description: str,
                 parameters: dict) -> None:
        """注册工具。parameters 遵循 JSON Schema 格式。"""
    
    def schemas(self) -> list[dict]:
        """返回 OpenAI function calling 格式的 tools 列表。"""
    
    async def execute(self, name: str, arguments: dict) -> str:
        """执行工具，返回 JSON 字符串结果。
        
        执行链: arguments → _adapt_arguments() → func(**adapted) → JSON 结果
        异常被捕获并返回 {"error": str, "hint": str} 格式。
        hint 字段帮助 LLM 自纠正参数错误 (Poka-yoke)。
        """
    
    def _adapt_arguments(self, name: str, func: Callable, arguments: dict) -> dict:
        """智能参数适配 (防呆层)
        
        1. 先尝试原始 arguments
        2. 如果 TypeError → 检查 func 是否接受 _raw 参数 → 传 {"_raw": arguments}
        3. 检查 flat→nested 转换 (如 LLM 传 {name: "x"} 但 func 期望 {selector: {name: "x"}})
        4. 返回适配后的 arguments
        """
    
    def list_tools(self) -> list[str]:
        """返回所有已注册工具名。"""
```

### 2.2 execution/controller.py — CrawlController

**核心职责**: THE loop。LLM 决策 + 工具执行的主循环。

```python
class CrawlController:
    """
    LLM-as-Controller 执行循环
    
    依赖:
    - llm_client: 用于决策的 LLM (支持 function calling)
    - tools: ToolRegistry (所有可用工具)
    - governor: Governor (治理检查)
    - context_mgr: ContextManager (构建 messages)
    
    流程:
    1. governor.should_stop() → 是否终止
    2. context.build() → messages (含历史 + 治理注入)
    3. llm.chat(messages, tools) → response
    4. 如果有 tool_calls: 逐个执行, 记入 history
       - extract_css 结果自动追踪到 _collected_data
       - save_data 调用时若无 data 参数，自动注入 _collected_data
    5. 如果 LLM 说完成 (stop + 无 tool_calls): 编译结果返回
    6. 回到 1
    
    数据追踪:
    - _collected_data: list — 累积 extract_css 提取的数据
    - _extract_css_data(): 从工具结果中提取 extract_css 的数据记录
    - 当 save_data 被调用且 data 为空时，自动填充 _collected_data
    """
    
    def __init__(self, llm_client, tools: ToolRegistry, 
                 governor: 'Governor', context_mgr: 'ContextManager'):
        self.llm = llm_client
        self.tools = tools
        self.governor = governor
        self.context = context_mgr
        self.history = StepHistory()
    
    async def run(self, task: dict) -> dict:
        """
        执行爬取任务
        
        Args:
            task: 包含 url, spec, policies 的任务定义
            
        Returns:
            {
                "success": bool,
                "data": list[dict],        # 提取的数据
                "steps": int,              # 总步数
                "stop_reason": str,        # 为什么停止
                "summary": str,            # LLM 的总结
                "new_links": list[str],    # 发现的新链接 (多页模式)
                "metrics": {...}           # 性能指标
            }
        """
    
    async def _execute_step(self) -> bool:
        """执行一步。返回 True 继续，False 结束。"""
```

### 2.3 management/context.py — ContextManager

**核心职责**: 控制 LLM 每步 "看到什么"。这是系统中最关键的工程。

```python
class ContextManager:
    """
    上下文构建器
    
    每步构建 messages 列表:
    1. system prompt: 角色 + 能力 + 规则 (固定, ~800 tokens)
    2. task context: spec + 当前进度 (固定, ~300 tokens)
    3. compressed history: 最近 N 步完整 + 更早摘要 (~2000 tokens)
    4. governance nudges: 预算警告 / 行为修正 (可选, ~100 tokens)
    
    关键技术:
    - 历史压缩: 保留最近 3 步完整，更早的压缩为摘要
    - 工具结果截断: 15000 字符上限 (配合 HTML 清洗，确保 LLM 看到有效内容)
    - System prompt 不包含硬编码工作流 — LLM 自行决定步骤顺序
    """
    
    def __init__(self, max_history_steps: int = 3, max_tokens: int = 6000):
        self.max_history_steps = max_history_steps
        self.max_tokens = max_tokens
    
    def build(self, task: dict, history: 'StepHistory', 
              tools_schema: list[dict], nudges: str | None = None) -> list[dict]:
        """构建完整的 messages 列表。"""
    
    def _system_prompt(self, task: dict) -> dict:
        """构建 system message。
        
        exploration role: 通用探索指引，不硬编码步骤
        extraction role: 通用提取指引，不限制策略选择
        """
    
    def _compress_history(self, history: 'StepHistory') -> list[dict]:
        """压缩历史为 messages。最近 N 步完整 (含 15000 字符截断)，更早的摘要。"""
```

### 2.4 management/governor.py — Governor

**核心职责**: LLM 行为治理。不干预策略选择，只在异常时介入。

```python
class Governor:
    """
    LLM 行为治理器
    
    合并旧代码中的:
    - MetaController 的策略监控逻辑
    - RiskMonitor 的资源/时间监控
    - Pipeline 的 max_retries / 错误计数
    
    治理规则:
    1. 预算: LLM 调用次数 / token 总量不超限
    2. 步数: 总步数不超过 max_steps (默认 30)
    3. 循环: 检测重复 action (同样的工具+参数连续 N 次)
    4. 时间: 总执行时间不超限
    5. 完成: CompletionGate 通过 → 建议停止
    
    介入方式:
    - Nudge (软): 注入 system message 提醒 LLM
    - Force stop (硬): should_stop() 返回原因，循环终止
    """
    
    def __init__(self, max_steps: int = 30, max_llm_calls: int = 20,
                 max_time_seconds: int = 300, gate: 'CompletionGate' = None):
        ...
    
    def should_stop(self, history: 'StepHistory') -> str | None:
        """检查是否应该强制停止。返回原因或 None。"""
    
    def get_nudges(self, history: 'StepHistory') -> str | None:
        """Generate governance nudges. Returns injected text or None.
        
        Examples:
        - "⚠️ Budget: 16/20 LLM calls used (80%). Wrap up soon."
        - "⚠️ Loop detected: extract_css failed 3 times. Try execute_code or different selectors."
        - "✅ min_items=10 reached. You may finish now."
        - "⚠️ Time: 4min/5min elapsed. Prioritize saving current results."
        """
    
    def record_llm_call(self, tokens: int) -> None:
        """记录一次 LLM 调用。"""
    
    def _detect_loop(self, history: 'StepHistory') -> bool:
        """检测循环行为。"""
```

### 2.5 management/orchestrator.py — Orchestrator

**核心职责**: 顶层入口。接收用户任务 → 构建环境 → 调度执行 → 返回结果。

```python
class Orchestrator:
    """
    顶层编排器
    
    旧代码中的 SelfCrawlingAgent.run() 迁移到这里。
    
    职责:
    1. 加载配置 → 初始化 LLM/Browser
    2. 推断 Spec (如果用户只给了 URL)
    3. 注册所有工具到 ToolRegistry
    4. 默认执行 full_site 两阶段流程:
       Phase 1 — Exploration: CrawlController (探索 prompt) → SiteMap
       Phase 2 — Extraction: 对 SiteMap 中每个目标页 → CrawlController (提取 prompt)
    5. 编译最终结果
    
    single_page 模式仅用于测试/调试。
    """
    
    def __init__(self, config_path: str = None):
        ...
    
    async def run(self, start_url: str = None, spec: dict = None) -> dict:
        """主入口。默认 full_site 模式。"""
    
    def _build_tools(self) -> ToolRegistry:
        """注册所有 20 个工具到 registry"""
    
    async def _run_full_site(self, url: str, spec: dict) -> dict:
        """默认模式: 探索 + 提取两阶段
        
        Phase 1 (Exploration):
        - 创建 CrawlController, system prompt 为 "你是站点探索专家..."
        - LLM 自主导航、分析链接、理解站点结构
        - 输出 SiteMap: {pages: [{url, type, priority, notes}], structure: str}
        
        Phase 2 (Extraction):
        - 用 Scheduler 管理 SiteMap 中的目标页列表
        - 对每个目标页创建 CrawlController, system prompt 为 "你是数据提取专家..."
        - site_context (来自探索阶段) 注入上下文
        - 每个 Controller 独立提取, 结果汇总
        """
    
    async def _run_single_page(self, url: str, spec: dict) -> dict:
        """测试模式: 跳过探索，直接提取单页"""
    
    async def cleanup(self):
        """清理资源 (browser)"""
```

### 2.6 management/scheduler.py — Scheduler

```python
class Scheduler:
    """
    多页调度器
    
    封装 CrawlFrontier，为 Orchestrator 提供:
    - 页面队列管理
    - 去重
    - 优先级排序
    - 并发控制 (未来)
    """
    
    def __init__(self, frontier: CrawlFrontier = None, max_concurrent: int = 1):
        ...
    
    def add_urls(self, urls: list[str], depth: int = 0) -> int:
        """添加 URL 到队列，返回实际添加数量。"""
    
    def next_url(self) -> str | None:
        """获取下一个待爬取的 URL。"""
    
    def has_next(self) -> bool:
        """是否还有待爬取的 URL。"""
    
    def mark_done(self, url: str, result: dict) -> None:
        """标记完成。"""
```

### 2.7 verification/verifier.py — Verifier

```python
class Verifier:
    """
    数据质量验证 (作为 Tool 暴露给 LLM)
    
    合并:
    - agents/verify.py 的 _calculate_quality_score() 和 _check_validation_rules()
    - core/verifier.py 的 verify_quality()
    
    作为 tool 注册:
    - name: "verify_quality"
    - description: "验证提取数据的质量。返回评分和问题列表。"
    """
    
    def verify(self, data: list[dict], spec: dict) -> dict:
        """
        验证数据质量
        
        Returns:
            {
                "quality_score": float,   # 0.0 - 1.0
                "valid_items": int,
                "total_items": int,
                "issues": list[str],
                "details": {...}
            }
        """
```

## 3. 工具注册清单 — 按能力域划分

> **根基原则**: Agent 的最小可行能力 = LLM + execute_code(bash)。
> 有了 bash，agent 理论上能完成任何人类开发者在终端能做的事情。
> 其余 19 个工具都是**效率优化**——节省 token、减少步骤、降低错误率。
> 但如果所有工具都失败了，agent 总能退化到 "写代码解决问题" 这个根基。

### 3.0 架构根基 — execute_code

```
能力金字塔:
                    ┌─────────────┐
                    │ Convenience  │  extract_css, analyze_page, ...
                    │   Tools (19) │  省 token、省步骤、降低错误
                    ├─────────────┤
                    │ execute_code │  ← 根基能力
                    │ (bash/py/js) │  Agent 能做任何事
                    ├─────────────┤
                    │  LLM 推理    │  ← 决策中枢
                    └─────────────┘
```

| Tool | 参数 | 定位 |
|------|------|------|
| `execute_code` | `code: str, language: str` | **Agent 的根基能力** — Docker 内可 bash/python/js；非 Docker 仅 python |

**为什么是根基而非扩展**:
- 有 bash + LLM 推理，agent 可以 `pip install` 任何库、`curl` 任何 API、处理任何格式
- 其他所有工具 (navigate, extract_css, analyze_page) 本质上都可以用 bash+python 代码实现
- 区别在于**效率**: `extract_css` 一次调用 < 写 BeautifulSoup 脚本 (省 ~500 tokens + 1 步)
- 所以工具设计的逻辑是: **将最高频操作封装为工具，长尾场景让 agent 自己写代码**

**三种 language 模式**:

| Language | 能力 | 场景 | 环境要求 |
|----------|------|------|---------|
| `bash` | 系统级——安装包、文件操作、API 调用、管道组合 | 安装依赖、下载文件、处理非标准格式 | Docker only |
| `python` | 数据处理——解析、清洗、转换、计算 | HTML 解析、JSON 处理、正则提取 | 所有环境 |
| `javascript` | Node.js 环境 | 复杂 JS 数据转换 | Docker only |

**Docker-as-Body**: Docker 容器是 agent 的身体。bash 不是 "扩展能力"，是 agent 操控自己身体的基本方式——就像人的手脚不是 "扩展"，是基本能力。

### 3.1 Browser 能力域 (11 tools) — "操控浏览器"

**能力边界**: 所有需要 Playwright 的操作。这是 browser-use 等框架已解决的能力域。
我们的实现保持精简——LLM 足够聪明来组合原子操作完成复杂交互。

| Tool | 参数 | 能力 | 边界 (不能做什么) |
|------|------|------|-------------------|
| `navigate` | `url: str` | 导航到 URL，自动等待加载 | 不处理认证/登录 (需 LLM 组合 fill+click) |
| `go_back` | 无 | 浏览器后退 | 仅后退一步，无前进 |
| `get_html` | `selector?: str` | 获取页面/元素 HTML (已清洗) | 自动去除 script/style/svg/noscript/注释；大页面截断至 15000 字符 |
| `get_text` | 无 | 获取可见文本 (去 HTML 标签) | 丢失结构信息；适合快速预览，不适合精确提取 |
| `click` | `selector: str` | 点击元素 | selector 必须唯一匹配；弹窗/overlay 可能遮挡 |
| `fill` | `selector: str, value: str` | 填写输入框 | 只对 `<input>`/`<textarea>` 有效；不处理 `<select>` |
| `select_option` | `selector: str, value: str` | 选择下拉菜单选项 | 仅 `<select>` 元素；自定义下拉需 click 组合 |
| `press_key` | `key: str` | 发送键盘事件 | 单个键或组合键 (Enter, Escape, Control+a) |
| `scroll` | `direction: str, pages: float` | 滚动页面 | 不检测 "已到底部"；无限滚动需 LLM 判断何时停止 |
| `screenshot` | `full_page?: bool` | 截图 (返回 base64) | 可用于多模态 LLM 视觉分析；大页面截图可能很大 |
| `evaluate_js` | `script: str` | 在页面上下文执行 JS | **万能逃生舱**——任何浏览器交互都可通过 JS 实现 |

**不包含的能力 (v1)**: `wait_for` 合并入 navigate 自动等待 + evaluate_js 自定义等待；多标签页管理；文件上传。

### 3.2 Extraction 能力域 (2 tools) — "从页面获取结构化数据"

**能力边界**: 将 HTML/API 响应转化为结构化数据列表。
这是我们的**核心价值**——browser-use 等通用框架不具备的爬取专用能力。

| Tool | 参数 | 能力 | 边界 |
|------|------|------|------|
| `extract_css` | `selectors: dict, container?: str` | CSS 选择器批量提取 | 需要规律的 HTML 结构；动态渲染内容可能不在 DOM |
| `intercept_api` | `url_pattern: str, action?: str, timeout?: int` | 拦截 SPA 的 API 响应 | 仅捕获 fetch/XHR JSON 响应；WebSocket 不支持 |

**设计决策**:
- ❌ 旧版 `run_script` 和 `extract_from_json` 合并到 `execute_code`
- `extract_css` 内含 `_sanitize_selector()` (修复 LLM 生成的 `a@href` 格式)
- `intercept_api` 的 `action` 参数：拦截期间执行的操作 ("scroll"/"wait"/"click:selector")
- LLM 也可以直接在回复中输出提取结果 (无需工具调用，适合非结构化页面)

### 3.3 Analysis 能力域 (3 tools) — "快速理解页面"

**能力边界**: 零 LLM 调用的程序化分析 (<50ms)。给 LLM 结构化的页面理解。
这是 programmatic intelligence 和 LLM intelligence 的最佳结合点——快速的确定性分析辅助 LLM 决策。

| Tool | 参数 | 能力 | 边界 |
|------|------|------|------|
| `analyze_page` | 无 (使用当前页面) | 页面结构分析: 类型(list/detail)、SPA 检测、分页类型、容器结构、反爬标志 | 基于启发式规则，非 100% 准确；复杂 SPA 可能误判 |
| `analyze_links` | 无 (使用当前页面) | 提取+分类页面链接: same-domain 过滤、pagination 检测、sitemap 发现 | 不做链接内容预判；优先级排序需 LLM 补充。注册时过滤未知 kwargs |
| `search_page` | `query: str, regex?: bool` | 在页面文本中搜索内容，返回匹配位置+上下文 | 仅文本搜索，不理解语义；长页面定位特定内容很有用 |

**设计决策**:
- ❌ 旧版 `detect_spa` 合并入 `analyze_page`
- `analyze_page` 无需参数——自动使用当前 browser 页面
- `analyze_links` 注册时对 LLM 传入的未知参数做 filter（而非崩溃），提升 Poka-yoke 防呆性
- `search_page` 参数名为 `query`（非 `pattern`），与函数签名一致

### 3.4 Execution 能力域 — (见 §3.0 架构根基)

`execute_code` 已在 §3.0 详述。此处仅补充实现细节:
- **不再有 Sandbox 类** — Docker 容器本身就是隔离层，不需要容器里再套 "sandbox"
- 实现: `tools/code_runner.py` 中的 `execute_code()` 函数 (非类)
- 核心: 写临时文件 → subprocess → 超时控制 → 捕获 stdout/stderr
- 安全边界在 Orchestrator 的工具注册层:
  - Docker 内: 注册 execute_code 支持 bash/python/js (全能力)
  - 非 Docker: 注册 execute_code 仅支持 python + 危险模式检查
- 预装库: beautifulsoup4, lxml, requests, pandas (可选)
- 超时: python/js 30s, bash 60s

### 3.5 Verification 能力域 (1 tool) — "检查数据质量"

**能力边界**: 对提取的数据做 programmatic 质量评估。
双触发: LLM 可调用 (自检)，Governor 也调用 (治理检查)。

| Tool | 参数 | 能力 | 边界 |
|------|------|------|------|
| `verify_quality` | `data: list[dict], spec?: dict` | 质量评分 + 完成度检查 + 问题清单 | 基于规则 (非空、类型、格式)；不验证语义正确性 |

**设计决策**:
- 🔄 合并旧版 `verify_quality` + `check_completion`
- 返回: `{quality_score, valid_items, total_items, issues, completion_met}`
- spec 可选: 无 spec 时只做基本检查 (非空、类型一致)

### 3.6 Storage 能力域 (2 tools) — "保存结果"

| Tool | 参数 | 能力 | 边界 |
|------|------|------|------|
| `save_data` | `data: list, format?: str` | 保存提取数据 (JSON/CSV) | 追加模式；不去重 (LLM 负责) |
| `download_file` | `url: str, filename?: str` | 下载文件到本地 | HTTP 直接下载；不处理需认证的文件 |

---

### 3.7 工具总表 (20 tools)

| # | Category | Tool | Token Cost (schema) |
|---|----------|------|-------------------|
| 1 | Browser | `navigate` | ~80 |
| 2 | Browser | `go_back` | ~30 |
| 3 | Browser | `get_html` | ~50 |
| 4 | Browser | `get_text` | ~30 |
| 5 | Browser | `click` | ~50 |
| 6 | Browser | `fill` | ~60 |
| 7 | Browser | `select_option` | ~60 |
| 8 | Browser | `press_key` | ~50 |
| 9 | Browser | `scroll` | ~60 |
| 10 | Browser | `screenshot` | ~40 |
| 11 | Browser | `evaluate_js` | ~50 |
| 12 | Extraction | `extract_css` | ~100 |
| 13 | Extraction | `intercept_api` | ~100 |
| 14 | Analysis | `analyze_page` | ~40 |
| 15 | Analysis | `analyze_links` | ~60 |
| 16 | Analysis | `search_page` | ~80 |
| 17 | Execution | `execute_code` | ~80 |
| 18 | Verification | `verify_quality` | ~100 |
| 19 | Storage | `save_data` | ~70 |
| 20 | Storage | `download_file` | ~60 |
| | | **Total** | **~1,250 tokens/step** |

## 4. 模块依赖图

```
config/ ← 所有模块
utils/  ← 所有模块

tools/llm.py       ← (无内部依赖)
tools/browser.py   ← (无内部依赖)
tools/code_runner.py ← (无内部依赖)
tools/parser.py    ← (无内部依赖)
tools/storage.py   ← (无内部依赖)
tools/downloader.py ← (无内部依赖)

tools/extraction.py ← tools/parser.py, tools/browser.py
tools/analysis.py   ← tools/parser.py, tools/browser.py
tools/registry.py   ← (无内部依赖, 接收外部注册)

verification/verifier.py  ← (无内部依赖)
verification/evidence.py  ← tools/storage.py
verification/monitor.py   ← (无内部依赖)

strategy/spec.py   ← tools/llm.py, tools/browser.py
strategy/policy.py ← config/
strategy/gate.py   ← verification/verifier.py

execution/actions.py   ← (纯数据类)
execution/history.py   ← execution/actions.py
execution/controller.py ← execution/history.py, tools/registry.py, 
                          management/context.py, management/governor.py

management/state.py      ← (无内部依赖)
management/scheduler.py  ← (无内部依赖, 内含 CrawlFrontier)
management/governor.py   ← strategy/gate.py, verification/monitor.py
management/context.py    ← execution/history.py
management/orchestrator.py ← 以上所有
```

## 5. 错误处理策略

### 工具层错误
- 每个工具 execute 捕获所有异常，返回 `{"error": str}` 格式
- LLM 看到错误后自行决定：重试？换工具？放弃？

### LLM 调用错误
- LLMClient 内部重试 (3 次, exponential backoff)
- CircuitBreaker 防止级联失败
- 如果 LLM 完全不可用: Governor 强制停止

### 治理层错误
- Governor 异常不传播: 记录日志，返回 None (不阻塞主循环)
- ContextManager 异常: 降级为最小上下文 (只有 system + last step)

### 策略层错误
- SpecInferrer 失败: 使用默认 spec
- PolicyManager 失败: 使用默认策略 (允许一切)
- CompletionGate 失败: 忽略 (继续循环直到其他条件停止)
