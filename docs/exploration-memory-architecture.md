# 探索、长期记忆与数据库架构设计

> 本文档记录对话中形成的完整设计思路，尚未实施。

---

## 一、问题背景与需求重定义

### 现有系统的漂移

`full_site` 模式的设计意图本是探索——Explorer 的存在就是为了理解站点、发现数据在哪里。但随着迭代，系统重心漂移到"尽可能多地抓取"（CompletionGate = 收集 N 条记录），Explorer 沦为 URL 发现器，站点理解这件事从未真正发生过。

现有 `full_site` 模式其实是在**胡乱搜索**：Phase 0 搜索一批 URL，Explorer 再搜索一批，Extractor 逐个去提取。没有对站点结构的真正理解，也没有根据理解调整策略。称之为"full_site"名不副实。

### 真实需求

用户有一个明确的需求：比如"找 threejs pens"。但用户不了解这个站点——不知道数据在哪里、有多少、质量如何。

> 用户希望 agent 能先探索站点，告诉他：这个站点的数据分布在哪里、各个位置的数据量大概有多少、数据的内容质量如何——在此基础上，再有针对性地执行提取。

这不是"探索"和"提取"两个独立任务，而是**一个完整任务的两个阶段**：先理解，再执行。

### 只有一种模式

不应该有 `recon` 和 `full_site` 两个分离的模式。正确的 `full_site` 本来就应该包含对站点的充分理解。新的统一模式：

```
理解阶段：探索站点结构 + 采样各分区 → 建立站点数据地图
执行阶段：根据地图，有针对性地提取目标数据
```

**交付物**：
- 站点数据地图（哪里有什么数据，大概有多少，估算置信度）
- 各分区的 2-3 条样本（数据长什么样，质量如何）
- 基于以上，执行针对性提取得到的结果集

---

## 二、核心架构思路

### 2.1 探索与提取是反馈回路，不是线性阶段

探索发现分区 → 采样验证质量 → 质量信号影响提取优先级 → 提取过程中发现新分区 → 回到探索。

这个回路已经隐含在现有架构里（Extractor 的 `new_links` 回流 frontier），但被"Phase 1 结束后才开始 Phase 2"的线性思维强行打断了。

正确模型：**单一事件循环，exploration 和 extraction 交织进行**，frontier 是唯一的协调信道。

### 2.2 网页没有明确类型，Agent 判断优先于规则路由

Web 上的页面不存在干净的"类型"划分：
- 一个 tag 页既展示 item 列表（listing），也有分类链接（directory）
- 一个 user 页有作品集（content aggregator），也可能有 bio 文章（content）
- 搜索结果页是动态生成的，分类取决于搜索词

因此，**不能通过 if/else 按 URL 类型路由到不同 agent**。正确做法：

1. Agent 访问页面，**自主判断**这个页面是什么性质
2. 根据判断决定：是深入探索子结构、是采样、是提取，还是三者都做
3. Agent 的判断结果写入 DB，影响后续调度优先级

URL 类型（listing/content/directory）只是**初始 hint**（来自 Phase 0 或 Explorer 上报时的标注），不是决定性的路由规则。

### 2.3 发现子结构与采样是两个独立步骤

这两件事的目的不同，时机也可以不同：

| 步骤 | 目的 | 产出 |
|------|------|------|
| **结构发现** | 这个分区下面还有什么子分区？有多少条目？ | 子 URL 列表 + 估算规模 |
| **质量采样** | 这个分区的数据长什么样？值不值得全量抓取？ | 2-3 条样本记录 |

一个分区可能：
- 已被发现（存在于地图），但尚未采样（没拿过样本）
- 已采样，但子结构未完全探索（还有更深的子分区）
- 结构和采样都完成（这个分区理解完毕）

DB schema 中这两个状态必须独立追踪（`structure_explored` 和 `sampled` 是两个不同字段）。

### 2.4 多 Session 架构，共享长期记忆

全局视野不来自一个长 session，而来自**跨 session 的共享 DB**。

每个 session 启动时从 DB 读取全局地图（"目前已知什么"），结束时把发现写回 DB（"我新发现了什么"）。Session 短且聚焦，但多个 session 合起来构建出完整的站点理解。

BFS 优先：先铺开顶层结构，再按质量信号决定深入哪些分支。Agent 可以向 DB 写入优先级信号影响调度顺序。

---

## 三、交叠结构问题与数据库设计

### 3.1 为什么必须用关系型数据

同一个 content URL 可以通过多条路径被发现：
```
/tag/threejs       → pen X
/collection/3d     → pen X
/user/voxelo       → pen X
搜索 "three.js"    → pen X
```

这是**多对多关系数据**。放在 dict 或 JSON 文件里只能知道"这个 URL 被发现过"，无法表达：
- 它属于哪几个分区（影响内容分类判断）
- 哪些分区之间有内容重叠（影响是否值得全量抓取的决策）
- 同一 URL 在不同语境下的质量信号是否一致

### 3.2 现有 HistoryDB 的问题

现有 PostgreSQL DB 只做**结果持久化**：

```sql
runs    -- 每次 run 的元数据
records -- 提取出来的 JSONB 记录
```

探索过程的所有状态（URL 队列、去重、优先级、结构发现、质量信号）全在内存里（SharedFrontier = dict），run 结束消失。DB 和探索逻辑完全脱节，两者是平行的孤岛。

### 3.3 DB 应该成为探索的 Source of Truth

**SharedFrontier 从"源"降级为"缓存"**，DB 才是 source of truth。

需要新增三层：

#### 拓扑层（Topology）

```sql
-- 所有见过的 URL 及其属性
CREATE TABLE urls (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url                 TEXT UNIQUE NOT NULL,
    url_hint            TEXT,         -- agent 上报时的初始 hint（content/listing/unknown）
    first_discovered_at TIMESTAMPTZ DEFAULT NOW(),
    quality_score       FLOAT,        -- 提取质量信号，agent 写入
    embedding           vector(1536)  -- pgvector，内容相似度用
);

-- 被识别为"数据分区"的页面（agent 判断后写入）
CREATE TABLE sections (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url                     TEXT UNIQUE REFERENCES urls(url),
    title                   TEXT,
    agent_classification    TEXT,         -- agent 的判断（listing/directory/mixed/unknown）
    estimated_items         INT,
    estimation_confidence   TEXT,         -- high | medium | low | unknown
    estimation_basis        TEXT,         -- "分页：第1/47页 × 20条/页"
    structure_explored      BOOLEAN DEFAULT FALSE,  -- 子结构是否已发现
    sampled                 BOOLEAN DEFAULT FALSE   -- 是否已采样（独立于结构发现）
);

-- 多对多：content URL 属于哪些 section，通过哪条路径发现
CREATE TABLE url_section (
    url_id          UUID REFERENCES urls(id),
    section_id      UUID REFERENCES sections(id),
    discovery_path  TEXT,       -- 发现这个关系的 session ID
    PRIMARY KEY (url_id, section_id)
);

-- 各 section 的样本记录（agent 采样后写入）
CREATE TABLE samples (
    id          BIGSERIAL PRIMARY KEY,
    section_id  UUID REFERENCES sections(id),
    data        JSONB NOT NULL,
    crawled_at  TIMESTAMPTZ DEFAULT NOW()
);
```

#### 情景层（Episodic）

```sql
-- 每个 agent session 的轨迹记录
CREATE TABLE sessions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              TEXT REFERENCES runs(id),
    role                TEXT,       -- explorer | sampler | extractor
    assigned_url        TEXT,
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    ended_at            TIMESTAMPTZ,
    outcome             TEXT,       -- success | failed | empty | timeout
    steps_taken         INT,
    records_count       INT,
    new_sections_found  INT,        -- 新发现了多少子分区
    trajectory_summary  TEXT        -- 做了什么、发现了什么、失败在哪（文本摘要）
);
```

#### 语义层（Semantic）

通过 `pgvector` 扩展，在 `urls.embedding` 列上做近似最近邻搜索：
- "这个页面和已经提取过的内容相似吗？" → 内容去重
- "哪个分区和用户的 requirement 最匹配？" → 针对性优先调度

---

## 四、长期记忆架构

### 4.1 记忆分类与现状

| 类型 | 含义 | 现状 | 目标位置 |
|------|------|------|---------|
| **Procedural**（程序性） | 怎么做：proven_scripts，JS 提取方法 | ✅ run_knowledge.json | 保留，可逐步迁移到 DB |
| **Semantic**（语义性） | 知道什么：站点结构，分区关系，URL 拓扑 | ⚠️ run_knowledge.json（丢失、无法查询） | DB topology 层 |
| **Episodic**（情景性） | 做过什么：哪个 session 探索了哪里，结果如何 | ❌ 完全没有 | DB episodic 层 |

Episodic 记忆的缺失是最大的问题。每次 run 结束，"这次探索的轨迹"就消失了。下次遇到相似站点，从零开始重复试错。

### 4.2 核心设计原则（来自调研）

**MemGPT/Letta 的分层记忆原则**：
- Agent context window = 工作记忆（有限）
- 长期记忆在外部 DB，agent 通过工具调用按需读取
- Agent 不需要记住所有历史，只需要知道**怎么查**自己需要的东西

→ 应用：Explorer/Sampler 启动时不需要完整地图在 context 里，调用 `query_site_topology(url)` 获取已知信息即可。

**WebCoach 的跨 session 反馈原则**（2025 年研究）：
- 每个 session 结束后记录轨迹摘要
- 下一个相似 session 启动时，检索历史中相似情境的轨迹注入 context
- 实验结果：重复性错误显著减少，长期规划能力提升

→ 应用：上一次探索 `/tag/threejs` 的轨迹（用了哪个 JS script，采样是否成功，遇到什么 block）可以被下一次处理同类分区的 session 读取，避免重复试错。

**Cognee 的记忆进化原则（memify）**：
- 记忆不是静态的
- 每次 run 结束后：合并重复 URL 表示，强化有效路径，弱化/删除死链和空结果

**MAGMA 的多关系原则**（2026 年研究）：
- 纯语义记忆（只有向量相似度）不够
- 需要同时维护语义关系、时序关系、拓扑关系
- 多关系结构在长期任务上显著优于单一语义方法

### 4.3 Agent 访问长期记忆的工具

在现有 `read_run_knowledge` / `write_run_knowledge` 基础上，新增 DB 查询工具：

```python
# Explorer / Sampler 可用
query_site_topology(url)        # 这个页面/分区，DB 里已经有什么记录？
get_unsampled_sections()        # 已发现但尚未采样的分区列表
get_unexplored_sections()       # 已发现但子结构未探索的分区列表
find_similar_sections(url)      # 有没有已处理过的相似分区？（pgvector）

# 所有 agent 可用
get_session_history(url)        # 这个 URL 历史上被谁处理过，结果如何？
```

---

## 五、统一模式的完整流程

```
用户输入：start_url + requirement（如 "find threejs pens on codepen.io"）

Phase 0（非 LLM，~3s，现有 + 扩展）
  现有：search + sitemap + robots → SiteIntelligence
  扩展：对 entry_points 候选做 browser GET 验证（非 HEAD）
        → 初步确认哪些路径存在数据
            ↓
理解阶段（新）
  Explorer session：根据 requirement 和 SiteIntelligence 找入口分区
    → 上报发现的 URL（content/listing hint）到 frontier + DB

  事件循环（agent 判断驱动，非类型 if/else）：
    对 frontier 中每个 URL，派发 session：
      session 访问页面 → 自主判断页面性质
        → 如果是分区入口：发现子结构（写入 DB sections）
        → 如果有可采样内容：采样 2-3 条（写入 DB samples）
        → 如果是内容页：提取（写入 DB records）
        → 新发现的 URL 写回 frontier + DB

  两步完成条件（独立追踪）：
    structure_explored = True → 已知分区的子结构都探索完毕
    sampled = True           → 已知分区都有质量样本
            ↓
执行阶段（基于理解的针对性提取）
  根据 DB 中的质量信号和结构地图：
    - 优先提取质量高的分区的数据
    - 跳过质量低或与 requirement 不匹配的分区
    - Extractor session（现有逻辑 + pre-navigation 修复）
            ↓
输出
  站点数据地图：{section → {classification, estimated_items, samples, quality_score}}
  提取结果集：根据地图针对性提取的记录
```

---

## 六、与现有架构的关系

### 需要重新思考的核心问题

现有架构是**线性的、相互独立的**：
```
Phase 0 → Explorer（一次性）→ [URL 列表] → Extractor × N（各自独立）
```

新架构是**循环的、相互反馈的**：
```
Phase 0 → 理解循环（exploration + sampling 交织）→ 执行循环（informed extraction）
```

这个转变意味着：

| 现有组件 | 现状角色 | 新架构中的角色 |
|---------|---------|--------------|
| Phase 0 | 静态 briefing | 静态 briefing + 初步路径验证 |
| Explorer | 一次性 URL 发现（长 session） | 初始入口发现（短 session，可多次触发） |
| SharedFrontier | 内存 URL 队列，run 内有效 | DB 的写回缓存，跨 run 持久 |
| Extractor | 独立 session，无上下文共享 | 有 DB 支撑的上下文（知道为什么来这个 URL） |
| CompletionGate | count-based（min_items） | structure-based（理解完整度）+ quality-based |
| HistoryDB | 结果持久化孤岛 | 系统核心知识库，所有 agent 读写 |
| run_knowledge.json | 跨 session 知识（轻量） | 保留作为快速访问缓存，DB 是 source of truth |

### 不变的核心机制

- **Frontier 作为协调信道**：仍然是 URL 状态机，只是持久化到 DB
- **Pre-navigation**：已实施，Orchestrator 在每个 session 前保证 browser 状态
- **js_extract_save / proven_scripts**：提取层逻辑不变
- **Phase 0 非 LLM 发现**：保留，扩展验证逻辑
- **content_filter 容错机制**：保留

### 真正需要新建的

1. **DB topology 层**（sections, url_section, samples, sessions 表）
2. **Agent 的 DB 查询工具**（query_site_topology 等）
3. **Session 轨迹记录**（episodic 层写入）
4. **基于 DB 的完成条件判断**（structure_explored + sampled 两个维度）

---

## 七、实施优先级

### 已完成
- [x] Extractor pre-navigation（消除 52% 工具浪费）

### 第一阶段：DB Schema 扩展（地基）
- 扩展 HistoryDB：新增 urls/sections/url_section/samples/sessions 表
- SharedFrontier 保持现有逻辑，新增 DB 写回（双写，逐步迁移）
- 新增 `query_site_topology` 等 agent 工具

### 第二阶段：结构发现与采样分离
- 明确 sections 表中 `structure_explored` 和 `sampled` 两个独立状态
- Session 结束时将发现的分区写入 DB（不依赖 run_knowledge.json）
- 基于 DB 的完成条件：检查两个维度是否都满足

### 第三阶段：统一模式重构
- Explorer session 改为短 session + 多次触发（而非一次性长 session）
- 事件循环改为 agent 判断驱动（session 自主决定做结构发现还是采样还是提取）
- 输出格式扩展：数据地图 + 质量样本 + 提取结果集

### 第四阶段：跨 session 记忆（WebCoach 模式）
- Session 结束时写入 trajectory_summary 到 episodic 层
- Session 启动时检索相似历史轨迹注入 context
- Run 结束后 memify（合并重复节点，强化有效路径）

### 第五阶段：语义层
- pgvector embedding（urls 表）
- 内容相似度去重
- `find_similar_sections` 工具

---

## 八、关键设计决策（已达成共识）

1. **只有一种模式**：正确的 full_site 包含充分的站点理解，recon 不是独立模式
2. **用户有 requirement，但需要先理解站点**：探索服务于需求，不是独立于需求的
3. **网页类型由 Agent 判断**，不是由规则路由，URL hint 只是初始参考
4. **结构发现和质量采样是两个独立步骤**，各自独立追踪完成状态
5. **多 session 架构**，全局视野来自共享 DB，不来自长 session
6. **DB 是 source of truth**，SharedFrontier 降级为 DB 的 write-back 缓存
7. **Orchestrator 负责环境状态**（pre-navigation 已实施）
8. **记忆三层**：procedural（已有）+ semantic（DB topology）+ episodic（DB sessions）
9. **不引入新服务**：PostgreSQL + pgvector，不上 Neo4j 或专用向量 DB
