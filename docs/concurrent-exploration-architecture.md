# 并发探索架构设计

> 本文档是 exploration-memory-architecture.md 第三阶段（统一模式重构）的详细设计。
> 基于与用户的深度讨论，记录对 Explorer/Extractor 职责和执行模型的根本性重新定义。

---

## 一、核心认知更新

### 1.1 提取 = 探索工具，不是独立任务

这是整个架构最重要的认知转变。

Explorer 调用 `js_extract_save` 不是"提前做了 Phase 2 的工作"，而是**用提取来感知站点**——就像雷达发射脉冲来探测障碍物。提取到的 1 条数据里，schema、字段值、隐含的 URL 模式都是探索地图上的坐标点。

**现有系统的错误**：Explorer 把提取当作"可选的验证步骤"，目的是确认"这个页面有没有内容"。

**正确的理解**：提取是 Explorer 最主要的感知手段，每次提取之后都应该 think()——从数据里推断下一步去哪。

### 1.2 为什么需要 Extractor 这个独立角色

Explorer 用提取感知，但它的提取是**轻触**——只取 1 条样本，目的是理解格式。

真正的完整提取是**智能密集型任务**：
- 页面结构复杂，需要多次尝试不同的 CSS selector 或 JS 脚本
- SPA 需要等待渲染
- 分页、懒加载、滚动触发
- 不同 URL 同类型页面的细微格式差异
- 提取失败后的调试和重试

Explorer 没有预算为每个 URL 做深度提取——它的步骤要花在站点广度探索上。所以需要 Extractor：一个**专门负责把单个 URL 的数据提干净**的 agent。

**关键区别**：

| | Explorer | Extractor |
|--|--|--|
| 提取目的 | 感知（理解格式） | 收割（获取所有数据） |
| 提取深度 | 1 条样本 | 全量记录 |
| 预算分配 | 花在广度探索 | 全部花在这一个 URL |
| 提取失败时 | 记录，继续探索其他 | 调试，直到成功或超时 |

### 1.3 并发是必要的，不只是效率优化

Explorer 不应该等 Extractor 完成再决定下一步。如果等待，探索速度将由最慢的提取任务决定。

正确的模型：Explorer 发现一个可提取的 URL → **立即 dispatch Extractor（非阻塞）** → 继续探索。两者并发执行。

Extractor 写回全局记忆（proven_scripts、提取 schema），Explorer 周期性读取这些信息，用来调整探索方向。

---

## 二、执行模型

### 2.1 整体流程

```
用户输入: start_url + requirement

Phase 0（非 LLM，~3s）
  search + sitemap + robots → SiteIntelligence
  输出: entry_points, direct_content（浏览器验证过的起点）
       ↓

理解阶段（并发）
  Explorer 主协程:
    持续运行，直到"站点理解完毕"
    每次发现可提取 URL → dispatch Extractor（asyncio.create_task）
    每次 js_extract_save 成功 → think() → 从数据推断下一步
    周期性读全局记忆 → 了解已提取内容，调整探索方向

  Extractor 并发工作者（1-N 个，同时运行）:
    负责单个 URL 的完整提取
    写回全局记忆（proven_scripts、schema、data）
    报告异常（非常规格式、提取失败原因）

  完成条件:
    Explorer 判断: "已探索所有发现的 section，有可用的提取方法"
       ↓

执行阶段（基于理解的批量提取）
  frontier 中所有 QUEUED URL → 并发 Extractor 批量提取
  proven_scripts 优先 hard-replay（0 LLM steps）
  无 proven_scripts 的 URL → LLM Extractor session
       ↓

输出
  站点数据地图: {section → {estimated_items, samples, quality_score}}
  提取结果集: 所有 EXTRACTED URL 的 records
```

### 2.2 Explorer 的内循环

Explorer 的每一步（visit）遵循这个模式：

```
1. navigate(target_url)
2. think(): 这是什么页面？有什么值得做的？
3. 分支决策:
   a. 是 listing/section 入口 → report_sections([...]) + 继续探索子结构
   b. 有可提取内容 → js_extract_save(1条) → think(数据) → dispatch Extractor(url) → 继续
   c. 是空页面/无关 → record_failure + 记录死路
4. 从步骤 2 的 think() 里提取线索:
   - 数据里有作者/标签/关联 URL → 加入探索队列
   - 发现新的 URL 模式 → 更新 site_model
   - 估算 section 的内容规模
```

**关键**：步骤 4 是当前系统完全缺失的。Explorer 提取了 1 条数据，但没有用数据内容来决定下一步。

### 2.3 全局记忆作为协调机制

Explorer 和 Extractor 不直接通信，通过全局记忆协调：

**Explorer 写入:**
- `site_model`: 站点结构、URL 模式、内容分布
- `sections`: 发现的 section 及其估算内容量
- `failure_log`: 死路记录（探索不到的 URL 类型）

**Extractor 写入:**
- `proven_scripts`: 成功提取的 JS 脚本（URL pattern → script）
- `golden_records`: 质量样本（前 3 条成功提取的记录）
- `extraction_schema`: 字段名、类型、填充率

**Explorer 读取（周期性）:**
- 已提取了多少条 → 覆盖率，决定是否继续探索
- proven_scripts 里有没有新脚本 → 哪些 URL 类型已被摸透
- golden_records → 确认提取质量符合 requirement

**Extractor 读取（启动时）:**
- proven_scripts → 是否有可直接使用的脚本
- golden_records → 好数据长什么样（质量对齐）
- site_model → 这个 URL 属于什么类型，预期什么格式

---

## 三、两种提取的详细设计

### 3.1 Explorer 轻提取（sensing）

**目的**：理解这类页面的数据格式，验证可提取性，获取探索线索。

**约束**：
- 只提取 1 条记录
- 失败不重试（记录失败，继续探索其他方向）
- 提取后必须 think()，从数据里提取线索
- 如果发现的是新的可用脚本，写入 proven_scripts

**think() 内容**（Explorer 提取后应该推断的）：
```
这条数据的 schema 是什么？与 requirement 匹配吗？
这条数据里有没有：
  - 作者/用户 → 他们可能有更多类似内容
  - 标签/分类 → 可以去探索对应的 tag/category 页面
  - 关联 URL → 这类内容的聚集处
  - 内容量信号 → 分页标记、"1 of N" 等
```

### 3.2 Extractor 深提取（harvesting）

**目的**：把这个 URL 的所有目标数据提干净。

**起点**：Explorer 已经建立的先验知识：
- proven_scripts（有的话直接用）
- golden_records（知道好数据长什么样）
- site_model（知道这类 URL 的提取方式）

**行为模式**：
```
1. 读 proven_scripts → 有匹配 → 直接 js_extract_save → TASK COMPLETE（3步以内）
2. 无 proven_scripts → 观察页面 → 尝试提取 → 成功 → 写 proven_scripts → TASK COMPLETE
3. 提取失败 → 换方法 → 失败 → 记录 → FAILED
```

**完成条件（明确的）**：
- 成功提取 ≥1 条记录，且记录通过质量检查 → TASK COMPLETE
- 达到步骤上限或超时 → 报告 FAILED + 原因

---

## 四、并发执行的实现

### 4.1 Orchestrator 的变化

```python
# 现有（顺序）:
await _run_explorer()         # 等 Explorer 完成
for url in frontier:          # 然后逐个提取
    await _run_extractor(url)

# 新（并发）:
extractor_tasks = set()

async def _exploration_loop():
    """Explorer 主循环，发现 URL 时立即 dispatch Extractor"""
    # Explorer session 运行
    # report_urls() 触发 Extractor dispatch
    pass

async def _on_url_discovered(url):
    """URL 进入 frontier 时的回调"""
    if should_extract_now(url):  # content URL，非 listing
        task = asyncio.create_task(_run_extractor(url))
        extractor_tasks.add(task)
        task.add_done_callback(extractor_tasks.discard)

# 并发运行 Explorer + 已 dispatch 的 Extractors
await asyncio.gather(
    _exploration_loop(),
    # extractor tasks 通过 create_task 自动并发
)

# 等待所有 Extractor 完成
await asyncio.gather(*extractor_tasks)

# 最终批量提取阶段（frontier 中剩余 QUEUED URL）
await _batch_extraction_phase()
```

### 4.2 并发控制

并发 Extractor 数量需要限制（浏览器是共享资源）：

```python
_extractor_semaphore = asyncio.Semaphore(3)  # 最多 3 个并发 Extractor

async def _run_extractor_bounded(url):
    async with _extractor_semaphore:
        await _run_extractor(url)
```

**浏览器共享问题**：多个 Extractor 不能同时使用一个 browser context。

解决方案：
- **方案 A**：每个 Extractor 用独立 browser tab（playwright 的 new_page()）
- **方案 B**：Extractor 队列化，但 Explorer 和 Extractor 可以交替执行
- **方案 C**（最简单）：Explorer 和 Extractor 共用一个 browser，但 Extractor 在 Explorer 的 step 间隙执行

方案 C 最容易实现：Explorer 每完成一步，检查是否有待执行的 Extractor task，执行它，然后继续探索。

### 4.3 Explorer 读取 Extractor 结果

Explorer 周期性（每 N 步）从全局记忆读一次：

```python
# 在 Explorer 每第 N 步插入
if step_number % READ_INTERVAL == 0:
    extraction_progress = run_intelligence.get_extraction_summary()
    # 注入到 Explorer 的 think() context:
    # "已提取 X 条，covering Y 个 section，proven_scripts 有 Z 个 pattern"
```

---

## 五、Explorer prompt 的根本性重写

这是实现这个架构最关键的改动。

### 5.1 当前 prompt 的核心错误

```
❌ 当前: "Mission: find URLs of pages that contain target data"
❌ 当前: "You MAY extract a 1-record sample to validate content quality"
```

这把 Explorer 定位为 URL 发现机器，提取是可选的副业。

### 5.2 新 prompt 的核心使命

```
✅ 新: "Mission: understand this site well enough that bulk extraction becomes mechanical"
✅ 新: "Extraction is your primary sensing tool — use js_extract_save to understand each section"
✅ 新: "After every successful extraction, think(): what does this data tell you about where to find more?"
```

### 5.3 关键行为要求

**提取后必须推断**（这是当前完全缺失的）：
```
每次 js_extract_save 成功后:
  call think() with:
    - 数据的 schema 是什么？
    - 与 requirement 的匹配程度？
    - 数据里有没有指向更多同类内容的线索？
      (作者、标签、pagination、related URLs)
    - 这个 URL pattern 现在已经被理解了，后续同类 URL 可以直接提取
```

**完成条件（Explorer 自己判断）**：
```
我已经完成了以下工作:
  ✓ 发现了所有可识别的 section（tag页、category页、搜索结果、用户主页等）
  ✓ 每个 section 至少采样了 1 条
  ✓ 有至少 1 个可用的提取脚本记录在 proven_scripts 里
  ✓ 对站点的数据分布有了清晰的认知（哪里多、哪里少、哪里没有）
→ 说 TASK COMPLETE
```

---

## 六、完成条件重新设计

### 6.1 Explorer 完成条件（理解阶段）

不是步数耗尽，不是 URL 数量达标，而是**站点理解完整度**：

```python
def explorer_is_done(run_intelligence) -> bool:
    site_model = run_intelligence.read("site_model")
    if not site_model:
        return False  # 连基本的站点模型都没有

    proven = run_intelligence.read("proven_scripts")
    if not proven:
        return False  # 没有验证过的提取方法

    sections = db.get_all_sections(run_id)
    unsampled = [s for s in sections if not s["sampled"]]
    if unsampled:
        return False  # 还有未采样的 section

    return True  # 理解完整
```

### 6.2 整体 run 的完成条件

```
理解阶段完成: Explorer done = True
        AND
执行阶段完成: frontier 中 QUEUED URL 数量 = 0
        OR
用户需求满足: CompletionGate(all_data, spec) = True
             AND coverage > 10% of estimated_total
```

---

## 七、实施路径（从现有代码出发）

### 现有已完成的基础设施

- [x] DB schema: sections/samples/url_section/sessions 表
- [x] StructuralCompletionGate
- [x] report_sections() side-channel
- [x] Extractor pre-navigation
- [x] proven_scripts + hard-replay
- [x] Post-extraction nudge (Governor)

### Stage 2（下一步，可独立测试）

**重点：让 Explorer 真正用提取感知，并从数据推断方向**

改动：
1. `context.py` Explorer prompt：使命从"发现 URL"改为"理解站点"，提取后 think() 推断作为明确要求
2. `context.py` Extractor prompt：明确"从 proven_scripts 启动，3步以内完成"
3. 维持现有顺序执行模型（不做并发，Stage 3 再改）

预期效果：
- Explorer 开始真正建立站点理解（不只是 URL 列表）
- Extractor 有更强的先验知识起点

### Stage 3（并发模型重构）

**重点：Explorer 和 Extractor 并发执行**

改动：
1. `orchestrator.py`：`asyncio.create_task` dispatch Extractor，Explorer 继续运行
2. Browser tab 隔离（多 page 或交替执行）
3. Explorer 周期读全局记忆，调整探索方向
4. 最终批量提取阶段

### Stage 4（记忆增强）

- Session 轨迹写入 episodic 层（已有 sessions 表）
- Explorer 启动时读历史轨迹（WebCoach 模式）
- Run 间知识持久化（不清空 run_knowledge.json）

---

## 八、关键设计决策（本文档新增）

**决策 9：Explorer 和 Extractor 并发执行**
- 原因：Explorer 不应阻塞在等待提取结果上，站点探索速度不应受提取速度限制
- 实现：asyncio.create_task，Semaphore 控制并发数

**决策 10：Extractor 是独立角色而非 Explorer 的内联步骤**
- 原因：完整提取是智能密集型任务，需要专注的预算和能力
- Explorer 只做轻采样（1 条），Extractor 做完整提取（全量）

**决策 11：提取后必须推断**
- Explorer 每次 js_extract_save 成功后，必须从数据内容推断下一步探索方向
- 这是当前系统最大的缺失，也是从"URL 发现器"到"站点理解器"的核心跃迁

**决策 12：全局记忆是 Explorer 和 Extractor 的唯一协调机制**
- 两者不直接通信
- Explorer 写探索发现，Extractor 写提取经验
- 各自读取对方的输出来指导自己的行为
