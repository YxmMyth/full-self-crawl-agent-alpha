# 结构映射功能重设计方案

> **⚠️ 实现状态：仅 Step 1（Extractor pre-navigation）已实施。**
> **Phase 1.5 Listing Sampler、Phase 0 VerifiedListings、report_listing_urls() 均未实现。**

## 背景：上次实现的问题总结

E2E 测试揭示了三个架构级根因，它们是"新功能无法生效"的真正原因，不是 prompt 写法问题：

### 根因一：Extractor 的初始环境状态不确定
- Orchestrator 把 URL 分配给 agent，但不保证 browser 在那个 URL 上
- Agent 的任务描述（`Starting URL: X`）和实际 browser 位置（上一个 session 留下的某个页面）脱节
- 40/53 个 extractor session 在错误页面上执行了 js_extract_save，产生 52% 的工具调用浪费

**这是基础设施问题，不是 agent 的认知问题。**

### 根因二：Explorer 角色超载 → prompt 复杂度触发 content_filter
- 上次实现把三个使命塞进一个 agent：发现内容 URL + 识别站点结构 + 从 listing 页采样
- 复杂 prompt（JSON 格式、LEAF/DIRECTORY/MIXED 分类词、→ 箭头）触发 Gemini content_filter
- 初始 exploration：20 次 content_filter 才通过；re-exploration round 1：50 次从未通过

**单一 agent 承载多个相互竞争的使命，必然导致行为不稳定。**

### 根因三：结构映射结果依赖 agent 自发填充，无保证机制
- StructuralCompletionGate 依赖 structure_map 非空才能激活
- 但 Explorer 在 content_filter 压力下退化回旧行为，structure_map 永远为空
- 新功能整条链路从未闭合

**下游组件依赖上游 agent 的自发输出，没有任何保证机制，这是脆弱的架构。**

---

## 设计原则（从教训中提炼）

1. **Orchestrator 负责环境状态，Agent 只负责认知**
   - 任何 agent session 启动前，orchestrator 必须确保环境（browser 位置）与任务描述一致
   - Agent 不应该需要猜测或修复自己的起始状态

2. **一个 agent，一个清晰使命**
   - Explorer = URL 发现（当前架构，保持不变）
   - 结构发现 = 独立的、有确定性保证的阶段

3. **工具层约束 > Prompt 规则**
   - 硬约束（pre-navigation、hard cap）在工具/orchestrator 层实施
   - Prompt 只描述目标和策略，不承担约束责任

4. **确定性在前，概率性在后**
   - Phase 0（非 LLM）应尽可能多地完成结构发现
   - LLM agent 在确定性信息的基础上运作，减少试错步骤

5. **新功能必须有独立的回退路径，且回退是主动设计的**
   - 不能依赖"agent 失败后自然 fallback 到旧行为"

---

## 新架构设计

### 总览

```
Phase 0（非 LLM，~3s）
  现有：search + sitemap + robots + HEAD probe → SiteIntelligence
  新增：listing page 验证（用 browser 实际加载，而非 HEAD）→ VerifiedListings
          ↓
Phase 1：Explorer（保持原有使命：URL 发现）
  不变：搜索 + 导航 + report_urls()
          ↓
Phase 1.5：Listing Sampler（新 Phase，orchestrator 驱动）
  从 Phase 0 VerifiedListings + Explorer 上报的 listing URL 出发
  每个 listing 页：orchestrator pre-navigate → 启动轻量 Sampler agent → 2-3 条样本
          ↓
Phase 2：Extractor 事件循环（增加 pre-navigation 保证）
  orchestrator 在 agent 启动前先 navigate 到 assigned URL
  Extractor 只处理 content URL（listing URL 已在 Phase 1.5 处理）
```

---

### Phase 0 扩展：VerifiedListings

**现有问题**：Phase 0 的 HEAD probe 对 CodePen 返回 403 Forbidden，导致所有 listing 候选都被丢弃，Explorer 只能从搜索结果起步。

**新增逻辑**：在 Phase 0 末尾，对 entry_points 里的候选 listing URL，用 browser 实际 GET（不只 HEAD），检查是否为真正的 listing 页面（包含多个 item 链接）。这步在 Phase 0 的 browser 初始化之后完成，不引入新的 browser 实例。

```python
# Phase 0 新增输出字段
@dataclass
class SiteIntelligence:
    entry_points: list[ScoredURL]
    direct_content: list[ScoredURL]
    live_endpoints: list[str]
    sitemap_sample: list[str]
    robots_txt: str
    verified_listings: list[VerifiedListing]  # 新增：browser 验证过的 listing 页

@dataclass
class VerifiedListing:
    url: str
    title: str
    item_count_estimate: int          # 从 pagination 读取
    estimation_confidence: str        # "high" | "medium" | "low" | "unknown"
    sample_item_urls: list[str]       # 页面上找到的 2-3 个 item URL
```

**关键约束**：这个验证步骤最多访问 3-5 个候选 URL，超时 5s，失败静默跳过。不能拖慢 Phase 0。

---

### Phase 1.5：Listing Sampler（新 Phase）

这是新功能的核心，独立于 Explorer，独立于 Extractor。

**触发条件**：Phase 1 结束后，如果有 verified_listings 或 Explorer 通过 report_listing_urls() 上报了 listing 类型 URL。

**执行逻辑**（orchestrator 驱动，不是 agent 自发）：

```
for each listing_url in (verified_listings + explorer_reported_listings):
    # Orchestrator 主动 pre-navigate（不是 agent 做的）
    await browser.navigate(listing_url)

    # 启动 Sampler agent（简单使命：采样 2-3 条）
    sampler = CrawlController(role="listing_sampler", max_steps=10)
    result = await sampler.run({url: listing_url, already_navigated: True})

    # Orchestrator 消化结果，写 structure_map
    run_intelligence.add_structure_node({
        url: listing_url,
        sampled: True,
        sample_records: result.data[:3],
        estimated_items: parse_pagination(result),
    })
```

**关键设计点**：
- `already_navigated: True` 告诉 agent "browser 已经在这个页面上，直接开始采样"
- Sampler agent 的使命极其简单：**读 DOM，提取 2-3 条记录，DONE**
- Orchestrator 负责 structure_map 写入，不依赖 agent
- 如果 listing URL 列表为空，整个 Phase 1.5 跳过，无回归

**Sampler agent prompt（极简）**：
```
你在一个 listing 页面上（orchestrator 已经导航好了）。
任务：提取 2-3 条代表性样本记录，然后结束。
1. 用 js_extract_save 提取样本（最多 3 条，系统自动截止）
2. 如果提取成功，说 DONE
3. 如果页面没有目标内容（被 block 等），说 FAILED: 原因
不要继续探索，不要 navigate，不要搜索。
```

这个 prompt 无 JSON 语法、无分类标签、无复杂结构，不会触发 content_filter。

---

### Phase 2 修复：Extractor Pre-Navigation

**修复点**：在 orchestrator 的 `_run_full_site()` 事件循环里，`mark_in_flight` 之后、`CrawlController.run()` 之前，添加一行：

```python
frontier.mark_in_flight(url_record.url)
await self._browser.navigate(url_record.url)  # 新增：保证环境状态
# 然后再启动 extractor agent
```

**效果**：
- Extractor 启动时，browser 保证在 assigned URL 上
- Agent 的 `Starting URL: X` 与实际 browser 状态完全一致
- `proven_scripts` 的"跳过 navigate"行为变为正确行为（而不是 bug）
- `navigate: skipping` 的浪费消失（agent 不再需要重复 navigate）
- 估计消除当前 52% 的工具调用浪费

**代价**：每个 extractor session 增加一次 navigate（~2-3s）。但这一次 navigate 替代了当前平均 ~3-5 次的失败 navigate + go_back 循环，净收益明显。

---

### report_listing_urls() 侧信道

Explorer 当前只有 `report_urls()`（上报 content URL）。新增一个 `report_listing_urls()`（上报 listing URL），进入独立队列，供 Phase 1.5 消费。

```python
# execute_code preamble 里新增
def report_listing_urls(urls):
    """上报 listing/archive/category 页面 URL，供采样阶段使用。
    这些 URL 不会进入 content 提取队列，只用于结构发现和采样。
    """
    ...写入 listing_urls.txt 侧信道...
```

Explorer 的现有 prompt 不改变，只在说明里补充一行：
```
如果你发现了 listing/分类页面（如 /tag/threejs），
用 report_listing_urls([url]) 上报（不是 report_urls）。
```

这行说明用自然语言，无代码密度，不影响 content_filter 触发率。

---

### StructuralCompletionGate 激活路径

现在 structure_map 的写入是 orchestrator 保证的（Phase 1.5），不依赖 agent。

完成条件：
1. **有 structure_map**（Phase 1.5 执行过）：StructuralCompletionGate 检查所有 listing 节点是否已采样
2. **无 structure_map**（Phase 1.5 跳过，无 listing URL 被发现）：CompletionGate 原有逻辑，零回归

---

## 实现顺序（每步独立可测试）

### Step 1：Extractor Pre-Navigation（一行修复，最高优先级）
- 文件：`orchestrator.py`，`_run_full_site()` 事件循环
- 效果：消除 52% 浪费，这是整个系统最大的效率问题
- 测试：E2E 后检查 `navigate: skipping` 数量减少，`records.jsonl` 中 duplicate 消失
- 风险：极低（加一次 navigate，已知页面）

### Step 2：report_listing_urls() 侧信道
- 文件：`orchestrator.py`（execute_code preamble），`controller.py`（side-channel 收集）
- 不改任何 prompt，只增加一个函数定义和侧信道收集
- Explorer 遇到 listing 页时，agent 可以选择调用它（不强制）

### Step 3：Phase 0 VerifiedListings（可选，提升结构发现可靠性）
- 文件：`discovery/engine.py`，`discovery/browser_probe.py`（新）
- 对 entry_points 候选做 browser GET 验证
- 输出：site_intel.verified_listings
- 这步让 Phase 1.5 有更稳定的起点（不依赖 Explorer 上报 listing URL）

### Step 4：Phase 1.5 Listing Sampler orchestration
- 文件：`orchestrator.py`，`context.py`（listing_sampler role）
- Orchestrator 驱动：pre-navigate + 启动 Sampler + 写 structure_map
- `run_intelligence.py` 新增 structure_map 方法（与上次相同，但现在由 orchestrator 保证写入）
- `gate.py` 新增 StructuralCompletionGate（与上次相同）

### Step 5：单元测试
- Step 1: 测试 extractor 开始时 browser 在正确 URL 上（mock browser）
- Step 2: 测试 report_listing_urls 侧信道收集
- Step 3: 测试 VerifiedListings 数据结构和 Phase 0 输出
- Step 4: 测试 Phase 1.5 orchestration 逻辑（mock sampler result）

---

## 关键区别：与上次实现的对比

| 维度 | 上次实现 | 新设计 |
|------|---------|--------|
| 结构发现主体 | Explorer agent（不可靠） | Orchestrator 保证 + Phase 0 辅助 |
| browser 状态保证 | 无（依赖 agent navigate） | Orchestrator 在每次 session 前保证 |
| Agent 使命 | Explorer 同时做结构发现+采样+URL发现 | 每个 agent 只做一件事 |
| content_filter 风险 | 高（复杂 JSON/代码 prompt） | 低（Sampler prompt 极简） |
| 回退路径 | 隐式（agent 失败 → 旧 CompletionGate） | 显式（无 listing URL → 跳过 Phase 1.5） |
| structure_map 写入 | 依赖 agent 自发调用工具 | Orchestrator 保证写入 |
| 实现顺序 | 全部一次性实现 | 分步，每步独立可测 |

---

## 未解决的问题（此方案不处理）

- **content_filter 概率性问题**：Explorer 的 prompt 依然可能偶发 content_filter，但 re-exploration 的 content_filter 循环问题（Round 1 失败）更本质地来自 `max_llm_calls=50` 太低 + 每次 content_filter 都消耗一次 LLM call。可以单独优化：content_filter 时不计入 `_llm_calls`，或 content_filter 后指数退避。这与结构映射功能无关，可独立修复。

- **VoXelo "hot URL" 问题**：已提取的 URL 被 60 次尝试 navigate。根因是 Extractor 在 pre-navigation 后，如果遇到自己的 assigned URL 也被其他 extractor 偶发提取，会看到 `navigate: skipped`。Step 1 的 pre-navigation 修复后，agent 会直接在正确页面上，减少迷失。剩余问题可以通过 task context 增加 `current_url` 字段解决（告诉 agent"browser 已在你的目标页面，无需再 navigate"）。
