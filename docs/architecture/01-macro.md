# 宏观架构文档 — LLM-as-Controller 五层治理

## 1. 设计哲学

### 1.1 核心转变

**旧模式 (Pipeline-as-Controller)**:
- 固定流水线: Sense → Plan → Act → Verify → Judge → Reflect
- LLM 是子程序: 每个 Agent 调一次 LLM，拿到结果，传给下个 Agent
- 流水线就是硬约束: 即使 LLM 知道该直接用 Script 提取，也必须先经过 Plan 生成 CSS

**新模式 (LLM-as-Controller)**:
- LLM 是决策中枢: 看到当前状态 → 选择工具 → 执行 → 观察结果 → 循环
- 代码提供能力: 工具(browser/extract/analyze)和治理(预算/循环检测/强制停止)
- 没有固定顺序: LLM 可以先分析、再直接用 script 提取、跳过 CSS、自己验证

### 1.2 灵感来源

| 项目 | 模式 | 关键洞察 |
|------|------|----------|
| OpenHands CodeActAgent | while loop + bash/python/browser tools | SWE-bench 77.6%, 单循环, LLM 完全自主 |
| browser-use Agent | step() = observe→decide→execute→post | 隐藏的管理层: MessageManager 做上下文压缩 |
| ScrapeGraphAI | 用户选模式 (SmartScraper vs ScriptCreator) | 不做自动策略选择，让使用者决定 |
| Crawl4AI | 插件式策略 (LLM/CSS/Cosine) | 不做 waterfall，让使用者配置 |
| **Anthropic "Building Effective Agents"** | ACI (Agent-Computer Interface) | 工具设计投入 ≥ HCI 投入；Poka-yoke 防呆设计 |
| **HuggingFace smolagents** | Code Agent (代码 > JSON 工具调用) | LLM 写代码比 JSON tool call 更灵活；planning_interval 周期性重规划 |

**我们的区别**: 在 LLM-as-Controller 之上保留五层治理架构，解决 "LLM 需要管理" 的现实问题。

### 1.2b 能力 vs 自主度 — ACI 设计哲学

**核心矛盾**: 给 agent 更多能力（工具）→ 需要更多规则约束用法 → 约束限制了自主度。

**解决方案** (来自 Anthropic + smolagents 的共识):

1. **能力来自好的工具设计 (ACI)，不是 prompt 规则**
   - 工具描述足够清晰，LLM 自然会选对用法
   - 错误信息包含自纠正提示（而非只说"参数错"）
   - 工具应当 Poka-yoke（防呆）：让错误用法难以发生

2. **自主度来自不限制工具使用顺序**
   - System prompt 不写 "1. navigate 2. analyze 3. extract" 这种硬编码流程
   - LLM 根据观察到的环境自行决定下一步
   - 每个网站结构不同，固定流程必然失败

3. **execute_code 是终极后备 (CodeAct 思想)**
   - 当专用工具无法解决问题时，LLM 可以写代码自己解决
   - 这保证了能力的下限：只要问题可编程解决，agent 就能处理
   - 专用工具是效率优化，不是能力边界

4. **治理通过观察和建议，不通过限制**
   - Governor 观察行为模式（循环、预算耗尽、连续失败）
   - 通过 nudge 提醒 LLM（"你已经连续失败 3 次了，换个方法试试"）
   - 只在极端情况强制停止（超时、超预算）

### 1.3 设计原则

**继承自原架构 (不变)**:

1. **契约驱动 (Spec-Driven)**: 所有行为基于 Spec 定义。Spec 冻结后不可修改，是唯一的任务真相来源。
2. **证据验证 (Evidence-Based)**: 完成 = 证据满足门禁条件。不依赖主观判断，每个阶段输出结构化证据。
3. **分层制衡 (Layered Governance)**: 每层有明确的 "做什么" 和 "不做什么":
   - 战略层: 定义边界，**不**参与执行
   - 管理层: 调度治理，**不**生成代码
   - 验证层: 独立检查，**不**修改规则
   - 执行层: LLM 自主决策，**不**定义约束
   - 工具层: 提供原子能力，**不**参与决策
4. **上下文管理 (Context Management)**: 有界上下文，长期任务自动压缩，关键阶段状态快照
5. **风险控制 (Risk Control)**: 门禁检查、风险阈值、强制回滚点、实时预警

**新增原则**:

6. **LLM 智能最大化**: 不通过 lossy 通道压缩 LLM 输出（如 CSS selector enum），让 LLM 输出完整代码或工具调用
7. **治理不是限制，是保护**: Governor 不干预 LLM 的策略选择，只在异常时介入
8. **工具是能力，不是约束**: 工具 schema 告诉 LLM "你能做什么"，但不告诉它 "你必须按什么顺序做"
9. **CodeAct 模式**: LLM 不仅调用预定义工具，还能通过 execute_code 创造新工具。execute_code (尤其 bash) 是 agent 的**根基能力**——其他所有工具本质上都可以用代码实现，它们存在只是为了效率优化。

## 2. 五层架构

```
┌─────────────────────────────────────────────────┐
│ 战略层 (Strategy)                                │
│ • 任务启动时设定目标和约束                       │
│ • Spec (目标), Policy (约束), Gate (完成标准)     │
│ • 不参与每步循环                                 │
└──────────────────────┬──────────────────────────┘
                       │ task spec + policies
┌──────────────────────▼──────────────────────────┐
│ 管理层 (Management)                              │
│ • 每步都参与，是 LLM 的 "管家"                   │
│ • Orchestrator: 接受任务, 创建 Controller, 调度  │
│ •   full_site: 先 ExplorationController 再提取   │
│ • ContextManager: 构建每步 LLM 的 messages       │
│ • Governor: 预算/循环/风险, 注入治理 nudges       │
│ • Scheduler: 多页队列, 并发控制                   │
│ • State: 全局状态, 断点恢复                       │
│                                                   │
│   ┌───────────────────────────────────────────┐   │
│   │ 执行层 (Execution)                        │   │
│   │ • CrawlController: THE loop               │   │
│   │   while not done:                         │   │
│   │     context = management.build_context()  │   │
│   │     action = LLM(context + tools)         │   │
│   │     result = execute(action)              │   │
│   │     management.governance_check()         │   │
│   └───────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────┘
                       │ tool calls
┌──────────────────────▼──────────────────────────┐
│ 验证层 (Verification)                            │
│ • 可由 LLM 主动调用，也可由 Governor 强制调用    │
│ • verify_quality(): 数据质量评分 + 完成度检查     │
│ • collect_evidence(): 证据收集                    │
│ • check_risk(): 风险检查                          │
└──────────────────────┬──────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────┐
│ 工具层 (Tools)                                   │
│ • 原子能力，LLM 通过 function calling 调用        │
│                                                   │
│ 根基: execute_code (bash/python/js)               │
│   — Agent 的基本能力，可自我扩展                  │
│                                                   │
│ 便利工具 (高频操作的效率封装):                     │
│ • Browser: navigate, go_back, click, fill,        │
│            select_option, press_key, scroll,       │
│            get_html, get_text, screenshot,         │
│            evaluate_js                             │
│ • Extraction: extract_css, intercept_api          │
│ • Analysis: analyze_page, analyze_links,          │
│             search_page                            │
│ • Verification: verify_quality                    │
│ • Storage: save_data, download_file               │
│ • LLM: generate/chat (内部用, 不直接暴露)         │
└─────────────────────────────────────────────────┘
```

## 3. 关键数据流

### 3.1 单页爬取流程

```
用户: "爬取 https://example.com 的产品列表"
  │
  ▼
战略层: SpecInferrer 推断 CrawlSpec (理解需求 → understanding + success_criteria + exploration_hints)
  │
  ▼
管理层.Orchestrator: 创建 CrawlController(task=spec)
  │
  ▼
执行层.CrawlController 主循环:
  │
  │  Step 1: LLM 决定先导航
  │    → tools.navigate("https://example.com") → html
  │
  │  Step 2: LLM 决定分析页面
  │    → tools.analyze_page(html) → {type: list, has_pagination, spa: false}
  │
  │  Step 3: LLM 根据分析结果决定用 script 提取
  │    → tools.execute_code("from bs4 import BeautifulSoup; ...") → [{name, price, url}, ...]
  │
  │  Step 4: LLM 决定验证数据质量
  │    → tools.verify_quality(data, spec) → {score: 0.92, issues: []}
  │
  │  Step 5: LLM 认为完成，返回结果
  │    → "提取了 25 条产品数据，质量分 0.92"
  │
  ▼
管理层.Orchestrator: 收集结果, 返回给用户
```

### 3.2 多页/全站流程 (两阶段 Controller)

全站模式分两个阶段，都用相同的 CrawlController 机制，只是任务不同：

```
Orchestrator
  │
  │  ═══════════ 阶段一: 探索 ═══════════
  │
  ├─ 创建 ExplorationController(task="探索站点结构")
  │     │
  │     │  这是一个普通的 CrawlController，但 system prompt 不同:
  │     │  "你的任务是探索这个网站的结构，发现所有与目标相关的页面。"
  │     │
  │     │  Step 1: navigate(start_url)
  │     │  Step 2: analyze_page() → 理解首页结构
  │     │  Step 3: discover_sitemap() → 检查 sitemap.xml
  │     │  Step 4: analyze_links() → 分类首页链接
  │     │  Step 5: navigate(list_page_url) → 探索列表页
  │     │  Step 6: analyze_links() → 发现更多页面
  │     │  ...直到 LLM 认为探索充分
  │     │
  │     └─ 输出: SiteMap = {
  │            "pages": [
  │              {"url": "...", "type": "list", "priority": 1, "description": "产品列表页"},
  │              {"url": "...", "type": "detail", "priority": 2, "description": "产品详情"},
  │              ...
  │            ],
  │            "site_structure": "...",  // LLM 对站点结构的理解
  │            "crawl_plan": "...",      // LLM 建议的爬取顺序和策略
  │          }
  │
  │  ═══════════ 阶段二: 提取 ═══════════
  │
  ├─ Scheduler: 用 SiteMap 初始化 CrawlFrontier (按 priority 排序)
  │
  ├─ while frontier.has_next():
  │     url = frontier.pop()
  │     controller = CrawlController(
  │       task=spec, url=url,
  │       site_context=site_map.site_structure  // 探索阶段的知识传递给提取阶段
  │     )
  │     result = controller.run()
  │     state.save_progress(url, result)
  │
  └─ 编译所有页面结果 → 返回
```

**为什么分两阶段**:
1. 探索阶段 LLM 的任务是 "理解站点"，不需要提取数据，上下文更聚焦
2. 探索产出的 site_structure 是全局知识，传递给每个提取 Controller，避免重复分析
3. 探索 Controller 可以跨页面导航，而提取 Controller 专注单页/少量页面
4. 如果探索发现反爬严重，可以在提取前调整策略（而不是提取失败才知道）

**单页/多页模式**: 不需要探索阶段，直接创建提取 Controller。

## 4. 层间接口契约

### 战略层 → 管理层
```python
# CrawlSpec — Agent 的最高意志
# 核心是自然语言，结构化字段只是辅助，可以为空
# 用户的需求和网站的实际数据形态可能完全不同
# 例: 用户要 "HTML格式PPT"，网站里可能是 HTML+CSS打包的ZIP
# 所以 spec 不能硬性约束数据格式——LLM 需要自由发现和适应

CrawlSpec = {
    "start_url": str,
    "requirement": str,           # 用户原始自然语言需求 (唯一必填)
    
    # --- 以下全部由 SpecInferrer 推断，都是软约束 ---
    "understanding": str,         # LLM 对需求的理解 (自然语言)
    "success_criteria": str,      # 什么算成功 (自然语言)
    "exploration_hints": str,     # 给探索阶段的建议 (自然语言)
    
    # target_fields 是可选的，仅当需求明确涉及结构化字段时才有
    # 很多任务没有明确字段 (如 "下载所有PPT资源")
    "target_fields": list | None, # [{"name": str, "description": str}] 或 None
    
    "min_items": int,             # 软目标，默认 10
    "quality_threshold": float,   # 软目标，默认 0.7
    
    "policies": {"max_pages": int, "max_depth": int, "rate_limit": float}
}
```

### 管理层 → 执行层
```python
# ContextManager 输出: LLM 看到的消息
Messages = [
    {"role": "system", "content": "You are an autonomous web crawling agent..."},
    {"role": "user", "content": "Task: extract products from example.com..."},
    {"role": "assistant", "content": null, "tool_calls": [...]},
    {"role": "tool", "content": "Navigation successful, HTML: ..."},
    ...
    {"role": "system", "content": "[Governor] Budget: 80% used. Wrap up soon."}  # governance nudge
]
```

### 执行层 → 工具层
```python
# OpenAI function calling 格式
ToolCall = {
    "id": str,
    "type": "function",
    "function": {
        "name": "extract_css",
        "arguments": '{"selectors": {"title": "h2.product-name"}, "container": ".product-card"}'
    }
}

ToolResult = {
    "tool_call_id": str,
    "content": str  # JSON 序列化的结果
}
```

## 5. Docker-as-Body (继承 + 深化)

新架构完全继承旧版 Docker-as-Body 理念，并进一步明确:
- Docker 是 agent 的身体，不是工具
- **Docker 容器本身就是隔离层** — 不需要容器内再套 "sandbox"。旧版的 Sandbox 类/strict_mode 概念已移除。
- **bash 是 agent 的基本能力**: 就像人的手脚不是 "扩展"，是基本能力。Agent 通过 bash 安装包、下载工具、调用 API——这不是"自我扩展"，是基本操作。
- **安全边界在工具注册层**:
  - Docker 内: Orchestrator 注册 execute_code 支持 bash/python/js (全能力)
  - 非 Docker: Orchestrator 注册 execute_code 仅支持 python + 危险模式检查
- `is_docker()` 检测运行环境，自动调整注册的工具能力
- **能力金字塔**: LLM 推理 → execute_code/bash (根基) → 19 个便利工具 (效率优化)

## 6. 系统边界 (继承自原架构)

### 适合的场景
- ✅ 公开网站的结构化数据采集
- ✅ 网站结构频繁变化（LLM 适应力强）
- ✅ 多页面全站自动探索
- ✅ 需要可追溯、可审计的数据采集

### 不适合的场景
- ❌ 需要复杂登录/验证码的网站
- ❌ 实时性要求极高的场景
- ❌ 超大规模采集（LLM 成本高）

### 质量预期

| 场景 | 预期成功率 |
|------|-----------|
| 简单静态页面 | 95%+ |
| 标准列表页 | 85%+ |
| 复杂动态页面 (JS渲染) | 70%+ |
| 强反爬网站 | 50%+ |

### 资源约束
- 单任务执行时间: 30秒 - 10分钟
- LLM 调用次数: 新架构预期 3-8 次/页（旧版 6-8 次/页）
- 内存: 200MB - 1GB
