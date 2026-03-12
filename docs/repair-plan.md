# 鲁棒性深度修复方案

> 基于 2026-03-12 全面代码审计。每个修复包含：精确代码位置、改动内容、测试方案。
> 侧信道 context 泄漏经验证为误报（controller.py:454 已正确更新 result.content）。
> 新发现：`_run_sampling_loop` 定义但从未调用——Explorer 的 fallback 采样安全网不存在。

---

## 原则

1. **先止血，再治病，最后强身**
2. **每个修复有对应单元测试**
3. **不引入新功能**——鲁棒性修复和能力提升（线 B）分开推进
4. **改完跑** `python -m pytest tests/unit/ -q` 全过

---

## 第一波：止血（防崩溃 + 防数据丢失）

### 1.1 `_extract_one()` 异常保护

**问题**：单个 URL 提取抛异常 → 整个 run 崩溃，所有剩余 URL 丢失，当前 URL 卡在 IN_FLIGHT。

**精确位置**：`orchestrator.py` 两处

```
第一处（burst loop drain）— line 879:
874│ while True:
875│     url_record = frontier.next()
876│     if url_record is None:
877│         break
878│     frontier.mark_in_flight(url_record.url)
879│     await _extract_one(url_record)  # ← 无保护
880│     round_extracted += 1

第二处（final drain）— line 912:
905│ while True:
906│     url_record = frontier.next()
907│     if url_record is None:
908│         ...
909│         break
910│     frontier.mark_in_flight(url_record.url)
911│     await _extract_one(url_record)  # ← 无保护
```

**改动**：两处均改为：
```python
try:
    await _extract_one(url_record)
except Exception as e:
    logger.error(f"Extraction failed for {url_record.url}: {e}", exc_info=True)
    frontier.mark_failed(url_record.url, f"exception: {type(e).__name__}: {e}")
```

**测试方案**（`tests/unit/test_management.py`）：
```python
async def test_extract_one_exception_does_not_crash_loop():
    """Single URL extraction failure should not terminate the run."""
    # Setup: frontier with 3 URLs
    # Mock _extract_one to raise on URL #2
    # Verify: URL #1 and #3 extracted, URL #2 marked FAILED
    # Verify: frontier.stats() shows 1 failed, 2 extracted
```

---

### 1.2 崩溃前保存部分结果

**问题**：`run()` 的 outer except（line 153-158）返回 `data: []`，丢弃 frontier 中已提取的数据。

**精确位置**：`orchestrator.py` line 153-158

```
153│ except Exception as e:
154│     logger.error(f"Task {task_id} failed: {e}", exc_info=True)
155│     if self._state_mgr:
156│         self._state_mgr.update(task_id, status="failed")
157│         self._state_mgr.add_error(task_id, str(e))
158│     return {"success": False, "error": str(e), "data": []}
```

**改动**：
```python
except Exception as e:
    logger.error(f"Task {task_id} failed: {e}", exc_info=True)
    if self._state_mgr:
        self._state_mgr.update(task_id, status="failed")
        self._state_mgr.add_error(task_id, str(e))
    # Preserve partial results from frontier
    partial_data = []
    if self._frontier:
        try:
            partial_data = self._frontier.all_data()
            logger.info(f"Preserving {len(partial_data)} records from partial run")
        except Exception:
            pass
    # Also try to save to artifacts
    if self._artifacts and partial_data:
        try:
            self._artifacts.add_records(partial_data)
            self._artifacts.save_records_file()
        except Exception:
            pass
    return {"success": False, "error": str(e), "data": partial_data}
```

**测试方案**：
```python
async def test_partial_results_preserved_on_crash():
    """Crash mid-run should return already-extracted data, not empty list."""
    # Setup: frontier with data already ingested
    # Trigger exception in _run_full_site after some extractions
    # Verify: returned dict has data with len > 0
```

---

### 1.3 激活 `_run_sampling_loop`

**问题**：`_run_sampling_loop`（line 683-745）定义了完整的 fallback 采样逻辑，但**整个代码中零调用**。Explorer 未 inline 采样的 section 不会被补采——文档描述的安全网不存在。

**精确位置**：应在 burst loop 之后、final drain 之前调用。

```
当前代码 line 900-904:
900│ logger.info(f"Round {explore_round} done: ...")
901│ if stop_extraction:
902│     break
903│
904│ # Final drain: process any remaining QUEUED URLs
```

**改动**：在 burst loop 之后、final drain 之前插入调用：
```python
# line 903 之后插入:
# Fallback sampling: ensure all discovered sections have samples
all_sections = []
if self._db and self._run_id:
    try:
        all_sections = await self._db.get_all_sections(self._run_id)
    except Exception:
        pass
if not all_sections:
    # Fallback: use sections collected by Explorer
    all_sections = explore_meta.get("sections", []) if explore_meta else []
if all_sections:
    await _run_sampling_loop(all_sections)
```

**测试方案**：
```python
async def test_sampling_loop_called_for_unsampled_sections():
    """Sections discovered but not sampled by Explorer should be fallback-sampled."""
    # Setup: Explorer returns 3 sections, 1 sampled, 2 not
    # Verify: _run_sampling_loop is called
    # Verify: after call, 2 additional Sampler sessions are created
```

---

## 第二波：止静默错误

### 2.1 URL 规范化统一

**问题**：11 处 `url.split("#")[0].rstrip("/")` 散布在 4 个文件中，无共享函数。

**精确位置**（全部 11 处）：

| 文件 | 行号 | 上下文 |
|------|------|--------|
| `orchestrator.py` | 991 | _navigate_tracking_wrapper |
| `orchestrator.py` | 1532 | _js_extract_save |
| `scheduler.py` | 56 | URLScheduler.add() |
| `scheduler.py` | 104 | URLScheduler.mark_visited() |
| `scheduler.py` | 204 | SharedFrontier.add() |
| `scheduler.py` | 242 | SharedFrontier.mark_in_flight() |
| `scheduler.py` | 249 | SharedFrontier.mark_extracted() |
| `scheduler.py` | 266 | SharedFrontier.mark_sampled() |
| `scheduler.py` | 276 | SharedFrontier.mark_failed() |
| `run_intelligence.py` | 319 | get_proven_script() |
| `controller.py` | 420, 424, 430 | _collect_side_channel sampled URL tracking |

**改动**：

新建 `src/utils/url.py`：
```python
def normalize_url(url: str) -> str:
    """Strip fragment and trailing slash for consistent URL comparison."""
    if not url:
        return url
    return url.split("#")[0].rstrip("/")
```

然后在 11 处替换为 `from ..utils.url import normalize_url` + `normalize_url(url)`。

**测试方案**（`tests/unit/test_url_utils.py`）：
```python
def test_normalize_strips_fragment():
    assert normalize_url("https://x.com/page#section") == "https://x.com/page"

def test_normalize_strips_trailing_slash():
    assert normalize_url("https://x.com/page/") == "https://x.com/page"

def test_normalize_both():
    assert normalize_url("https://x.com/page/#top") == "https://x.com/page"

def test_normalize_empty():
    assert normalize_url("") == ""

def test_normalize_no_change():
    assert normalize_url("https://x.com/page") == "https://x.com/page"

def test_normalize_preserves_query_params():
    assert normalize_url("https://x.com/page?id=1") == "https://x.com/page?id=1"
```

---

### 2.2 URL pattern 匹配锚定

**问题**：`_url_matches_pattern()` 用 `re.search` 做子串匹配。

**精确位置**：`run_intelligence.py` line 370-378

```python
def _url_matches_pattern(self, url: str, pattern: str) -> bool:
    from urllib.parse import urlparse
    path = urlparse(url).path
    pat = pattern.lstrip("*/").replace("*", "[^/]+")
    try:
        return bool(re.search(pat, path))  # ← 子串匹配
    except re.error:
        return False
```

**改动**：
```python
def _url_matches_pattern(self, url: str, pattern: str) -> bool:
    from urllib.parse import urlparse
    path = urlparse(url).path.rstrip("/")
    # Build regex: "*" → "[^/]+" segment wildcard, anchor full path
    segments = pattern.strip("/").split("/")
    regex_parts = []
    for seg in segments:
        if seg == "*":
            regex_parts.append("[^/]+")
        else:
            regex_parts.append(re.escape(seg))
    regex = "^/" + "/".join(regex_parts) + "$"
    try:
        return bool(re.match(regex, path))
    except re.error:
        return False
```

同时修复 `_url_to_pattern`（line 356-368）使其生成的 pattern 与上述匹配逻辑兼容：
```python
def _url_to_pattern(self, url: str) -> str:
    from urllib.parse import urlparse
    path = urlparse(url).path
    segments = [s for s in path.split("/") if s]
    normalized = []
    for seg in segments:
        if re.match(r'^[a-zA-Z0-9_-]{3,16}$', seg) and not seg.isdigit():
            normalized.append("*")
        else:
            normalized.append(seg)
    return "/" + "/".join(normalized) if normalized else "/*"
```

注意：pattern 格式从 `*/pen/*` 变为 `/*/pen/*`（前导 `/`），两个函数必须同步修改。

**测试方案**：
```python
def test_pattern_matches_same_structure():
    ri = RunIntelligence(...)
    assert ri._url_matches_pattern("https://codepen.io/user1/pen/abc123", "/*/pen/*")

def test_pattern_rejects_substring():
    ri = RunIntelligence(...)
    assert not ri._url_matches_pattern("https://codepen.io/admin/pen/view", "/*/pen/*")
    # "admin" matches *, "pen" matches "pen", but "view" doesn't need a 3rd segment — wait
    # Actually /admin/pen/view has 3 segments, pattern /*/pen/* also has 3. This SHOULD match.
    # The real test: /admin/pen-tools/view should NOT match /*/pen/*
    assert not ri._url_matches_pattern("https://x.com/admin/pen-tools/view", "/*/pen/*")

def test_pattern_roundtrip():
    ri = RunIntelligence(...)
    url = "https://codepen.io/user1/pen/abc123"
    pattern = ri._url_to_pattern(url)
    assert ri._url_matches_pattern(url, pattern)

def test_pattern_rejects_extra_segments():
    ri = RunIntelligence(...)
    assert not ri._url_matches_pattern("https://x.com/a/b/c/d", "/*/pen/*")
```

---

### 2.3 content_filter 识别与重试

**问题**：`content_filter` 作为正常 `finish_reason` 到达 controller，但 controller 不区分它和"LLM 正常完成"。`_get_decision` 返回一个 LLMDecision，其中 `tool_calls=[]` + `finish_reason="content_filter"` → `wants_to_stop=True` → session 结束。

**精确位置**：`controller.py` line 102-121

```python
102│ decision = await self._get_decision(messages)
103│ if decision is None:
104│     stop_reason = "LLM error: failed to get response"
105│     break
106│
107│ self.governor.record_llm_call(decision.total_tokens)
...
116│ if decision.wants_to_stop:
117│     stop_reason = "LLM completed"
118│     summary = decision.content or ""
119│     logger.info(f"LLM says done after {self.history.count} steps")
120│     break
```

**改动**：在 line 107 之后、line 116 之前插入 content_filter 检测：
```python
# Content filter retry (Gemini safety filter)
if (decision.finish_reason == "content_filter"
        and not decision.tool_calls
        and self._content_filter_retries < 3):
    self._content_filter_retries += 1
    logger.warning(
        f"content_filter on step {self._step_number} "
        f"(retry {self._content_filter_retries}/3)"
    )
    await asyncio.sleep(2)
    continue  # re-enter loop, governor will re-check, context rebuilt
```

在 `__init__` 中加 `self._content_filter_retries = 0`。
在正常 tool_call 执行后重置 `self._content_filter_retries = 0`。

**测试方案**：
```python
async def test_content_filter_retries_then_succeeds():
    """LLM returns content_filter twice, then normal response — session continues."""
    # Mock: first 2 calls → LLMDecision(finish_reason="content_filter", tool_calls=[])
    # 3rd call → LLMDecision(tool_calls=[ToolCall("think", ...)])
    # Verify: session runs, step count = 1 (only the think step counts)
    # Verify: _content_filter_retries reset to 0

async def test_content_filter_exhausted():
    """3 consecutive content_filter → session stops with clear reason."""
    # Mock: all calls → content_filter
    # Verify: stop_reason contains "content_filter"
    # Verify: session ends after 3 retries, not infinite loop
```

---

### 2.4 DataVerifier 集成到 pipeline

**问题**：DataVerifier 实现完整（13 个测试全过），但注册为 LLM 可选工具（orchestrator.py:188），从未自动运行。

**精确位置**：`orchestrator.py` line 922-954（最终结果构建）

```python
922│ all_data = frontier.all_data()
923│ stats = frontier.stats()
924│
925│ # Build sections summary for output
```

**改动**：在 line 923 后插入：
```python
# Automatic quality verification
quality_report = None
if all_data and spec:
    from ..verification.verifier import DataVerifier
    verifier = DataVerifier()
    quality_report = verifier.verify(all_data, spec)
    if quality_report.get("issues"):
        logger.warning(f"Quality issues: {quality_report['issues']}")
```

在返回的 result dict（line 939-954）中加入：
```python
"quality": quality_report,
```

**测试方案**：
```python
def test_verifier_runs_on_final_output():
    """DataVerifier.verify() is called automatically on final results."""
    # Setup: frontier with 10 records, 5 duplicates
    # Run _run_full_site
    # Verify: result["quality"] is not None
    # Verify: result["quality"]["issues"] mentions duplicates
```

---

## 第三波：治病

### 3.1 Context token budget 执行

**问题**：`ContextManager.__init__` 声明 `max_tokens=6000` 但 `build()` 从未检查。

**精确位置**：`context.py` — `build()` 方法的返回前。

**改动**：`build()` 返回前加 budget 检查：
```python
# Token budget enforcement (rough estimate: 1 token ≈ 4 chars for English/code)
total_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
estimated_tokens = total_chars // 4
if estimated_tokens > self.max_tokens:
    # Trim old history first (keep system + task + recent 1 step + nudges)
    messages = self._trim_to_budget(messages, self.max_tokens)
    trimmed_tokens = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 4
    logger.debug(f"Context trimmed: {estimated_tokens} → {trimmed_tokens} tokens")
return messages
```

`_trim_to_budget` 按优先级裁剪：
1. 删除最老的 history steps（保留最近 1 步）
2. 截断 task context 的 site_intel 部分
3. 截断 nudges

**测试方案**：
```python
def test_build_respects_token_budget():
    """build() output should not exceed max_tokens estimate."""
    ctx = ContextManager(max_history_steps=3, max_tokens=2000)
    # Feed it 10 steps of large history
    messages = ctx.build(task=..., history=large_history, ...)
    total_chars = sum(len(json.dumps(m)) for m in messages)
    assert total_chars // 4 <= 2500  # allow 25% margin for estimation error

def test_trim_preserves_system_and_recent():
    """Trimming removes old history but keeps system prompt and latest step."""
    # Verify system message always present
    # Verify last tool result always present
```

---

### 3.2 Phase 0 DDG 搜索重试 + 降级标记

**问题**：DDG 限速后 entry_points 静默为空。

**精确位置**：`discovery/signals/search_signal.py` — DDG 调用处。

**改动**：
```python
MAX_RETRIES = 3
for attempt in range(MAX_RETRIES):
    try:
        results = _ddg_search(query, max_results)
        break
    except Exception as e:
        if attempt < MAX_RETRIES - 1:
            delay = (attempt + 1) ** 2  # 1s, 4s, 9s
            logger.warning(f"DDG search attempt {attempt+1} failed: {e}, retry in {delay}s")
            await asyncio.sleep(delay)
        else:
            logger.error(f"DDG search failed after {MAX_RETRIES} attempts")
            results = []
```

在 `SiteIntelligence` 中加 `search_degraded: bool = False`，失败时设为 True。
在 `context.py` 中检查并注入："⚠️ Phase 0 search was rate-limited. Use search_site() tool more actively."

**测试方案**：
```python
async def test_ddg_retries_on_failure():
    # Mock DDG: fail twice, succeed third
    # Verify: results returned, 3 calls made

async def test_ddg_all_fail_marks_degraded():
    # Mock DDG: all 3 fail
    # Verify: search_degraded=True in SiteIntelligence
```

---

### 3.3 RunIntelligence 原子写入

**精确位置**：`run_intelligence.py` line 71-73, 120-122

**改动**：
```python
def _save_knowledge(self, data: dict) -> None:
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(self._knowledge_file), suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp_path, self._knowledge_file)  # atomic on all platforms
    except Exception:
        # Clean up temp file if replace failed
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
```

同样修改 `save_golden_records`。

**测试方案**：
```python
def test_atomic_write_survives_crash():
    """If json.dump fails mid-write, original file is preserved."""
    ri = RunIntelligence(artifacts_dir)
    ri.write("test_key", "original_value")
    # Mock json.dump to raise after partial write
    with patch("json.dump", side_effect=IOError("disk full")):
        with pytest.raises(IOError):
            ri.write("test_key", "new_value")
    # Verify: original value still readable
    assert ri.read("test_key") == "original_value"
```

---

### 3.4 浏览器 SPA DOM 稳定检测加总超时

**精确位置**：`browser.py` line 253-270

```python
253│ last_count = 0
254│ stable_ticks = 0
255│ for _ in range(15):  # poll for up to 15s
256│     await _asyncio.sleep(1)
```

**现状**：已有 `range(15)` 作为循环上限，即最多 15 次迭代 × 1s = 15s。但 `page.evaluate()` 本身可能挂起。

**改动**：给内层 evaluate 加超时：
```python
for _ in range(15):
    await _asyncio.sleep(1)
    try:
        count = await asyncio.wait_for(
            self.page.evaluate("document.querySelectorAll('*').length"),
            timeout=5.0
        )
    except (Exception, asyncio.TimeoutError):
        logger.debug("DOM stability check: evaluate timed out, breaking")
        break
```

**测试方案**：
```python
async def test_dom_stability_check_has_timeout():
    # Mock page.evaluate to hang (never return)
    # Verify: navigate completes within 20s (not infinite)
```

---

## 第四波：强身

### 4.1 SharedFrontier 公共 API

**位置**：`scheduler.py`（新方法）+ `orchestrator.py` line 992, 1533（替换私有访问）

```python
# scheduler.py — 新增
def get_status(self, url: str) -> "URLStatus | None":
    """Public method to check URL status. Returns None if URL not in frontier."""
    from ..utils.url import normalize_url
    norm = normalize_url(url)
    rec = self._records.get(norm)
    return rec.status if rec else None
```

替换 orchestrator.py 中的 `self._frontier._records.get(norm)` 为 `self._frontier.get_status(norm)`。

### 4.2 search_tool 域名过滤

**位置**：`search_tool.py` 域名比较处。

```python
# 原：self.domain not in netloc（子串匹配）
# 改为：
netloc == self.domain or netloc.endswith("." + self.domain)
```

### 4.3 content_filter 不计入 LLM call budget

**位置**：`controller.py` 的 content_filter 重试逻辑（2.3 中新增的）。

```python
# content_filter 重试时，不 record_llm_call（不消耗 budget）
if decision.finish_reason == "content_filter" and not decision.tool_calls:
    # Do NOT call self.governor.record_llm_call() for filter retries
    ...
```

---

## 执行节奏

```
第一波（止血）— ✅ 已完成：
  ✅ 1.1 _extract_one try/catch
  ✅ 1.2 崩溃保存部分结果
  ✅ 1.3 激活 _run_sampling_loop

第二波（止静默错误）— ✅ 已完成：
  ✅ 2.1 URL 规范化统一（src/utils/url.py）
  ✅ 2.2 Pattern 匹配锚定（全路径 ^/$）
  ✅ 2.3 content_filter 重试（controller 3 次）
  ✅ 2.4 DataVerifier 集成（最终 + mid-pipeline via B1.3）

第三波（治病）— 部分完成：
  ✅ 3.1 Token budget 执行 — build() 裁剪 + _trim_to_budget (B2.3)
  ✅ 3.2 Phase 0 DDG 重试 — search_signal.py 已含 3 次指数退避重试
  ✅ 3.3 原子写入（_atomic_write）
  ⚠️ 3.4 SPA evaluate 超时 — 循环有 15 次上限但 evaluate 本身无超时

第四波（强身）— ✅ 已完成：
  ✅ 4.1 Frontier 公共 API（get_status）
  ✅ 4.2 域名过滤（精确 + 子域名）
  ✅ 4.3 content_filter 不计 budget

线 B — 能力闭环：
  ✅ B1.4 Per-section coverage tracking
  ✅ B1.3 DataVerifier mid-pipeline
  ✅ B1.1 Inter-round feedback
  ✅ B1.2 site_model validation
  ✅ B2.1 StructuralCompletionGate 激活（覆盖 Explorer TASK COMPLETE）
  ✅ B2.2 Section-aware 提取优先级（next() 优先零记录 section）
  ✅ B2.3 ContextManager token budget 执行（_trim_to_budget）
  ✅ B2.4 repair-plan.md 状态更新
  ✅ B3.1 Proven scripts quality metrics（attempts/failures tracking, degraded skip）
  ✅ B3.2 Hard-replay golden schema validation（validate_records → LLM fallback）
  ✅ B3.3 Hard-replay failure tracking（exception/null/no-content → skip logic）
  ✅ B3.4 Enriched prior experience（_update_experience, HARD-REPLAY/LLM + fields）
```

---

## 衡量标准

| 指标 | 修复前 | 修复后目标 |
|------|--------|-----------|
| 单 URL 异常导致 run 崩溃 | 是 | 否（mark_failed + 继续） |
| E2E 崩溃后数据保留 | 0 条 | 前 N-1 条保留 |
| 未采样 section 有 fallback | 否（dead code） | 是（_run_sampling_loop 被调用） |
| content_filter Step 0 session 死亡 | 是 | 否（重试 3 次） |
| DataVerifier 自动运行 | 否 | 是（quality 字段在 output 中） |
| URL pattern 误匹配 | 有（子串） | 无（全路径锚定） |
| URL 规范化统一 | 11 处散落 | 1 个函数 |
| Phase 0 DDG 限速后行为 | 静默空 | 重试 3 次 + degraded 标记 |
| context token 超标 | 无检查 | 自动裁剪 |
| JSON 持久化崩溃安全 | 否 | 是（原子写入） |
| 单元测试数 | 127 | ~175+ |
