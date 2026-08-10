# `researchd-agent` 最终开发执行指令

> **执行对象**：Linux 服务器上的 Reasonix 主线程  
> **目标仓库**：当前工作目录，即用户创建的空目录 `researchd-agent/`  
> **指令性质**：这是实施任务，不是架构讨论。请直接审计环境、初始化仓库、编写代码、运行测试、部署服务并启动 pilot。  
> **日期基线**：2026-08-10  
> **最终负责人**：当前 Reasonix 主线程。子 Agent 可以并行工作，但主线程对接口冻结、代码集成、测试结果和最终交付承担唯一责任。

---

## 0. 任务目标与执行原则

在当前空目录中实现一个可持续运行的科研控制系统 `researchd`。它要实现下面的闭环：

```text
PI 给定选题或 steering
    ↓
系统持续推进文献、数据、分析、审查和写作任务
    ↓
把研究状态、证据、Claim、Issue、Decision 持久化
    ↓
先自动运行廉价诊断和可并行方案
    ↓
只有遇到真正需要科学判断的分叉时才找 PI
    ↓
通过飞书发送整理后的进展、证据冲突和决策卡片
    ↓
吸收 PI 决策，只恢复受影响分支，其他任务继续
    ↓
同步项目文档并继续执行
```

本系统不是普通聊天机器人，也不是“输入题目后直接生成论文”的单次流水线。它是由持久状态、任务调度、证据追溯、人工门控和可恢复执行构成的研究操作系统。

必须遵守以下执行原则：

1. **直接实施，不再提交一份新的总体架构建议。** 只有发现已冻结设计与本机真实接口明确冲突时，才可写 ADR 说明并作最小兼容调整。
2. **先检查真实环境，后编码集成。** 本机命令帮助、实际协议握手、本地源码和官方文档优先于记忆或猜测。
3. **不要因非关键不确定性停下来询问。** 采用保守默认值，记录到 `docs/assumptions.md`，继续推进。
4. 只有以下情况可以形成实施阻塞项：缺少必须的凭据或权限、会破坏已有未提交改动、不可逆外部操作、真实协议完全不支持且无安全兼容路径。即使出现阻塞，也必须先完成不依赖该阻塞的其余工作。
5. **严禁伪造成功。** 未实际连接的飞书、未实际运行的 Executor、未通过的测试都必须标为 `BLOCKED` 或 `PARTIAL`。
6. **每一阶段形成可运行增量、测试证据和原子 Git commit。** 不允许最后一次性生成大量未经运行的代码。
7. **不暴露秘密。** 环境审计只记录配置键、路径、是否存在和脱敏后的端点，不打印 token、API key、cookie 或完整凭据。
8. **不保存或展示模型隐藏思维链。** 只保存协议事件、结构化结果、工具元数据、错误和必要的可审计摘要。

---

## 1. 当前开发 Harness：Reasonix 主线程

本次开发由 Reasonix 而非 Codex 主线程统筹。已核实配置如下，视为当前环境事实；不要随意改写用户全局配置。

### 1.1 主模型与 provider

```toml
default_model = "gateway/deepseek-v4-flash"
planner_model = "gateway/gpt-5.6-sol"
```

- 主执行模型：`gateway/deepseek-v4-flash`
- Planner：`gateway/gpt-5.6-sol`
- 主 provider：`gateway`，端点为本机网络中的 OpenAI-compatible 代理；不要把 provider secret 复制进仓库。
- 备用 provider：`deepseek`，可使用 `deepseek-v4-flash/pro`。
- 当前 gateway 池已知包含 DeepSeek、GPT-5.6 系列、GPT-5.3 Codex、GPT-5.5/5.4 系列、Claude 和 Grok 4.5 等模型；Phase 0 必须重新枚举实际可用模型，不依据本说明硬编码完整列表。

### 1.2 可调用 subagent profile

| Profile | 主要用途 | 模型 | 权限特征 |
|---|---|---|---|
| `explore` | 本机环境和代码库广搜 | `deepseek-v4-flash` | 以读取和定位为主 |
| `research` | 外部资料、协议和代码调研 | `deepseek-v4-pro` | 研究型 |
| `review` | 改动评审 | `gpt-5.6-sol` | 独立审查 |
| `security-review` | 安全与权限审查 | `gpt-5.6-sol` | 独立安全审查 |
| `reviewer` | 深度架构/实现复核 | `gateway/gpt-5.6-sol`，max | 手动、read-only |
| `deep-research` | 高难度官方资料与兼容性核实 | `gateway/gpt-5.6-sol`，max | 手动、read-only、可 web_fetch |

默认 subagent 配置与全局上限：

```text
subagent_model = gateway/deepseek-v4-flash
subagent_effort = max
max_subagent_depth = 2
max_subagent_concurrency = 32
max_parallel_writers = 16
```

Reasonix 当前的模型选择优先级为：`subagent_models` 覆盖 > task 调用参数 > profile frontmatter > `subagent_model` 默认。使用命名 profile 时要尊重这一优先级，并在开发日志中记录实际解析出的 profile/model，不要仅根据请求名称推断。

这些是上限，不是目标。实施时遵守：

- 同时运行的写入型子 Agent 默认不超过 4–6 个；
- 共享 schema、数据库模型、迁移文件和核心配置只由主线程或单一指定 Writer 修改；
- Reviewer 和 research 类 Agent优先只读，不与 Writer 争用文件；
- 并行 Writer 必须修改互不重叠的目录，或使用独立 worktree/分支；
- 主线程负责顺序合并并运行全量测试；
- 不让子 Agent 互相进行长篇对话；子 Agent 只返回接口提案、代码改动、风险或审查结论。

### 1.3 推荐的开发分工

第一轮环境审计可并行启动：

- `explore`：检查本机 Python、uv、systemd、Reasonix、Codex、cc-connect、飞书配置、目录和现有服务；
- `research`：核实本机 Reasonix ACP、Codex App Server、cc-connect 接口和飞书 SDK 的真实能力；
- `deep-research`：仅针对本地无法确认的协议细节，查官方文档；
- `reviewer`：审查本指令与初始接口划分是否存在自相矛盾；
- `security-review`：在权限模型确定后审查 threat model。

主线程不得把“已委派”当作完成。必须读取子 Agent 产物、验证事实、解决冲突并落实到代码和测试。

---

## 2. 必须区分：开发主线程与产品运行模型

本次**开发主线程**是 Reasonix；这不等于产品运行时只能使用 Reasonix。

`researchd` 运行时必须支持可替换 Executor Adapter：

- `ReasonixAdapter`：通过 Reasonix ACP；
- `CodexAdapter`：通过 Codex App Server；
- `FakeExecutor`：测试和故障注入。

所有 Planner、Worker、Auditor、Reporter-compressor 和 ACP interaction 都通过**可配置的 Executor Profile**选择适配器、模型和推理强度。代码中不得硬编码任一模型或 provider。

必须严格分离：

```text
ACP session interaction profile
    只影响飞书自然语言理解、状态说明和 steering 解析

Project execution policy
    决定 Planner、Worker、Auditor、cross-model review 等科研执行角色
```

切换飞书会话中的交互模型不得静默改变正在运行或未来的科研执行模型。项目执行策略必须通过显式配置或命令修改，并产生审计事件。

---

## 3. 冻结的产品与架构边界

### 3.1 进程拓扑

```text
┌─────────────────────────────────────────────────────────┐
│                         飞书                              │
│ 项目群｜PI Inbox｜项目文档｜决策卡片                      │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│                       cc-connect                         │
│ 飞书鉴权、入站消息、附件、卡片回调、出站消息投递          │
└───────────────┬────────────────────────▲────────────────┘
                │ ACP 入站                │ Delivery API 出站
                ▼                         │
┌─────────────────────────────────────────────────────────┐
│                    researchd service                      │
│ 状态库｜调度器｜Decision Gate｜Outbox｜Reporter            │
│ 文档投影｜Executor Adapters｜恢复与审计                    │
└───────────────┬────────────────────────┬────────────────┘
                │                        │
       Reasonix ACP Adapter       Codex App Server Adapter
                │                        │
                └────────────┬───────────┘
                             ▼
                    项目目录、数据与计算资源
```

### 3.2 三个本仓库入口

实现以下 CLI entry points：

```text
researchd service      # 长期常驻服务，唯一数据库 Writer
researchd acp          # cc-connect 启动的轻量 ACP 入站 shim
researchctl            # 运维、查询、恢复和诊断 CLI
```

`researchd acp` 只负责：

- 接收 cc-connect 转来的用户消息；
- 确定性命令解析；
- 必要时调用可配置的 interaction profile 做受约束意图分类；
- 把规范化事件提交给 `researchd service`；
- 返回接收确认、查询结果或简短解释。

它不执行长期科研任务，不持有项目权威状态，也不承担后台主动通知。

### 3.3 入站与出站分离

- **入站**：飞书 → cc-connect → ACP → `researchd acp` → `researchd service`。
- **出站**：`researchd` Transactional Outbox → cc-connect Delivery API → 飞书。

不要把后台主动报告寄托于一个永不结束的 ACP prompt，也不要把已编译报告重新作为“用户消息”送进另一个 LLM。

### 3.4 权威状态

| 内容 | 权威来源 |
|---|---|
| 项目、Task、Run、Decision、Outbox 当前状态 | SQLite |
| 事件审计 | SQLite append-only `events` |
| 代码、研究文档、已批准决策导出、阶段快照 | Git 工作区 |
| 大型数据和中间产物 | 文件系统或对象存储；数据库只存 provenance |
| 飞书项目文档 | 可编辑投影，不是主库 |
| 飞书消息 | 已发送 Report 记录，不是研究状态 |
| Executor thread/session | 可恢复执行信息，不是研究记忆 |

### 3.5 不采用的方案

第一版不得引入：

- Temporal、Celery、Redis、Kafka；
- LangGraph 作为全局状态机；
- Kubernetes 或微服务拆分；
- 一个永久不结束的模型会话作为研究记忆；
- 让多个“人格化 Agent”在群里讨论；
- 将完整 shell 日志、原始模型回复或 reasoning stream 发送飞书。

---

## 4. 技术栈

控制平面固定为：

```text
Python                 3.12+
依赖管理               uv
HTTP / internal API    FastAPI + Uvicorn
数据模型               Pydantic v2
ORM / migration        SQLAlchemy 2 + Alembic
运行数据库             SQLite（WAL）
异步模型               asyncio
HTTP client            httpx
日志                    结构化 JSON logging
测试                    pytest + pytest-asyncio
飞书 SDK                官方 Python SDK（实际包名以环境审计为准）
ACP                     官方/实际可用 Python ACP SDK；若不成熟则薄协议适配
```

SQLite 初始化要求：

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;
```

只有 `researchd service` 允许写数据库。ACP shim、CLI、Executor 和 cc-connect 必须通过内部 API 提交命令。

SQLAlchemy 模型不要依赖无法迁移到 PostgreSQL 的 SQLite 特有结构。

---

## 5. 目标仓库结构

在当前空目录中初始化 Git，并逐步形成：

```text
researchd-agent/
├── pyproject.toml
├── uv.lock
├── README.md
├── AGENTS.md
├── CHANGELOG.md
├── IMPLEMENTATION_STATUS.md
│
├── src/
│   └── researchd/
│       ├── __init__.py
│       ├── cli.py
│       ├── service.py
│       ├── config.py
│       │
│       ├── domain/
│       │   ├── project.py
│       │   ├── question.py
│       │   ├── task.py
│       │   ├── run.py
│       │   ├── artifact.py
│       │   ├── evidence.py
│       │   ├── claim.py
│       │   ├── issue.py
│       │   ├── decision.py
│       │   ├── report.py
│       │   └── events.py
│       │
│       ├── application/
│       │   ├── commands.py
│       │   ├── handlers.py
│       │   ├── decision_gate.py
│       │   ├── evidence_validation.py
│       │   ├── review_policy.py
│       │   ├── context_builder.py
│       │   └── model_policy.py
│       │
│       ├── persistence/
│       │   ├── models.py
│       │   ├── repositories.py
│       │   ├── transaction.py
│       │   └── outbox.py
│       │
│       ├── scheduler/
│       │   ├── loop.py
│       │   ├── dispatch.py
│       │   ├── leases.py
│       │   ├── budgets.py
│       │   ├── locks.py
│       │   └── reconciliation.py
│       │
│       ├── executors/
│       │   ├── base.py
│       │   ├── reasonix_acp.py
│       │   ├── codex_app_server.py
│       │   ├── fake.py
│       │   ├── profiles.py
│       │   └── schemas/
│       │       ├── planner_result.json
│       │       ├── work_result.json
│       │       └── audit_result.json
│       │
│       ├── reporting/
│       │   ├── diff.py
│       │   ├── eligibility.py
│       │   ├── spec.py
│       │   ├── builder.py
│       │   ├── lint.py
│       │   ├── compression.py
│       │   └── renderers/
│       │       ├── feishu_card.py
│       │       └── markdown.py
│       │
│       ├── integrations/
│       │   ├── delivery.py
│       │   ├── cc_connect.py
│       │   └── feishu_docx.py
│       │
│       ├── acp/
│       │   ├── agent.py
│       │   ├── inbound.py
│       │   ├── intents.py
│       │   └── session_config.py
│       │
│       ├── api/
│       │   ├── app.py
│       │   ├── dependencies.py
│       │   └── routes/
│       │
│       └── projections/
│           ├── git_export.py
│           └── feishu_document.py
│
├── migrations/
├── templates/
│   ├── project/
│   ├── reports/
│   └── cards/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── conformance/
│   ├── recovery/
│   ├── e2e/
│   └── fixtures/
├── deploy/
│   ├── systemd/
│   ├── config.example.toml
│   └── env.example
├── integrations/
│   └── cc-connect/
│       ├── README.md
│       └── patch/
└── docs/
    ├── environment-audit.md
    ├── compatibility-matrix.md
    ├── assumptions.md
    ├── architecture.md
    ├── adr/
    ├── state-machines.md
    ├── executor-protocol.md
    ├── model-configuration.md
    ├── cc-connect-integration.md
    ├── feishu-projection.md
    ├── operations.md
    ├── recovery.md
    ├── threat-model.md
    ├── requirements-traceability.md
    ├── blockers.md
    └── pilot.md
```

不要预先创建无内容文件来伪装完成度。目录和文件应随实现逐步形成。

---

## 6. 每个研究项目的工作区

项目工作区与 `researchd-agent` 代码仓库分离。模板至少包含：

```text
<project-root>/
├── INITIAL_BRIEF.md
├── PROJECT_CHARTER.md
├── research.yaml
├── questions/
├── decisions/
├── claims/
├── evidence/
├── analysis/
├── manuscript/
├── reports/
├── artifacts/
├── runs/
├── playbook/
│   ├── accepted/
│   └── proposals/
└── .research/
    ├── contexts/
    ├── projections/
    ├── snapshots/
    └── locks/
```

规则：

- `INITIAL_BRIEF.md` 不被 Agent 覆盖；
- `PROJECT_CHARTER.md` 只能经 Hard Gate 修改；
- 大型数据不提交 Git；
- 每个 Artifact 记录路径、SHA-256、大小、MIME、生成 Run、代码 commit 和数据版本；
- 已完成 Task 的历史不原地重写；发现错误时失效相关 Evidence，并创建纠正 Task。

---

## 7. 一等研究对象与状态机

第一版至少实现这些对象：

```text
Project
Question
Task
Run
Artifact
Evidence
Claim
Issue
Decision
Report
ContextPackage
PlanRevision
TasteRuleProposal
```

### 7.1 Task 状态机

```text
PROPOSED
   │ 合同校验通过
   ▼
READY ────────────────┐
   │ 获得执行租约       │ 阻塞条件解除
   ▼                   │
RUNNING ─────────► BLOCKED
   │                   ▲
   │ 结构化结果          │
   ▼                   │
REVIEW ────────────────┘
   ├── COMPLETED
   ├── READY       # 修改或重跑
   └── FAILED

任意非终态 ──► CANCELLED
```

强制不变量：

- `RUNNING` 不得直接进入 `COMPLETED`；
- 一次命令或模型调用成功不等于 Task 完成；
- 只有 Task Contract 的 success criteria 逐条验证通过后才能完成；
- Task 进入 `RUNNING` 时必须绑定当前 lease token；
- 只有当前 lease owner 能提交结果；
- 已完成 Task 不原地变回未完成，纠错通过新 Task 和 provenance 链完成。

### 7.2 Run 状态机

```text
QUEUED → STARTING → RUNNING
                      ├── SUCCEEDED
                      ├── FAILED
                      ├── INTERRUPTED
                      └── ORPHANED
```

`SUCCEEDED` 仅表示 Executor 正常返回并通过基础 schema 校验，不代表科研结果被接受。

### 7.3 Evidence 状态

```text
CANDIDATE
   ├── VERIFIED
   ├── CONTESTED
   ├── INVALID
   └── SUPERSEDED
```

Agent 的自由文本判断、Planner 建议和 Reporter 总结不能单独成为 Evidence。

文献 Evidence 至少记录：来源 ID、定位信息、实际支持的 statement、获取时间、本地快照/Artifact、限制。

计算 Evidence 至少记录：Run、Artifact、代码 commit、数据 snapshot/hash、统计量、不确定性和解释限制。

模型标注 Evidence 至少记录：rubric 版本、模型、抽样方式、校验 Artifact 和限制。

### 7.4 Claim 三维状态

```yaml
evidence_state:
  - UNTESTED
  - SUPPORTED
  - MIXED
  - UNSUPPORTED
  - CONTRADICTED

review_level:
  - NONE
  - INTERNAL
  - CROSS_MODEL
  - PI

use_state:
  - DRAFT
  - MANUSCRIPT_ELIGIBLE
  - INCLUDED
  - RETIRED
```

Claim—Evidence 关系至少支持：

```text
supports
challenges
qualifies
defines_scope
provides_context
invalidates
```

核心 Claim 进入标题、摘要或主要贡献前，至少需要跨模型审查，并由 PI 明确批准。

### 7.5 Issue 状态

```text
OPEN → INVESTIGATING
         ├── RESOLVED
         ├── ACCEPTED_RISK
         └── SUPERSEDED
```

绝大多数 Issue 先自动调查，不直接通知 PI。

### 7.6 Decision 状态

```text
CANDIDATE → OPEN → ANSWERED → APPLIED → CLOSED
                 └──────────────► WITHDRAWN（问题失效）
```

Worker 只能提出 `DecisionCandidate`。只有 Decision Gate 能创建面向 PI 的 `OPEN` Decision。

每个 Decision 必须包含：

```yaml
question:
trigger:
why_material:
options:
recommendation:
recommendation_basis:
evidence_refs:
unresolved_uncertainty:
reversibility:
blocking_scope:
continue_scope:
decision_version:
```

Decision 按钮重复点击必须幂等。

---

## 8. 科学决策门控

实现并测试以下规则：

```text
Ask PI = Material AND Unresolved AND (TasteSensitive OR HardGate)
```

### Material

至少影响一项：核心研究问题、构念、主指标含义、样本边界、识别/分析策略、核心 Claim、文章叙事、对外发布或高成本不可逆操作。

### Unresolved

Agent 已经完成：

1. 查找已批准规则；
2. 运行低成本诊断；
3. 可并行的替代方案已经并行运行；
4. 比较实际科学后果；
5. 仍不存在明显占优方案。

### TasteSensitive

不同选项体现科学重要性、解释边界、文章故事或证据呈现方面的判断，不是简单技术优劣。

### HardGate

以下操作无论模型多有把握都必须找 PI：

- 修改 Project Charter；
- 修改核心研究问题或构念；
- 在看到结果后改变主要纳入排除规则；
- 选定论文中心解释；
- 确定标题、摘要和主要贡献；
- 投稿、公开发布或共享敏感材料；
- 超出明确预算或权限；
- 删除原始数据或执行不可逆操作。

Decision fingerprint 用项目、影响对象、决策类别和规范化 option set 去重，禁止换措辞重复询问。

Decision 只阻塞 `blocking_scope`；`continue_scope` 继续执行。

---

## 9. Task Contract

每个科研 Task 必须有研究导向合同，而不是“写一个脚本”式工程任务。

```yaml
task_id: T-018
role: analysis_worker
objective: >
  判断不同学科分类层级是否改变跨学科引用变化的方向和解释。
why_now: >
  主结果依赖 field 分类，尚未知道 subfield 是否支持不同故事。
inputs:
  - E-031
  - A-012
  - D-001
deliverables:
  - field 与 subfield 结果比较表
  - 差异来源诊断
  - 是否影响核心 Claim 的判断
success_criteria:
  - id: SC-1
    text: 两套结果均可复现
  - id: SC-2
    text: 差异被量化
  - id: SC-3
    text: 明确说明差异是否改变科学解释
stop_conditions:
  - 分类缺失率超过预设阈值
escalation_conditions:
  - 主结果反号
  - 两种分类支持不同文章故事
budget:
  max_wall_seconds: 7200
  max_executor_turns: 8
  max_model_calls: 8
  max_parallel_workers: 2
```

“脚本生成成功”“运行无报错”“生成多张图”都不是科研 Task 的充分完成条件。

---

## 10. 事件、事务与 Outbox

不采用纯事件重放架构，但所有有科学意义的状态变化必须在同一 SQLite 事务中：

1. 更新当前聚合状态；
2. 追加 `events`；
3. 如需通知，插入 `outbox`；
4. 提交事务。

统一事件格式：

```json
{
  "schema": "researchd.event.v1",
  "event_id": "01K...",
  "event_type": "task.completed",
  "occurred_at": "2026-08-10T15:32:18Z",
  "project_id": "interdisciplinary-citation-pilot",
  "aggregate": {
    "type": "task",
    "id": "T-018",
    "version": 4
  },
  "actor": {
    "type": "agent",
    "role": "auditor",
    "executor": "reasonix",
    "model": "gateway/gpt-5.6-sol",
    "run_id": "R-033"
  },
  "correlation_id": "COR-009",
  "causation_id": "EVT-previous",
  "idempotency_key": "task:T-018:complete:v4",
  "payload": {}
}
```

要求：

- `idempotency_key` 唯一；
- aggregate 使用乐观版本控制；
- 事件只追加；
- Outbox sender 可重试并支持 dead-letter；
- “数据库事务已提交、消息未发出”时重启后必须补发；
- “消息已发出、receipt 尚未写入”时依赖幂等键避免重复。

---

## 11. 数据库表

至少实现：

```text
projects
project_bindings
project_members
questions
tasks
task_dependencies
runs
executor_sessions
artifacts
evidence
claims
claim_evidence
issues
decisions
decision_options
reports
context_packages
events
inbound_messages
outbox
outbox_attempts
leases
workspace_locks
projection_states
plan_revisions
taste_rule_proposals
```

通用字段：

```text
id                 ULID 字符串
project_id
version
created_at UTC
updated_at UTC
created_by
status
metadata_json
```

大对象放文件系统，数据库只保存引用和 provenance。

---

## 12. Executor 输出协议

Executor 不得返回自由格式“工作汇报”。必须返回受约束 JSON。

### 12.1 PlannerResult

允许：提出 Task、依赖、风险和计划修订。禁止直接向 PI 发消息或直接创建 Open Decision。

### 12.2 WorkResult

```json
{
  "schema": "researchd.work_result.v1",
  "task_id": "T-018",
  "outcome": "SUBMIT_FOR_REVIEW",
  "criteria_results": [
    {
      "criterion_id": "SC-1",
      "status": "PASS",
      "refs": ["artifact:a1", "evidence:e1"]
    }
  ],
  "artifacts": [
    {
      "local_ref": "a1",
      "kind": "table",
      "path": "artifacts/field_subfield_comparison.parquet",
      "description": "field 与 subfield 结果对照"
    }
  ],
  "evidence_candidates": [
    {
      "local_ref": "e1",
      "type": "analysis_result",
      "statement": "两种分类层级下总体方向一致",
      "artifact_refs": ["a1"],
      "limitations": "部分小规模学科差异较大"
    }
  ],
  "claim_changes": [],
  "issues": [],
  "decision_candidates": [],
  "next_task_proposals": []
}
```

`outcome` 仅允许：

```text
SUBMIT_FOR_REVIEW
BLOCKED
FAILED
```

`researchd` 必须验证：schema、真实文件、路径边界、hash、引用对象、success criteria、数字 provenance 和推测/证据边界。

### 12.3 AuditResult

```json
{
  "schema": "researchd.audit_result.v1",
  "task_id": "T-018",
  "verdict": "ACCEPT",
  "checks": [],
  "evidence_status_changes": [],
  "claim_status_suggestions": [],
  "issues": [],
  "decision_candidates": [],
  "revision_request": null
}
```

`verdict`：`ACCEPT | REVISE | BLOCK | REJECT`。

### 12.4 结果校验失败

- Reasonix：严格 Pydantic/JSON Schema 校验；最多两次定向修复；仍失败则 Run 失败；
- Codex：若本机 App Server 支持 `outputSchema`，在协议层约束；仍进行本地校验；
- 原始输出只写入受限 Run 目录，不发送飞书；
- 不用正则从大段 Markdown 猜科研状态。

---

## 13. Context Package

每次 Planner、Worker 或 Auditor 只获得与当前 Task 有关的有限上下文：

```text
Project Charter 摘要
当前 Task Contract
相关 Question
已批准 Decision
相关 Claim
相关 Evidence 索引
必要 Artifact 路径与 hash
明确存在的证据冲突
权限、预算和适用规则
```

默认排除：

- 以前的飞书报告；
- 原始执行日志；
- 其他 Agent 的长篇自由讨论；
- 整个聊天历史；
- 与 Task 无关的文献综述；
- Reporter 语言模板；
- 已被否决的旧计划全文。

每个 Context Package 记录：对象列表、生成时间、token estimate、因预算排除内容和内容 hash。

Taste 规则只注入报告、论文写作和叙事审查，不污染普通数据处理和证据校验。

---

## 14. 调度、租约、锁与恢复

主循环：

```python
while service_is_running:
    ingest_inbound_events()
    reconcile_expired_leases()
    apply_human_decisions()
    validate_task_proposals()
    unblock_eligible_tasks()
    dispatch_ready_tasks()
    collect_executor_results()
    validate_artifacts()
    validate_evidence_candidates()
    schedule_reviews()
    apply_audit_results()
    evaluate_decision_candidates()
    rebuild_changed_projections()
    compile_due_reports()
    flush_outbox()
```

Planner 只在项目初始化、无可执行任务、里程碑完成、重大状态变化、PI steering、Decision 应用或现有计划全部失效时调用。

允许并行：文献检索、数据审计、独立分析、稳健性测试、Claim 审查、图表审查。

写操作保守处理：

- 同一文件、同一 manuscript section 只有一个 Writer；
- Worker 优先创建新 Artifact，不覆盖已有结果；
- 合并由专门 Task 顺序完成；
- 目录锁和租约必须可在重启后清理或恢复。

Run 保存：

```text
executor
executor_profile
resolved_model
reasoning_effort
configuration_source
process_instance_id
session/thread_id
turn_id
last_event_sequence
started_at
heartbeat_at
termination_reason
```

服务重启后：

1. 过期 Run 标记 `ORPHANED`；
2. 查询对应 Reasonix session 或 Codex thread/turn；
3. 已完成则收集；
4. 可恢复则恢复；
5. 状态不明则保留已有 Artifact，创建新 Run；
6. 不覆盖旧 Run。

---

## 15. 模型与 Executor Profile 配置

### 15.1 配置优先级

```text
1. Task Contract 明确指定的 executor_profile
2. Project 对 role 的覆盖
3. 全局 role 默认
4. Adapter 默认
```

所有解析后的实际配置固化到 Run。

### 15.2 示例配置

根据 Phase 0 的能力检测调整字段，但语义保持：

```toml
[interaction]
default_profile = "frontdesk_fast"
deterministic_commands = true
allow_session_override = true
allow_natural_language_intent = true
intent_confidence_threshold = 0.85

[executor_profiles.frontdesk_fast]
adapter = "reasonix"
model = "gateway/deepseek-v4-flash"
reasoning_effort = "low"
max_turns = 1
tools = "none"

[executor_profiles.frontdesk_deep]
adapter = "reasonix"
model = "gateway/gpt-5.6-sol"
reasoning_effort = "max"
max_turns = 2
tools = "read_only"

[executor_profiles.reasonix_worker]
adapter = "reasonix"
model = "gateway/deepseek-v4-flash"
reasoning_effort = "max"

[executor_profiles.reasonix_research]
adapter = "reasonix"
model = "gateway/deepseek-v4-pro"
reasoning_effort = "max"

[executor_profiles.reasonix_review]
adapter = "reasonix"
model = "gateway/gpt-5.6-sol"
reasoning_effort = "max"
tools = "read_only"

[executor_profiles.codex_worker]
adapter = "codex"
model = ""
reasoning_effort = "high"
sandbox = "workspaceWrite"

[roles]
interaction = "frontdesk_fast"
planner = "reasonix_review"
worker_default = "reasonix_worker"
literature_worker = "reasonix_research"
analysis_worker = "reasonix_worker"
auditor = "reasonix_review"
cross_model_reviewer = "reasonix_review"
report_compressor = "frontdesk_fast"
manuscript_writer = "reasonix_worker"
```

这只是默认示例，不得把它写死。若 Reasonix ACP 不支持 session 级模型切换：

- 不得直接修改用户全局 `~/.reasonix` 配置；
- 优先使用 ACP config option、会话参数或受支持的 task 参数；
- 若协议只允许进程级配置，则实现“命名 Reasonix 实例/命令模板 + 隔离配置 overlay”；
- overlay 放入受限运行目录并只覆盖必要键；
- 每个实例启动时做 capability negotiation；
- 无法实现某字段时记录 capability 为 false，使用明确 fallback，不假装支持。

### 15.3 `researchd acp` 的可配置交互模型

`researchd acp` 必须支持：

- 确定性命令优先；
- `interaction_profile`：`fast | deep | deterministic`；
- `interaction_reasoning`；
- 若 cc-connect 能显示 ACP session `configOptions`，则原生暴露；
- 若不能显示，则通过 `/research model` 和 `/research config` 命令提供相同能力；
- 会话覆盖只影响 interaction，不改变 Project execution policy。

实现命令：

```text
/research model
/research model interaction fast
/research model interaction deep
/research model interaction deterministic
/research config show
/research config set role.<role> <profile>
```

修改项目执行 profile 只影响未来 Run，除非用户显式要求取消并重建当前 Run。每次修改写入 `project.executor_policy_changed` 事件。

### 15.4 Cross-model 审查

不能仅依据 profile 名称认定“跨模型”。必须比较 `resolved_model` 的模型家族。核心 Claim 审查至少使用不同于主 Worker 的模型家族；若本机可用，可用 GPT/Claude/Grok 对 DeepSeek 工作结果复核。选择由 profile 配置，不硬编码。

---

## 16. Reasonix Adapter

通过实际核实的 Reasonix ACP/CLI 接入，不解析 TUI 屏幕。

Adapter 需实现或明确标记能力：

```yaml
supports_session_new:
supports_session_load:
supports_session_resume:
supports_steering:
supports_cancel:
supports_structured_output:
supports_model_override:
supports_reasoning_override:
supports_tool_approval:
```

要求：

- 启动时完成 handshake 和 capability negotiation；
- 保存 session id、进程实例、消息序列和退出原因；
- JSON contract 本地强校验；
- 校验失败最多两次修复；
- 支持取消、恢复和失联 reconciliation；
- 不把 Reasonix 的全局配置复制进项目；
- 不把 API key 写入日志或 Artifact；
- Adapter conformance tests 同时覆盖 fake transport 和本机真实进程。

---

## 17. Codex Adapter

Codex 是可替换的一等运行时 Executor，即使本次开发主线程是 Reasonix，也必须实现或在真实缺失时完整保留接口、fake conformance 和明确 blocker。

通过本机实际 `codex app-server` 能力实现：

- initialize；
- thread start/resume/fork；
- turn start/steer/interrupt；
- 监听 turn 和工具事件；
- 输出 schema；
- sandbox/approval 策略；
- Run provenance 和恢复。

不要假设某个文档版本一定与本机版本一致。先用 `--help`、协议握手和最小试调用确认。

---

## 18. 内部 API 与 CLI

优先使用 Unix Domain Socket：

```text
/run/researchd/researchd.sock
```

无权限时使用：

```text
$XDG_RUNTIME_DIR/researchd/researchd.sock
```

只有在 UDS 不可行时绑定 `127.0.0.1:<port>`，并要求 Bearer Token。

核心 API：

```http
POST /v1/inbound/messages
GET  /v1/projects
POST /v1/projects
GET  /v1/projects/{project_id}/status
POST /v1/projects/{project_id}/pause
POST /v1/projects/{project_id}/resume
POST /v1/projects/{project_id}/cancel
GET  /v1/projects/{project_id}/tasks
GET  /v1/projects/{project_id}/decisions
POST /v1/decisions/{decision_id}/answer
POST /v1/projects/{project_id}/commands
POST /v1/projects/{project_id}/sync
POST /v1/reconcile
GET  /healthz
GET  /readyz
```

`researchctl` 至少实现：

```text
researchctl project list
researchctl project status <project-id>
researchctl task list <project-id>
researchctl decision list --open
researchctl pause <project-id>
researchctl resume <project-id>
researchctl reconcile
researchctl outbox retry
researchctl export <project-id>
researchctl doctor
```

规范化入站消息：

```json
{
  "schema": "researchd.inbound_message.v1",
  "message_id": "platform-message-id",
  "idempotency_key": "feishu:platform-message-id",
  "platform": "feishu",
  "cc_project": "research",
  "cc_session_key": "session-key",
  "actor": {
    "platform_user_id": "ou_xxx",
    "display_name": "PI"
  },
  "text": "/decision D-002 B --version 3",
  "attachments": [],
  "received_at": "2026-08-10T08:30:00Z"
}
```

第一版确定性命令：

```text
/research bind project <project-id>
/research bind inbox
/research status
/research pause
/research resume
/research digest
/research sync
/research model
/research config show
/research config set role.<role> <profile>
/decision <decision-id> <option-id> --version <n>
/explain <object-id>
/task <task-id>
/claim <claim-id>
```

普通自然语言先由 interaction profile 转为候选 intent，再由 application handler 检查目标唯一性、权限、版本和安全性。LLM 不得直接写数据库。

---

## 19. cc-connect 集成

### 19.1 现状审计

Phase 0 必须定位：

- cc-connect 的安装方式、版本、源码路径和当前分支；
- 工作区是否有未提交改动；
- 当前 ACP Agent 配置；
- 当前 Management API 和发送语义；
- 卡片按钮回调和消息更新能力；
- 是否已有等价 Delivery API；
- 飞书 app credential 的加载方式。

若源码目录有未提交改动，不得覆盖。创建独立分支、worktree 或只生成 patch。

### 19.2 最小 Delivery API

若当前版本没有满足需求的确定性出站接口，则增加窄接口：

```http
POST  /api/v1/projects/{project}/deliveries
PATCH /api/v1/projects/{project}/deliveries/{platform_message_id}
```

请求至少包含：

```json
{
  "session_key": "project-session",
  "idempotency_key": "project:RPT-024:v1",
  "kind": "interactive_card",
  "payload": {},
  "attachments": [],
  "reply_to": null,
  "metadata": {
    "project_id": "interdisciplinary-citation-pilot",
    "report_id": "RPT-024"
  }
}
```

要求：

- 只监听 UDS/localhost；
- 强 token；
- 幂等；
- 返回 platform message ID；
- 支持更新已有消息；
- 不经过 LLM；
- 不读取研究状态；
- 复用 cc-connect 飞书鉴权、卡片和附件能力；
- 保持小补丁、可独立上游提交；
- 在本仓库 `integrations/cc-connect/patch/` 保存 patch、安装方法和兼容性说明。

### 19.3 决策按钮

按钮 payload 使用确定性命令，例如：

```text
cmd:/decision D-002 A --version 3
```

流程：

1. cc-connect 临时将卡片更新为“已收到，正在应用”；
2. 回调经 ACP 进入 `researchd`；
3. 校验 Decision ID、version、actor 和幂等键；
4. 事务中应用 Decision、恢复相关 Task、写 event 和 outbox；
5. Outbox 将原卡片更新为最终“已应用”；
6. 重复点击返回当前状态，不重复执行。

---

## 20. 飞书界面与文档投影

### 20.1 三个视图

1. **项目群**：项目决策、异常、里程碑、图表和文件；
2. **PI Inbox**：跨项目阻塞 Decision、严重异常和定时摘要；
3. **项目文档**：当前研究状态的可读投影。

不展示：实时 shell 日志、Worker 开始消息、原始模型输出、思维链、每次自动重试。

### 20.2 项目文档结构

```text
项目名称 / 当前阶段 / 最后同步时间
1. 研究问题与边界
2. 当前最可信的结论
3. 证据与反证
4. 尚未解决的问题
5. 待 PI 决策
6. 正在执行的工作
7. 关键图表与产物
8. 方法与数据状态
9. 论文草稿入口
10. 最近变更
11. PI Notes
```

`projection_states` 保存 document、section、block id 和 hash。只更新变化区块，不整篇重写。

- `PI Notes` 永不自动覆盖；
- 系统管理区块被人工改动时，生成 `human_patch` 事件；
- 能映射为 Claim、PlanRevision 或 steering 时提交候选变更；
- 无法自动归类时创建 Advisory Issue；
- 飞书文档不是主库。

聊天和卡片继续由 cc-connect 统一投递；`researchd` 可使用同一飞书 app identity 调用 Docx API，但不得创建第二个聊天机器人。

---

## 21. Reporter：禁止 AI slop 的实现方式

Reporter 不是“博士生人格”，而是受约束编译管线：

```text
State Snapshot Diff
    ↓
Report Eligibility
    ↓
Deterministic ReportSpec
    ↓
可选的受约束语言压缩
    ↓
Report Linter
    ↓
Feishu Renderer
    ↓
Transactional Outbox
```

### 21.1 只有这些变化可触发报告

- 新的 Verified Evidence；
- Claim 状态改变；
- 新的实质性 Issue；
- Blocking Decision；
- Milestone；
- 需要告知的异常；
- 定时 Digest 到期且确有内容。

以下不构成报告理由：完成一条命令、下载文件、Worker 开始、自动重试、代码重构、未经验证的想法。

### 21.2 ReportSpec

```json
{
  "schema": "researchd.report_spec.v1",
  "type": "MILESTONE",
  "title": "参考文献覆盖审计完成",
  "bottom_line": {
    "text": "两个时期的识别率差异不足以单独解释当前观察结果。",
    "evidence_refs": ["E-041", "E-042"]
  },
  "conflicts": [],
  "uncertainties": [
    {
      "text": "早期会议论文覆盖可能影响计算机科学结果。",
      "issue_ref": "I-009"
    }
  ],
  "active_actions": [
    {
      "task_id": "T-024",
      "text": "进行会议论文分层敏感性分析"
    }
  ],
  "decision_id": null
}
```

语言模型只允许压缩已有字段，不得新增结论、删除不确定性、改变 Evidence 引用、决定按钮或修改消息类型。压缩失败时回退到确定性模板。

### 21.3 Linter

发送前至少检查：

1. 每个结论有可用 Evidence；
2. Candidate Evidence 没有被写成已验证事实；
3. 相关性没有被写成因果；
4. “当前动作”对应真实 Task；
5. 没有把普通工程问题伪装为 PI Decision；
6. 没有重复近期报告中的空洞固定表达；
7. 没有无具体 Task 的“下一步继续深入”；
8. 没有与本次状态差异无关的背景复述；
9. 消息长度符合平台限制；
10. 删除任一句后若信息不变，则该句应被删除。

结构化标题可以固定；需要禁止的是无信息量的礼貌、安抚、自我评价、伪诊断和工程黑话。

### 21.4 Taste Ledger

用户编辑只先生成 `TasteRuleProposal`，包含原文、修改、推断规则、scope、confidence 和来源。未批准不得全局生效。

---

## 22. 安全与权限

推荐系统路径；若无 root/sudo，则等价使用 XDG user 路径并记录实际部署：

```text
service user             researchd
config                   /etc/researchd/config.toml
secrets                  /etc/researchd/researchd.env
database                 /var/lib/researchd/researchd.db
runtime socket           /run/researchd/researchd.sock
logs                     journald
```

Secrets 权限 `0640` 或更严格。

Artifact path 必须：

- 解析后位于注册项目根目录；
- 拒绝 `..` 路径逃逸；
- 拒绝指向项目外的 symlink；
- 检查大小、MIME 和 hash；
- 必要时做敏感内容标记。

Executor 默认不可访问：

- 飞书 token；
- cc-connect token；
- researchd 数据库文件；
- 其他项目；
- SSH 私钥；
- 与任务无关的 provider secrets。

项目 Task 通过受控 sandbox、工作目录和工具权限获得最小能力。

必须编写并由 `security-review` 审查 `docs/threat-model.md`。

---

## 23. 分阶段实施与 Git 提交

每个阶段：先实现最小可运行切片，运行相关测试，修复失败，更新 `IMPLEMENTATION_STATUS.md`，再提交。不要在测试红色时继续堆叠后续功能。

### Phase 0：环境审计与兼容性矩阵

执行：

- `pwd`、目录权限和磁盘；
- Python、uv、git、systemd/user systemd；
- Reasonix 版本、help、ACP handshake、配置位置和 capability；
- Codex 版本、App Server help/最小握手；
- cc-connect 版本、源码、服务、接口、工作区状态；
- 飞书 app 配置是否存在、权限范围和文档 API 可用性；
- 现有 provider/model 配置是否可由当前进程使用；
- 可用项目根路径和备份位置。

产物：

```text
docs/environment-audit.md
docs/compatibility-matrix.md
docs/assumptions.md
docs/blockers.md
```

只记录脱敏信息。环境审计后继续实施，不等待用户确认。

建议 commit：

```text
chore: audit environment and scaffold researchd
```

### Phase 1：领域模型、迁移和持久层

实现 Pydantic schema、SQLAlchemy models、Alembic、状态转换、events、transactional outbox、FakeExecutor 和单元测试。

建议由一个 Writer 负责核心 models；`reviewer` 独立审查状态不变量。

建议 commit：

```text
feat: implement durable research state and domain invariants
```

### Phase 2：Service、API、CLI 与 ACP shim

实现 FastAPI/UDS、`researchd service`、`researchd acp`、`researchctl`、确定性命令、项目绑定、interaction profile 抽象和 fake 端到端入站测试。

建议 commit：

```text
feat: add service api cli and acp ingress
```

### Phase 3：Scheduler、租约、预算、锁与恢复

实现依赖调度、leases、workspace locks、budgets、orphan reconciliation、Outbox retry/dead-letter 和故障注入测试。

建议 commit：

```text
feat: add durable scheduling leases and recovery
```

### Phase 4：Reasonix ACP Adapter

优先实现，因为本次环境已经核实。完成真实 handshake、session lifecycle、结构化结果、修复 turn、取消/恢复、profile/capability 处理和 conformance tests。

建议 commit：

```text
feat: integrate reasonix as configurable executor
```

### Phase 5：Codex App Server Adapter

实现真实可用能力；若本机缺失必须保留完整 adapter interface、fake transport、测试和明确 blocker，不得伪造 conformance pass。

建议 commit：

```text
feat: add codex app server executor adapter
```

### Phase 6：Decision Gate、Reporter 与 cc-connect DeliveryPort

实现 state diff、eligibility、ReportSpec、Linter、卡片 renderer、Decision Gate、cc-connect 最小 patch、幂等发送和卡片更新。

先用 FakeDeliveryPort 通过集成测试，再接真实 cc-connect。

建议 commit：

```text
feat: add scientific decision gating and reliable reporting
```

cc-connect 外部仓库补丁单独提交，不把其源码复制进本仓库。

### Phase 7：飞书文档投影

实现 Docx adapter、section/block mapping、增量同步、PI Notes 保护、human patch detection 和 fake/real tests。

建议 commit：

```text
feat: add feishu project document projection
```

### Phase 8：Pilot 与黄金路径

创建真实 pilot，并另建隔离的 deterministic e2e fixture。不要把测试构造的假 Evidence 注入真实 pilot。

建议 commit：

```text
feat: bootstrap pilot and end-to-end research loop
```

### Phase 9：部署、备份和运维

实现 systemd（系统级或 user-level）、EnvironmentFile、health checks、日志、备份/恢复、rollback 和 runbook。

建议 commit：

```text
ops: add deployment backup and recovery tooling
```

### Phase 10：最终审查

并行调用：

- `review`：全仓 diff/实现审查；
- `reviewer`：架构与需求追踪审查；
- `security-review`：权限、secret、路径、API 暴露审查；
- `deep-research`：只复核仍存在不确定的协议兼容点。

修复问题，运行全部测试和实际服务验证，形成最后 commit：

```text
fix: close final integration and security findings
```

不要为追求固定 commit 名而制造空 commit；以上名称是推荐语义。

---

## 24. Pilot 定义

真实 pilot：

```yaml
project_id: interdisciplinary-citation-pilot
question: >
  比较 2017–2019 与 2021–2023 年论文参考文献的学科组成和跨学科引用份额变化，
  描述疫情前后知识来源结构发生了什么变化。
decision:
  id: D-001
  answer: A
  meaning: >
    研究采用描述性定位，不把前后差异直接解释为疫情的因果效应。
  status: APPLIED
```

首批 Task 可由 Planner 生成，但至少覆盖：

```text
T-001 概念与指标边界
T-002 相关文献和争议
T-003 候选数据源与引用覆盖审计
T-004 field/subfield 分类比较
T-005 最小年度趋势样本
```

Pilot 的目的主要是验证系统行为，不要求在本次开发任务中完成整篇论文。

同时建立 `tests/fixtures/golden_research_project/`，用确定性 FakeExecutor 制造：

- 一个 schema 失败并自动修复；
- 两个分析先出现可能冲突；
- 系统自动运行廉价诊断；
- 冲突仍然影响科学故事；
- 生成 D-002；
- 只阻塞相关分支；
- Decision 回答后恢复；
- 生成 Verified Evidence、Claim 更新、Milestone；
- 重启后不重复发消息。

测试 fixture 必须明确标为 synthetic，不得导入真实项目 Evidence。

---

## 25. 必须通过的测试矩阵

### 25.1 状态机

- 非法跳转被拒绝；
- `RUNNING → COMPLETED` 无法绕过 `REVIEW`；
- Run 成功不等于 Task 完成；
- 完成 Task 的纠正通过新 Task；
- 乐观并发冲突不静默覆盖。

### 25.2 幂等

重复以下输入只应用一次：

- 同一飞书消息；
- 同一 Decision 按钮；
- 同一 Executor 结果；
- 同一 Outbox delivery；
- 同一 Decision answer；
- 重启后的重复回调。

### 25.3 恢复

故障注入：

- service 在 Worker 运行时退出；
- Reasonix 进程中断；
- Codex App Server 中断；
- 数据库提交后、消息发送前退出；
- 消息发出后、receipt 写入前退出；
- Artifact 写入中断；
- 文档同步中断。

恢复后必须明确归类为已完成、重试、ORPHANED 或调查，不能出现“看起来完成但无证据”。

### 25.4 Evidence 与 Claim

- 不存在的 Artifact 无法注册；
- 无 Run/code/data provenance 的计算结果不能 VERIFIED；
- Candidate Evidence 不能支持正式 Milestone；
- CONTRADICTED Claim 不能无标记进入稿件；
- 核心 Claim 未跨模型审查时不能进入标题/摘要候选。

### 25.5 Decision Gate

- 工程错误自动解决，不找 PI；
- 可廉价并行的方案先并行；
- 只有数值差异时不询问；
- 支持不同科学故事时生成 Decision；
- Decision 只阻塞依赖分支；
- 相同问题不重复询问；
- Hard Gate 必须找 PI；
- 失效 Decision 可 WITHDRAWN。

### 25.6 Reporter

- 无状态差异不发送；
- Executor 原始输出和执行日志不进入飞书；
- 每个结论有 Evidence ref；
- “下一步”有真实 Task；
- 决策卡说明实际科学后果；
- Linter 阻止空洞套话和无证据自评；
- 压缩模型失败时可回退确定性模板。

### 25.7 模型配置

- interaction profile 可配置；
- 会话切换 interaction 不改变 project role policy；
- project role override 只影响未来 Run；
- 每个 Run 记录 resolved model/profile/source；
- Reasonix 不支持某能力时 fallback 明确；
- 不修改用户全局 Reasonix 配置；
- cross-model review 实际使用不同模型家族。

### 25.8 cc-connect 与飞书

- 项目群绑定；
- PI Inbox 绑定；
- 后台消息不依赖活跃 ACP prompt；
- 按钮只应用一次；
- 原卡片可更新；
- Delivery retry 不重复；
- 文档只更新变化 block；
- 人工修改不被静默覆盖；
- 缺少 token 时 Delivery API 拒绝启动。

### 25.9 安全

- 路径穿越和 symlink 逃逸被拒绝；
- Executor 无法读取 researchd secrets；
- API 不可被远程未认证访问；
- 日志不含 token；
- Agent 不能直接写 SQLite；
- 未授权对外发布被 Hard Gate 阻止。

---

## 26. 端到端黄金路径

必须实际跑通以下 deterministic 路径：

```text
1. 建立测试项目和项目群/假 Delivery 绑定
2. 导入 D-001 = A
3. Planner 生成首批 Task
4. 多 Worker 并行
5. 一个 Worker 返回无效 Schema
6. 系统自动修复或重试，不通知 PI
7. 两个分析产生解释冲突
8. 系统先运行廉价诊断
9. 冲突仍实质性
10. Decision Gate 生成 D-002
11. 只暂停依赖 D-002 的分支
12. 其他 Task 继续
13. 发送一张决策卡
14. 重复点击相同选项
15. 只应用一次并更新原卡片
16. 受阻 Task 恢复
17. Evidence 与 Claim 通过审查
18. 发送 Milestone
19. 项目文档增量更新
20. 强制重启 researchd
21. 从 SQLite、Run 和 session 恢复
22. 不重复发送任何旧消息
```

然后在真实环境具备凭据时，再完成真实 cc-connect/飞书烟雾测试。若凭据或 chat id 缺失，不能伪造真实通过；保留 deterministic e2e 的 PASS，并把真实平台项标为 BLOCKED，给出精确完成命令。

---

## 27. 部署和运维

优先部署为 systemd service。没有 root 权限时使用 user systemd，不把权限不足当作放弃部署的理由。

必须提供：

- service unit；
- EnvironmentFile 示例；
- 数据库初始化和 migration 命令；
- health/ready check；
- 日志查看；
- graceful stop；
- 自动 restart；
- SQLite 在线安全备份；
- 项目状态导出；
- 恢复演练；
- cc-connect patch 安装/回滚；
- 服务和配置权限检查；
- `researchctl doctor`。

不得把 secret 放进 Git。

---

## 28. 明确禁止事项

1. 不要在完成前只返回“建议的架构”。
2. 不要硬编码模型名、provider URL、token、chat id 或项目路径。
3. 不要修改用户全局 Reasonix 配置作为运行时模型切换方案。
4. 不要让飞书会话模型切换改变科研角色配置。
5. 不要让 LLM 直接执行 SQL 或写状态库。
6. 不要让 Worker 直接向 PI 发消息。
7. 不要把自然语言分析本身当作 Evidence。
8. 不要把原始 Executor 输出、工具日志或思维链发到飞书。
9. 不要让 Reporter 自由生成事实或按钮。
10. 不要在没有真实 Artifact、Run 和 provenance 时标记 Evidence VERIFIED。
11. 不要在没有真实接口测试时声称 Reasonix/Codex/飞书兼容。
12. 不要创建大型 cc-connect fork；保持窄补丁。
13. 不要在共享文件上并行写入造成竞态。
14. 不要整篇重写飞书项目文档。
15. 不要删除用户已有文件、数据或未提交改动。
16. 不要用大量模板化开场、安抚语、自我评价或工程性日志充当研究汇报。

---

## 29. 完成判据

只有以下内容被逐项验证后，才能将 v0.1 标为完成：

```text
□ 空库可执行全部 Alembic migration
□ 单元、集成、conformance、recovery、e2e 测试通过
□ Reasonix Adapter 真实 capability tests 通过
□ Codex Adapter 按本机可用程度真实验证或明确 BLOCKED
□ interaction model 和 project role profiles 可配置
□ cc-connect 补丁实际安装验证，或在缺权限时提供可应用 patch 和真实 blocker
□ 飞书决策卡可发送、点击、幂等应用并原地更新，或准确标记平台 blocker
□ 飞书项目文档可增量同步，或准确标记权限 blocker
□ systemd/user-systemd 可启动、停止、自动恢复
□ deterministic 黄金路径完整通过
□ 真实 pilot 已创建并至少开始首批任务
□ 数据库和项目目录备份恢复演练通过
□ 不存在原始 Executor 输出直达飞书的代码路径
□ threat model、运维、恢复、回滚和模型配置文档齐全
□ requirements-traceability 将每项冻结要求映射到代码和测试
□ 所有改动有 Git commit，工作树状态明确
```

不能用“基本完成”“理论上可用”替代测试证据。

---

## 30. 最终交付报告格式

最终向用户汇报时只给经过核实的实施结果，包含：

```text
1. 仓库绝对路径
2. 当前分支与 commit 列表/最终 commit
3. Python/Reasonix/Codex/cc-connect 实际兼容性结论
4. researchd 服务状态和启动方式
5. SQLite 路径、项目根目录和配置路径
6. 测试命令、通过数量和失败项
7. Reasonix/Codex Adapter 的真实能力矩阵
8. cc-connect 修改位置、patch/commit 和回滚方法
9. 飞书项目群、PI Inbox、项目文档的真实绑定状态
10. Pilot 当前状态、首批 Task 和已应用 D-001
11. 已完成的黄金路径证据
12. 仍未满足的验收项，逐项标记 PARTIAL/BLOCKED 和原因
13. 用户需要执行的唯一必要后续动作（如必须提供 chat id 或权限）
```

不要输出大段开发日志，不要隐藏失败项，也不要声称后台稍后继续。

---

## 31. 立即开始

现在从当前目录开始执行：

1. 确认目录和 Git 状态；若确为空且不是 Git 仓库，执行 `git init`；
2. 并行完成 Phase 0 的本机审计；
3. 生成最小 `pyproject.toml`、`AGENTS.md` 和环境审计文档；
4. 用 Planner 将本指令转成分阶段任务图，但不得改变冻结边界；
5. 从 Phase 1 开始持续实施；
6. 每个阶段测试通过后提交；
7. 主线程持续集成，不在每个阶段停下来等待用户批准；
8. 直到完成判据被验证，或仅剩确实需要外部凭据/权限的 blocker。

**不要回复一份新的计划。直接开始检查、编码、测试和提交。**

