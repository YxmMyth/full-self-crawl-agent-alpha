# Agent 设计顶级调研综合报告

> 调研时间: 2026-03-03
> 用途: 指导 full-self-crawl-agent-alpha 的架构演进

## 调研来源

| # | 来源 | 作者/团队 | 核心贡献 |
|---|------|----------|----------|
| 1 | [Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents) | **Anthropic** (Erik Schluntz, Barry Zhang) | ACI 设计哲学、5 种 workflow 模式、simplicity 原则 |
| 2 | [LLM Powered Autonomous Agents](https://lilianweng.github.io/posts/2023-06-23-agent/) | **Lilian Weng** (OpenAI 研究员) | Agent 三件套理论框架：Planning + Memory + Tool Use |
| 3 | [Agentic Design Patterns Part 1-5](https://www.deeplearning.ai/the-batch/how-agents-can-improve-llm-performance/) | **Andrew Ng** (DeepLearning.AI) | 四大设计模式：Reflection、Tool Use、Planning、Multi-Agent |
| 4 | [Orchestrating Agents (Swarm)](https://cookbook.openai.com/examples/orchestrating_agents) | **OpenAI** | Routines + Handoffs 模式，极简 agent 编排 |
| 5 | [smolagents](https://huggingface.co/blog/smolagents) | **HuggingFace** | Code Agent > JSON Tool Calling 的实证研究 |
| 6 | [What is an Agent](https://blog.langchain.dev/what-is-an-agent/) | **LangChain** (Harrison Chase) | Agency 光谱理论，从 Router → State Machine → Autonomous |
| 7 | [Voyager](https://voyager.minedojo.org/) | **NVIDIA + Caltech** (Jim Fan 等) | 终身学习 agent，Skill Library，自动课程 |

---

## 一、所有人都同意的事（共识）

### 1. 核心循环极其简单

每一家的核心 loop 本质上是同一个东西：

```python
memory = [task]
while should_continue(memory):
    action = llm_decide(memory)       # LLM 看历史，选下一步
    observation = execute(action)     # 执行工具
    memory += [action, observation]   # 更新记忆
```

Anthropic 原话：*"Agents are typically just LLMs using tools based on environmental feedback in a loop."*

OpenAI Swarm 的完整 agent 框架核心代码不到 100 行。smolagents 的核心 agent 逻辑在一个文件内。

**👉 对我们的意义**：我们的 CrawlController 已经是这个模式了。架构是对的。

### 2. 从简单开始，只在有证据时增加复杂度

Anthropic：*"Start with simple prompts, optimize them with comprehensive evaluation, and add multi-step agentic systems only when simpler solutions fall short."*

Andrew Ng：*"Many agentic workflows do not need planning."* 他明确说 Reflection 和 Tool Use 已经成熟可靠，但 Planning 和 Multi-Agent 结果还不可预测。

**👉 对我们的意义**：不要急着加 multi-agent 或复杂的 planning 层。先把单 agent + 好工具做扎实。

### 3. 工具设计（ACI）是最高投入回报比的工作

Anthropic 把它提升到和 HCI（人机交互）同等地位：*"Think about how much effort goes into human-computer interfaces (HCI), and plan to invest just as much effort in creating good agent-computer interfaces (ACI)."*

具体建议：
- 工具描述要像给初级开发者写文档一样清晰
- 包含 example usage, edge cases, input format requirements
- **Poka-yoke**（防呆）：修改参数设计让错误用法变困难
- 当多个工具相似时，名称和描述的区分尤其重要

---

## 二、四大设计模式（Andrew Ng 框架）

按成熟度排序：

| 模式 | 成熟度 | 核心思想 | 我们的现状 |
|------|--------|----------|-----------|
| **Reflection** | ⭐⭐⭐ 成熟 | LLM 审视自己的输出并改进 | ✅ 有 verify_quality，但不强制 |
| **Tool Use** | ⭐⭐⭐ 成熟 | LLM 调用外部工具获取信息/执行操作 | ✅ 核心能力，有 10+ 工具 |
| **Planning** | ⭐⭐ 发展中 | LLM 自主分解任务、制定步骤 | ⚠️ exploration 阶段有，但很浅 |
| **Multi-Agent** | ⭐ 实验性 | 多个 agent 协作分工 | ❌ 暂无，目前也不需要 |

**关键洞察**：Andrew Ng 发现用 GPT-3.5 + agent workflow (迭代) 能达到 95.1% 准确率，而 GPT-4 zero-shot 只有 67%。**迭代比模型更重要。**

**👉 对我们的意义**：
- **Reflection 是最该强化的**。verify_quality 应该从"可选"变成"默认执行"，这是最高 ROI 的改进
- Planning 暂时不急，我们的 exploration 阶段已经在做简单版的 planning

---

## 三、Voyager 的 Skill Library — 最值得借鉴的设计

Voyager（Jim Fan, NVIDIA）是 Minecraft 里的终身学习 agent，有三个组件非常值得我们学习：

### 1. Skill Library（技能库）
- 每次成功完成任务，把方法存为可复用的"技能"（代码片段）
- 技能用自然语言描述做索引，下次遇到类似场景就检索出来用
- **技能可以组合**：复杂技能 = 简单技能的组合
- 效果：比无技能库的 baseline 快 15.3x

**👉 对我们的意义**：这对爬虫 agent 极其有价值！
- 成功提取一个电商站后，把 CSS selector 策略存为"技能"
- 下次遇到类似结构的电商站，直接检索出来作为起点
- 不同网站模式（列表页、详情页、SPA、API 返回）各有一套"技能"
- 这是**长期记忆**的实现方式 — 目前我们没有跨任务记忆

### 2. Automatic Curriculum（自动课程）
- 不人工指定任务，让 LLM 根据当前状态自动决定下一个要探索的目标
- 目标是"发现尽可能多的新东西"（novelty search）

**👉 对我们的意义**：这就是"深度探索"的实现方向。现在我们的 exploration 只做了"收集链接"，但 Voyager 的思路是让 LLM 自主决定"这个网站还有什么我不知道的"。

### 3. Iterative Prompting with Self-Verification
- 每次执行后，把环境反馈 + 执行错误 + 自我验证结果都喂给 LLM
- LLM 据此改进代码直到任务成功

**👉 对我们的意义**：我们已经有这个 loop（tool result → history → next decision），但自我验证（verify_quality）需要更系统地集成。

---

## 四、Code Agent vs JSON Tool Calling

smolagents 的研究结论：**让 LLM 写代码比 JSON 工具调用效果显著更好**。

原因：
- **可组合**：代码能嵌套函数调用、存储中间变量
- **表达力**：JSON 只能平铺参数，代码能表达任意逻辑
- **训练数据**：LLM 训练集里有海量代码

```python
# JSON 模式（传统）
{"tool": "search", "args": {"query": "X"}}
{"tool": "extract", "args": {"selector": "..."}}

# Code 模式（smolagents 推荐）
results = search("X")
for r in results:
    data = extract(r, selector="...")
    if data.quality > 0.8:
        save(data)
```

**👉 对我们的意义**：我们的 `execute_code` 已经具备了 Code Agent 的能力！但目前它是作为"后备"存在的。可以考虑让它成为**主要模式**——LLM 直接写 Python 来完成提取，其他工具（navigate, get_html）作为 Python 函数可调用。

---

## 五、Memory 架构（Lilian Weng 框架）

Lilian Weng 把 agent 记忆分为三层：

| 记忆类型 | 对应 | 我们的实现 | 差距 |
|---------|------|-----------|------|
| **感知记忆** | 原始输入的 embedding | 当前页面 HTML | ✅ 有 |
| **短期记忆** | 上下文窗口内的 in-context learning | StepHistory (最近 3 步) | ✅ 有 |
| **长期记忆** | 外部向量存储 + 检索 | **无** | ❌ 最大差距 |

**👉 长期记忆是我们最大的结构性缺失**。每次任务从零开始，不记得之前爬过什么、什么策略有效。Voyager 的 Skill Library 就是一种长期记忆的实现。

---

## 六、OpenAI Swarm 的 Routines + Handoffs

OpenAI 的 Swarm 框架展示了一个有趣的模式：
- **Routine** = system prompt + tools（不是硬编码流程，而是带条件分支的自然语言指令）
- **Handoff** = 一个 agent 把对话交给另一个 agent（通过返回 Agent 对象）

LLM 能很好地处理自然语言中的条件分支，比代码 if/else 更灵活。

**👉 对我们的意义**：我们的 exploration → extraction 切换就是一种 handoff。目前是 orchestrator 硬编码控制的。可以考虑让 LLM 自行决定"我已经了解够了，开始提取"。

---

## 七、Anthropic 的 5 种 Workflow 模式

| 模式 | 适用场景 | 与我们的关系 |
|------|---------|-------------|
| **Prompt Chaining** | 任务可分为固定顺序的子任务 | spec 推断 → exploration → extraction 就是 chaining |
| **Routing** | 输入需要分类后走不同路径 | 可用于根据网站类型选择策略 |
| **Parallelization** | 独立子任务可并行 | 多页提取可以并行 |
| **Orchestrator-Workers** | 子任务不可预测 | full_site 模式的探索阶段 |
| **Evaluator-Optimizer** | 有明确评估标准，迭代改进有价值 | verify_quality → 改进提取 |

---

## 八、给我们项目的行动建议（按优先级排序）

| 优先级 | 行动 | 来源 | 难度 | 说明 |
|--------|------|------|------|------|
| 🔴 P0 | **强化 Reflection**: verify_quality 从可选变默认 | Andrew Ng | 低 | 最高 ROI 改进 |
| 🔴 P0 | **投资 ACI**: 每个工具的描述、error message、示例都精心设计 | Anthropic | 中 | 减少 LLM 工具使用错误 |
| 🟡 P1 | **Skill Library**: 成功策略持久化，跨任务复用 | Voyager | 高 | CSS selector 策略库 |
| 🟡 P1 | **Code-first 模式**: execute_code 升级为主要提取方式 | smolagents | 中 | 提升灵活性和表达力 |
| 🟢 P2 | **长期记忆**: 向量存储记住网站模式和成功策略 | Lilian Weng | 高 | Skill Library 的底层 |
| 🟢 P2 | **自主 Handoff**: LLM 自行决定 explore→extract 转换 | OpenAI Swarm | 中 | 减少硬编码控制流 |
| ⚪ P3 | **Automatic Curriculum**: 深度探索，LLM 自己规划探索路径 | Voyager | 高 | 解决"浅层探索"问题 |
| ⚪ P3 | **Multi-Agent**: 探索 agent + 提取 agent + 验证 agent | Andrew Ng | 高 | 目前不需要 |

---

## 一句话总结

> **正确的道路是：极简核心循环 + 精心设计的工具(ACI) + Reflection 强制执行 + 逐步加入 Skill Library 做长期记忆。**
> 不要急着做 multi-agent 或复杂 planning。先让单个 agent 在每个网站上都能自主决策并自我改进。
