# RepoPilot

> **AI 驱动的「GitHub Issue → 修复 PR」自反思 agent。**
>
> 不是通用型 copilot。RepoPilot 只做一件事：读懂一个 GitHub Issue，检索代码库，生成修复，跑测试，然后开一个 PR。当修复失败时，它会反思*为什么*失败，再重试。

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
</p>

---

## 为什么是 RepoPilot

> 📓 **想看工程细节？** 最值得读的是
> [`docs/ENGINEERING_LOG.md`](docs/ENGINEERING_LOG.md) —— 一份诚实的、以评估驱动的
> 调试记录（假设、埋点、被证伪的假设、以及负结果），而不是拿成功率吹牛。

RepoPilot 面向**专业开发者**——那些维护着真实项目、有测试套件、有 CI、有 PR 流程的人。它不是给 vibe coder 的黑盒自动修理工：它把推理过程摊开给你看，默认需要你审查，并且在修不了的时候会老实承认。

| | RepoPilot | Sweep | Devin | Claude Code |
|---|:---:|:---:|:---:|:---:|
| **可观测的推理链** | ✅ LangGraph trace | ❌ 黑盒 | ❌ 只有 agent 日志 | ❌ 不透明循环 |
| **开源** | ✅ MIT | ✅ MIT | ❌ 闭源 | ❌ 闭源 |
| **模型自由** | ✅ 用你自己的 API key | ✅ | ❌ | ❌ |
| **自反思重试** | ✅ REFLECT 节点 | ❌ | ❌ | ❌ |
| **本地跑测试** | ✅ clone + pytest | ❌ | ✅ | ✅ |
| **产出** | 草稿 PR（你审查） | 自动合并 | 完整 PR | 补丁文件 |

**核心差异化：**

- **可观测** —— LangGraph 显式状态机 + 条件边。每一次相位转换、每一次工具调用、每一次反思都通过内置 Tracer 记进 JSONL。你能调试出 agent *为什么*选了这个文件、*为什么*这样规划补丁、*哪里*出了错。
- **自反思** —— 当修复未通过测试，agent 进入 `REFLECT`：LLM 分析错误日志，找出根因，再把分析喂回 planner。它记得自己试过什么，避免重复同样的错误。
- **模型无关** —— 插入任意 OpenAI 兼容 API：DeepSeek、Ollama、LiteLLM 代理，或 OpenAI 本身。模型、成本、数据都由你掌控。
- **生而开源** —— 竞品（Devin、Claude Code）是闭源的。RepoPilot 采用 MIT 许可，你可以自托管、审计、扩展。

---

## 快速开始

```bash
pip install repopilot
```

设置你的 token：

```bash
export GITHUB_TOKEN=ghp_...
export LLM_API_KEY=sk-...         # 或 DEEPSEEK_API_KEY
export LLM_MODEL=deepseek-v4-pro  # 可选，默认 deepseek-v4-pro
```

运行：

```bash
repopilot https://github.com/org/repo/issues/42
```

只分析、不开 PR：

```bash
repopilot https://github.com/org/repo/issues/42 --dry-run
```

机器可读输出：

```bash
repopilot https://github.com/org/repo/issues/42 --json
```

---

## 工作原理

RepoPilot 实现了一个带自反思重试循环的**六相位状态机**，构建在 LangGraph 之上（未安装 LangGraph 时自动回退到内置 runner）。

```
                    ┌──────────────┐
                    │ UNDERSTAND   │  ← 读 issue，分类类型/严重度
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │   LOCATE     │  ← GitHub 代码搜索 → 按相关性排序
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │    PLAN      │  ← LLM 生成补丁 + 测试命令
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │   EXECUTE    │  ← git clone → 应用补丁 → 跑 pytest
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐     ┌──────────────┐
                    │   VERIFY     │────▶│   COMMIT     │──▶ DONE
                    └──────┬───────┘     └──────────────┘
                           │ 失败
                    ┌──────▼───────┐
                    │   REFLECT    │  ← LLM 分析失败根因
                    └──────┬───────┘
                           │
                           └───────────▶ PLAN  （带反思上下文重试）
```

**每个相位都是 LangGraph `StateGraph` 里的一个离散节点：**

| 相位 | 节点 | 做什么 |
|-------|------|-------------|
| `UNDERSTAND` | `understand_issue` | 通过 API 拉取 GitHub Issue，分类（bug/feature/security），提取标签与严重度 |
| `LOCATE` | `locate_code` | 对从 issue 正文提取的关键词做 GitHub 代码搜索，按相关性给候选文件打分，读取 top-6 文件内容 |
| `PLAN` | `plan_fix` | 用 issue 上下文 + 相关文件内容 + 历史失败反思提示 LLM → 生成补丁与测试命令 |
| `EXECUTE` | `execute_fix` | 把目标仓库 clone 到临时目录，用 `git apply` 应用补丁，跑项目测试套件（默认 `pytest`） |
| `VERIFY` | `verify_fix` | 检查测试输出。成功→`COMMIT`，失败→`REFLECT`，达最大重试或同一失败出现两次→`FAILED` |
| `REFLECT` | `reflect_on_failure` | LLM 分析修复*为什么*失败——具体错误、错误假设、遗漏边界情况，再喂回 `PLAN` |
| `COMMIT` | `commit_fix` | 通过 GitHub Contents API 推送改动文件，附带修复计划与测试结果开一个草稿 PR |
| `FAILURE` | `handle_failure` | 在 issue 下留言总结所发现的（相关文件、尝试过的补丁、失败原因）——部分进展，依然有用 |

**内建护栏：**

- **Token 预算** —— 每次运行可配置；超额时优雅停止，而不是烧光额度
- **重复失败检测** —— 同一补丁两次产生同样错误，则中止而非死循环
- **最大重试上限** —— 默认 3，是真会触达的、不是摆设
- **Pydantic 校验的结构化输出** —— LLM 响应被解析成带 schema 校验的类型化模型，并带 `ValidationError` 自动重试回退

---

## 架构深入

### Agent 状态（`AgentState`）

整次运行被建模为单个 `Pydantic` 模型——类型化、可序列化、可调试：

```python
class AgentState(BaseModel):
    issue_url: str
    issue_title: str
    issue_body: str
    current_phase: Phase                # 枚举驱动的路由
    relevant_files: list[FileInfo]      # 带相关性分数的排序
    fix_attempts: list[FixAttempt]      # 每次尝试的补丁 + 测试结果
    conversation_history: list[ConversationTurn]
    token_usage: int
    reflection_notes: str               # 由 REFLECT 节点填充
    # ... tool_calls, owner, repo, branch, pr_url 等
```

### 图引擎（双后端）

RepoPilot 在安装了 LangGraph 时使用它，同时自带一个 `FallbackStateGraph` + `FallbackCompiledGraph`，用一个带 `route_from_state` 的简单 while 循环满足同样的 `graph.ainvoke(state)` 契约。这意味着在无法安装 LangGraph（或你偏好零额外依赖）的环境里，agent 行为完全一致。

```python
graph = build_agent_graph()          # 返回 LangGraph 或 Fallback
final_state = await run_graph(graph, state)  # 相同接口
```

### Pydantic 校验的 LLM 输出

与其寄望 LLM 返回格式良好的 JSON，每次结构化调用都走 `validate_or_retry()`：

1. 把原始 LLM 响应解析为 JSON
2. 对目标 Pydantic 模型做校验
3. 遇 `ValidationError`：把 schema 错误注入提示，重试一次
4. 第二次仍失败：带告警回退到原始 dict——agent 继续运行而非崩溃

```python
# src/llm.py
async def validate_or_retry(system: str, user: str, schema: type[BaseModel]) -> dict
```

### Tracer（可观测性）

每次运行生成一个 trace ID。所有相位转换、工具调用、错误都以结构化 JSONL 记录：

```python
tracer.log("phase_enter", {"from": "PLAN", "to": "EXECUTE"})
tracer.log("tool_call", {"name": "search_code", "args": {...}}, result)
tracer.log("agent_v2_done", {...}, error="...")
```

### Token 预算管理

每次 LLM 调用后估算 token 用量（字符数 ÷ 4）。每个节点在发起更多调用前都检查 `_is_budget_exceeded(state)`。超额时 agent 路由到 `FAILURE`，并在 issue 上发布部分进展留言。

---

## 示例

下面是 RepoPilot 修复一个真实 issue 的过程：

```bash
$ repopilot https://github.com/cookiecutter/cookiecutter/issues/1973

🔍 RepoPilot analyzing https://github.com/cookiecutter/cookiecutter/issues/1973...

Phase: DONE
Success: True
PR: https://github.com/cookiecutter/cookiecutter/pull/2100
Turns: 7
Token used: 8420

Relevant files (3):
  cookiecutter/generate.py (score: 0.72)
  cookiecutter/config.py (score: 0.48)
  tests/test_generate.py (score: 0.35)

Fix attempts: 1
  ✅ Attempt 1: cookiecutter/generate.py
```

agent 识别出：配置 cookiecutter 模板时，CLI 布尔覆盖值被当成字符串（而非布尔）传入。它生成了一个针对性的 `isinstance` 转换，应用补丁，跑 `pytest`，开草稿 PR——全部在一条命令内完成。

> 更多可直接演示的 issue URL（来自 FastAPI、Textual、cookiecutter）见 `examples/candidate_issues.md`。

---

## 技术选型

| 决策 | 理由 |
|----------|----------|
| **LangGraph 显式状态机** | 每个相位、每条边、每次转换都在代码里可见——没有黑盒 `AgentExecutor`。条件路由是一个纯函数（`route_from_state`），而不是藏在提示词里。 |
| **Pydantic `AgentState`** | 整次运行一个类型化模型。可序列化以便调试。schema 驱动路由（控制流里没有 stringly-typed 的相位名）。 |
| **回退图引擎** | 没装 LangGraph 也能跑。`FallbackCompiledGraph` 是 30 行纯 Python，遵守同样的 `graph.ainvoke()` 契约，让离线/CI 测试变得轻松。 |
| **结构化输出 + 校验重试** | LLM 是随机的。`validate_or_retry` 捕获畸形 JSON、schema 不匹配、缺键——把确切错误注入后重试一次——让 agent 不会因一次坏解析而崩溃。 |
| **带护栏检查的 token 预算** | 每次 LLM 调用前检查预算。超额则走优雅失败路径（带部分发现的 issue 留言），而不是 HTTP 500。 |
| **重复失败检测** | 若 `verify_fix` 两次看到完全相同的补丁 + 完全相同的错误日志，则中止而非死循环。这是在最大重试上限之外的纵深防御。 |
| **GitHub 原生工作流** | 用 GitHub Contents API 推文件（不做本地 git push），开草稿 PR（不自动合并），失败时发布分析留言。 |
| **生产级 HTTP 层** | 对 429/502/503/504 及网络错误做指数退避重试（tenacity）。令牌桶限流器遵守 GitHub API 限额（认证后 4500 req/h）。通过 `logging` 模块做结构化日志。 |
| **Layer 2 单仓库记忆** | SQLite 支撑的文件索引 + issue 历史。在一个仓库修了 5 个 bug 后，agent 优先搜索历史上改动过的文件——比冷启动 GitHub API 搜索快 10 倍。原子 SQL 写入、fire-and-forget、WAL 模式。 |
| **模块化代码库** | 999 行的巨型文件拆进 `src/nodes/`（每个 agent 相位一个文件）。每个节点约 50-180 行。`src/state.py` 放所有 Pydantic 模型。`src/graph.py` 是 LangGraph 接线。 |

---

## 文档

| 文档 | 讲什么 |
|----------|---------------|
| `docs/ENGINEERING_LOG.md` | 评估驱动的调试故事：假设、埋点、被证伪的假设、以及诚实的负结果 |
| `ARCHITECTURE_V2.md` | 系统架构：LangGraph 状态机、各节点职责、reflect/retry 循环 |
| `docs/MEMORY_DESIGN_V2.md` | 四层记忆架构：工作记忆 → 单仓库 SQLite → 跨仓库策略学习 → 元统计。含并发分析与三种部署规模 |
| `docs/2026-07-05-technical-adjustments.md` | 评估驱动调试的一个完整案例：把失败归因分四层逐层剥开 |
| `BUGLOG.md` | 开发中发现并修复的真实 bug，附根因分析 |

---

## 测试

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 跑测试套件（380+ 个测试，几秒跑完）
pytest tests/ -q
```

测试覆盖：

- 完整状态机转换（UNDERSTAND → LOCATE → PLAN → EXECUTE → VERIFY → COMMIT → DONE）
- REFLECT → PLAN 重试循环
- 重复失败检测
- Token 预算耗尽
- Pydantic 校验重试逻辑
- GitHub API 工具 mock（读 issue、代码搜索、读文件）
- FastAPI 端点路由
- Tracer JSONL 日志

---

## 开发

```bash
git clone https://github.com/FMorgan-111/repopilot-view.git
cd repopilot-view
pip install -e .
pytest tests/ -q
```

### 运行 FastAPI 服务

```bash
uvicorn src.main:app --reload
```

端点：

- `GET  /health` —— 存活检查
- `POST /analyze` —— issue 分类 + 文件排序（v1）
- `POST /agent` —— 旧版 agent 循环
- `POST /agent/v2` —— 当前状态机 agent（推荐）

---

## 许可

MIT —— 见 `LICENSE`。

---

<p align="center">
  <sub>基于 LangGraph、Pydantic、httpx 与 DeepSeek v4-pro 构建。维护者 <a href="https://github.com/FMorgan-111">FMorgan-111</a>。</sub>
</p>
