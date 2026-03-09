# CLAUDE.md — 项目上下文（Claude Code 自动读取）

本文件记录架构决策、开发习惯和易踩的坑，供 Claude Code 开发时参考。

---

## 项目背景

这是一个 LLM-as-Controller 的网络爬虫 agent。核心理念来自 Anthropic 的 agent 设计研究：
- **不是 workflow（预定义代码路径）**，而是 **agent（LLM 动态决定下一步）**
- Agent 被给予**目标**，不是被给予 URL 执行指令

关键参考文档：`docs/agent-design-research.md`（包含 Anthropic、LangChain、Lilian Weng 等顶级工程师的调研结论）

---

## 核心架构决策（不要轻易改）

### 1. Goal-directed vs URL-directed
`context.py` 里的 extractor system prompt 经过多次迭代：
- ❌ 旧设计：`"Target URL: {url}"` + `"Do NOT navigate away"` → agent 是 URL-directed，缺乏智能性
- ✅ 现设计：`"Starting URL: {url}"` + goal-pursuing mission + think() examples → agent 自主决策

### 2. SharedFrontier 是系统核心
`scheduler.py` 里的 `SharedFrontier` 不只是 URL 队列，它是：
- Explorer 和 Extractor 之间的共享状态机
- URL 状态：QUEUED → IN_FLIGHT → EXTRACTED/FAILED/SAMPLED
- `_is_substantive()` 基于 spec.target_fields 过滤空记录（非硬编码）
- `add_batch()` 自动去重，agent 可以大胆上报 URL

### 3. URL 去重在 navigate 层（通用方案）
去重不做在 search_site，而是做在 `navigate` 工具本身：

```python
# orchestrator._navigate_tracking_wrapper
if self._frontier is not None:
    norm = url.split("#")[0].rstrip("/")
    rec = self._frontier._records.get(norm)
    if rec and rec.status in (URLStatus.EXTRACTED, URLStatus.SAMPLED):
        return {"url": url, "skipped": True, "reason": "URL already extracted in this run"}
```

**为什么在 navigate 层而不是 search_site 层？**  
navigate 是所有发现路径的最终入口——不管 URL 是 search_site 找到的、analyze_links 找到的、还是 agent 直接写死的，都必须经过 navigate。所以一个检查覆盖所有路径，通用于任何网站。

**`self._frontier` 怎么传进去的？**  
`_run_full_site()` 创建 frontier 后立即赋值：
```python
frontier = SharedFrontier(max_urls=300, spec=spec)
self._frontier = frontier  # navigate/js_extract_save 通过 self._frontier 访问
```
工具注册（`_build_tools()`）在此之前调用，但 `_navigate_tracking_wrapper` 是 instance method，运行时读 `self._frontier`，所以不需要 holder pattern。

**bypass URL 注册（闭环）：**  
如果 agent 绕过 frontier 直接导航并提取，`_js_extract_save` 在保存后负责把当前 URL 注册到 frontier：
```python
# 如果不是 IN_FLIGHT（说明是 bypass），注册为 EXTRACTED
if existing is None or existing.status != URLStatus.IN_FLIGHT:
    self._frontier.mark_extracted(norm, len(records), new_data=records)
```
这样下次 navigate 到同一个 URL 就会被拦截。

### 4. execute_code 是最强工具
agent 通过 `execute_code` 运行 Python，pre-loaded:
- `page_html`：当前页面 HTML
- `page_url`：当前 URL  
- `save_records(records)`：侧信道写入 records.jsonl
- `report_urls(urls)`：侧信道写入 urls.txt，被 frontier 收集

这个设计绕开了工具 schema 的限制，agent 可以写任意 Python 处理 DOM。

### 5. js_extract_save 是最直接的提取工具
`js_extract_save(script)` 让 agent 用 JS arrow function 直接从 DOM 抓取数据，结果绕过 LLM context 直接写到 records.jsonl（大型字符串不会污染 context）。
- 比 `extract_css` 更灵活（可以写任意 JS 逻辑）
- 比 `execute_code` 更安全（不需要沙箱 Python 进程）
- **返回 summary**（长字段替换为 `(N chars)` 占位符），agent 只看摘要

### 6. 侧信道机制
两个侧信道在 `controller.py` 里收集：
- `_records` 字段在工具返回值里 → 写入 records.jsonl，再被 `frontier.mark_extracted(new_data=...)` 接收
- `report_urls()` 的 output → 收集后 `frontier.add_batch(discovered_by="extractor")`

这意味着 agent 的 URL 上报和数据保存都是异步批量的，不是实时写盘。

---

## 开发习惯

### E2E 测试
**必须用这个命令，不能偷懒：**
```bash
docker compose run --rm dev python -m src.main https://codepen.io \
  --requirement "find threejs pens" \
  --mode full_site \
  --model gemini-2.5-flash
```

- **必须 full_site**：single_page 测不到 frontier 逻辑
- **必须 docker**：camoufox 只在 Docker 里工作
- **推荐 gemini-2.5-flash**：快且够用，调试首选

查看容器日志：
```bash
docker ps --filter "name=full-self-crawl" --format "{{.Names}}: {{.Status}}"
docker logs <name> 2>&1 | Select-Object -Last 60
```

### 单元测试
```bash
python -m pytest tests/unit/ -q
# 100 个测试，0.2s 内完成
# 每次改完代码必须跑，确保不破坏已有逻辑
```

### 改动原则
- **最小化改动**：这个系统每个组件都经过多次迭代，随意改容易引入新问题
- **不要加长 prompt**：Anthropic 的研究结论是工具设计比 prompt 更重要
- **硬约束 > 软约束**：在工具层面做限制（返回值过滤）比在 prompt 里加规则可靠得多

---

## 常见陷阱

### mark_extracted 防覆盖保护
`scheduler.mark_extracted()` 有一个保护：如果 URL 已是 EXTRACTED 且有数据，不能被空更新覆盖：
```python
if rec.status == URLStatus.EXTRACTED and rec.records_count > 0 and records_count == 0:
    return  # bypass 提取已成功，忽略 dispatcher 的空更新
```
背景：当 bypass 提取先于 dispatcher 完成时，dispatcher 最后的 `mark_extracted(url, 0)` 不应该把成功标记为失败。

### fingerprint dedup 仍有硬编码
`scheduler.py` 里 `_ingest_data` 的 fingerprint：
```python
fp = (r.get("title", ""), (r.get("js_code") or "")[:100])
```
用了 `js_code` 字段，是 CodePen 特有的。理想情况应该用 `spec.target_fields` 动态生成，但暂未修改。对非 CodePen 站点，title 相同的记录可能被误判为重复。

### context_filter 是 Gemini 的问题
日志里频繁出现 `finish_reason=content_filter` 是 Gemini 的安全过滤，不是 bug。Agent 会自动重试，通常 3-5 次后继续。

### max_history_steps=3 的含义
`ContextManager(max_history_steps=3)` 表示 agent 只能看到最近 3 步的历史，更早的被压缩。这是为了控制 token 用量。

### _is_substantive() 过滤逻辑
`frontier.mark_extracted()` 接收 `new_data` 后会调用 `_ingest_data()`，其中有 `_is_substantive()` 过滤：只有至少一个 `spec.target_fields` 字段非空的记录才计入 `_all_data`（最终 output.json）。  
records.jsonl 是原始全量，output.json 是过滤后的质量记录。两者行数差异大是正常的。

### Explorer 的 report_urls 时机
Explorer 通过 `report_urls(urls)` 上报发现的内容 URL，这些 URL 进入 frontier 的 QUEUED 状态，等 Phase 2 的提取事件循环处理。**Explorer 本身不提取这些 URL**，只负责发现和上报。例外：Explorer 调用 `mark_sampled()` 对少量样本做验证提取（SAMPLED 状态，Phase 2 跳过）。

### navigate 成功后不代表页面已加载
部分 SPA 在 `networkidle` 后内容还没渲染。`_navigate_tracking_wrapper` 有 SPA 检测：`elem_count > 50 and text_len < 200` 时触发 2s 等待 + 提示 agent 用 `analyze_links()` 读已渲染的 DOM，而不是立刻行动。

---

### 7. RunIntelligence — 运行级知识积累（新增）
`run_intelligence.py` 是跨 agent 会话的持久知识库，存储在 `artifacts/` 下两个 JSON 文件：

- **run_knowledge.json**：所有 agent 读写。含 `site_model`、`proven_scripts`（URL pattern → JS 代码）、`failure_log`、`coverage`。
- **golden_records.json**：Explorer 写入的 1-3 条验证样本，包含推断的 schema（字段类型、平均长度、是否必填）。

**三条决定性改进**：
1. Explorer 结束前 **必须** 写 `site_model`（站点结构、估算总量、提取方式）。Extractor 第一步读它，跳过试错。
2. Explorer 的采样记录不再丢弃，保存为黄金标准。`_js_extract_save` 在保存前做结构验证（非 LLM）：字段是否匹配 schema？长字段是否异常短？关键字段值是否出现在 page_html 中（防幻觉）？
3. CompletionGate 现在感知覆盖率：如果 `estimated_total > 20` 且覆盖率 `< 10%`，即使 min_items 满足也不停止。

**指纹去重修复**（`scheduler.py:355`）：由硬编码 `(title, js_code[:100])` 改为用 `spec.target_fields[:3]` 动态构建，通用于任何网站。

**Agent 工具**：新增 `read_run_knowledge(key?)` 和 `write_run_knowledge(key, value)`，两个阶段均可调用。

---

## 文件地图（改动频率高的）

| 文件 | 作用 | 改动场景 |
|------|------|---------|
| `src/management/orchestrator.py` | 三阶段编排核心 | 调整 Phase 逻辑、工具注册、frontier 集成 |
| `src/management/run_intelligence.py` | 运行级知识积累 | 调整知识传递、验证逻辑、覆盖率计算 |
| `src/management/context.py` | LLM 消息构建 | 调整 agent 的 system prompt 或 task context |
| `src/management/scheduler.py` | frontier 状态机 | 调整 URL 管理、过滤、质量信号 |
| `src/tools/search_tool.py` | domain-locked 搜索 | 调整搜索逻辑、frontier 感知过滤 |
| `src/execution/controller.py` | 单个 agent session | 调整步骤执行、历史管理 |
| `src/management/governor.py` | 循环控制 | 调整 step limit、time limit、完成条件 |

---

## 历史决策记录

### 为什么有 Phase 0？
非 LLM 的预发现（robots.txt + sitemap + search），~3s 完成，给 Explorer 提供结构化的初始 briefing。减少 Explorer 的盲目摸索步骤。

### 为什么 entry_points 不直接进 frontier？
`seed_from_intel()` 只把 `direct_content`（search 验证过的内容页）加入 frontier，不加 `entry_points`（listing 页）。原因：listing 页应该由 Explorer 导航进入并上报其中的内容 URL，而不是直接被当作内容提取。

### 为什么 extractor 的 new_links 被反哺回 frontier？
Goal-directed 架构里，agent 可能在 listing 页发现一批内容 URL。这些 URL 通过 `report_urls()` → 侧信道 → `frontier.add_batch()` 进入队列，让后续 agent 去处理，而不是当前 agent 逐个 navigate（这会造成重复）。

### 为什么用 `think()` 而不是 prompt 规则？
Anthropic tau-bench 研究：think tool 在**收到工具结果之后**调用最有价值（54% 提升），而不是"任务开始前"。`context.py` 里的 think() 规则是 "After receiving important tool results"，和 `<think_example>` 模板一起给 agent 提供推理框架，不是规则约束。
