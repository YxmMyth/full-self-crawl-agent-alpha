# Full-Self-Crawl-Agent Alpha

一个以 LLM 为控制核心的目标导向型网络爬虫 agent。不是"爬 URL 列表"，而是"理解目标，自主导航，提取数据"。

---

## 快速上手

### 1. 配置

```bash
cp .env.example .env
# 编辑 .env，填入：
# LLM_API_KEY=your-key
# LLM_BASE_URL=http://your-gateway:3000/v1
```

### 2. 启动基础服务（首次或重启后）

```bash
docker compose up -d camoufox db
```

Camoufox 是反检测浏览器（Firefox + C++ 级别指纹注入），db 是 PostgreSQL 历史库。两个服务常驻，不需要每次重启。

### 3. 运行 agent

```bash
# 推荐命令（full_site 模式 + Docker + gemini-2.5-flash）
docker compose run --rm dev python -m src.main https://example.com \
  --requirement "你想提取什么" \
  --mode full_site \
  --model gemini-2.5-flash
```

输出结果写入项目根目录的 `output.json`，artifact 文件在 `artifacts/`。

---

## 关键参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--mode full_site` | ✅ 推荐 | Phase 0 发现 → Explorer → 提取事件循环 |
| `--mode single_page` | 仅测试用 | 只提取一个页面，不导航 |
| `--model gemini-2.5-flash` | 配置文件值 | 覆盖 LLM 模型；flash 速度快成本低适合调试 |
| `--model gemini-2.5-pro` | — | 质量最高，用于生产 |
| `--max-steps 30` | 30 | 每个 agent session 最大步数 |
| `--max-time 300` | 300s | 每个 agent session 最大时间 |
| `--output result.json` | output.json | 结果输出路径 |

---

## 模型选择指南

本项目通过代理网关访问模型（`LLM_BASE_URL`），支持任何 OpenAI 兼容接口。

| 场景 | 推荐模型 |
|------|---------|
| 调试 / 快速迭代 | `gemini-2.5-flash` — 速度快，够用 |
| E2E 验证 | `gemini-2.5-flash` — 平衡速度与质量 |
| 生产 / 困难站点 | `gemini-2.5-pro` 或 `claude-opus-4-5` |
| 视觉任务（截图分析）| `gemini-2.5-flash`（vision_llm 配置） |

---

## Docker 说明

```yaml
# docker-compose.yml 里的三个关键服务：
camoufox   # 反检测浏览器，常驻，不要停
db         # PostgreSQL 历史库，常驻
dev        # 开发容器，按需 run --rm
```

**为什么必须用 Docker？**
- `camoufox` 做了 C++ 级别的浏览器指纹伪装，绑定在 Docker 里
- `BROWSER_WS_URL=ws://camoufox:1234/ws` 在 docker-compose 里自动配置
- 本地直接跑会因为找不到 WebSocket 端点而失败

**查看实时日志：**
```bash
# 获取容器名
docker ps --filter "name=full-self-crawl" --format "{{.Names}}: {{.Status}}"

# 实时跟踪
docker logs <container-name> -f 2>&1

# 只看最新 50 行
docker logs <container-name> 2>&1 | Select-Object -Last 50   # PowerShell
docker logs <container-name> 2>&1 | tail -50                  # bash
```

---

## 测试

```bash
# 单元测试（快，不需要 Docker，不需要 LLM）
python -m pytest tests/unit/ -q

# E2E 测试（需要 Docker + camoufox + db 运行中）
docker compose run --rm dev python -m src.main https://codepen.io \
  --requirement "find threejs pens" \
  --mode full_site \
  --model gemini-2.5-flash
```

---

## 项目结构

```
src/
├── main.py                  # CLI 入口
├── management/
│   ├── orchestrator.py      # 核心编排逻辑（Phase 0/1/2）
│   ├── scheduler.py         # SharedFrontier URL 状态机
│   ├── context.py           # LLM 消息构建（system prompt / task context）
│   └── governor.py          # 步数/时间/完成条件控制
├── execution/
│   └── controller.py        # 单个 agent session 执行循环
├── tools/
│   ├── browser.py           # navigate, go_back, scroll, etc.
│   ├── extraction.py        # extract_css, js_extract_save, etc.
│   ├── search_tool.py       # search_site（domain-locked，frontier-aware）
│   ├── code_runner.py       # execute_code（Python/JS 沙箱）
│   └── registry.py          # 工具注册中心
├── discovery/
│   └── engine.py            # Phase 0：robots.txt + sitemap + search 预发现
└── strategy/
    ├── spec.py              # CrawlSpec + SpecInferrer（理解用户意图）
    └── gate.py              # CompletionGate（判断任务完成）
```

---

## 架构概述（三阶段）

```
Phase 0: 发现（非 LLM，~3s）
  → robots.txt + sitemap + search 预热
  → 输出 SiteIntelligence（entry_points, direct_content, live_endpoints）

Phase 1: Explorer（单个 LLM agent）
  → 导航站点，理解结构，上报内容 URL 到 frontier
  → 工具：navigate, search_site, analyze_links, report_urls

Phase 2: 提取事件循环（每个 URL 一个 LLM agent）
  → 从 frontier 取 URL → goal-directed 提取 → 反哺新 URL 到 frontier
  → 工具：js_extract_save, execute_code, navigate, search_site, go_back
```

**关键设计原则：**
- Agent 被给予**目标**，不是被给予 URL 指令（goal-directed，非 URL-directed）
- **URL 去重在 `navigate` 层**：任何路径发现的 URL（search、links、直接导航），到达 navigate 时统一检查 frontier 状态，已 EXTRACTED/SAMPLED 的直接跳过，无需 browser 操作
- `execute_code` 里的 `report_urls()` 会被 frontier 去重，agent 可以大胆上报不用担心重复

---

## 登录认证（可选）

部分站点需要登录才能访问内容：

```bash
# 1. 保存登录状态（交互式）
python scripts/save_login.py https://target-site.com
# 浏览器打开，手动登录，按 Enter 保存

# 2. 之后的所有 docker compose run 会自动注入登录状态
# （states/auth_state.json 挂载到容器里）
```

---

## 常见问题

**Q: 结果 output.json 里记录很少，manifest 里很多？**  
A: `_is_substantive()` 过滤掉了空记录（所有 target_fields 都为空）。检查 `artifacts/data/records.jsonl` 看原始记录，对照 spec 的 `target_fields` 判断提取质量。

**Q: agent 一直在同一个页面转？**  
A: Governor 的 navigation loop 检测会强制停止（同一 URL 访问 5 次以上无进展）。通常是 SPA 限速或页面被反爬。查 `[Governor force stop]` 日志。

**Q: 换模型怎么配置？**  
A: 优先级：CLI `--model` > 环境变量 `LLM_MODEL` > `config/settings.json` 的 `llm.model`。

**Q: 本地不用 Docker 能跑吗？**  
A: 不能正常工作。浏览器依赖 camoufox WebSocket，去掉 Docker 后浏览器工具全部失效。
