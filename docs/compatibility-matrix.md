# 兼容性矩阵（Phase 0 实测）

> 状态：`VERIFIED`（本机实测）/ `PARTIAL`（部分可用）/ `BLOCKED`（缺凭据/权限/协议不支持）。
> 更新规则：只有新实测证据可把 BLOCKED/PARTIAL 升级为 VERIFIED。

## 1. 运行时平台

| 项 | 要求 | 实测 | 状态 |
|---|---|---|---|
| Python | 3.12+ | 3.12.12（/opt/anaconda3） | VERIFIED |
| uv | 任意可用 | 0.9.15，可装包 | VERIFIED |
| Git | 任意 | 2.34.1 | VERIFIED |
| systemd | user 级可用 | 249，`--user` running | VERIFIED |
| 内部 API 传输 | UDS 优先 | 可写 socket 目录：`.data/run/` | VERIFIED（非标准 /run 路径，见 assumptions） |

## 2. Reasonix（v1.21.2）ACP 能力

| 能力 | 实测 | 证据 | 状态 |
|---|---|---|---|
| initialize 握手 | 返回完整 agentCapabilities | 亲自 stdio 探测 | VERIFIED |
| session/new | 需可写 sessions 目录；REASONIX_HOME 隔离后可创建 | 隔离 home 探测 | VERIFIED（带 overlay 前提） |
| session/load | sessionCapabilities.loadSession=true | initialize 返回 | VERIFIED（能力声明） |
| session/resume | resume={} | initialize 返回 | VERIFIED（能力声明） |
| session/list | 返回 sessions[]（空列表） | 亲自探测 | VERIFIED |
| session/close | 能力声明存在 | initialize 返回 | VERIFIED（能力声明） |
| session/prompt | promptCapabilities.embeddedContext=true | initialize 返回 | VERIFIED（能力声明） |
| 模型覆盖 | sessionConfig.model 传入 | 探测（隔离 home 报 not configured 属 overlay 配置问题，非协议问题） | PARTIAL（Phase 4 落地） |
| structured output | 未在 ACP 层发现显式字段 | 待 Phase 4 深挖 | PARTIAL |
| steering | `_reasonix.io/session/steer` 扩展声明 | initialize._meta.reasonix.io | VERIFIED（能力声明） |
| cancel | session 生命周期含 close/delete；task cancel 另有 CLI | — | PARTIAL（Phase 4 落地） |
| 全局配置修改 | 禁止 | `/home` 只读，天然不可能 | VERIFIED（约束满足） |

## 3. Codex（0.146.0）App Server

| 能力 | 实测 | 证据 | 状态 |
|---|---|---|---|
| app-server 存在 | experimental，`--listen stdio/unix/ws` | `--help` | VERIFIED |
| 协议 schema | v1+v2 JSON Schema 生成成功 | /tmp/codex-schema/ | VERIFIED |
| initialize | InitializeParams(clientInfo) | v2 schema | VERIFIED |
| thread/start|resume|fork|list|read | 全部在 v2 schema | definition 枚举 | VERIFIED（schema 级） |
| turn/start（input+threadId，可选 model/effort/outputSchema/sandboxPolicy） | v2 schema | TurnStartParams | VERIFIED（schema 级） |
| turn/steer（expectedTurnId）/ interrupt（threadId+turnId） | v2 schema | TurnSteerParams/TurnInterruptParams | VERIFIED（schema 级） |
| 事件流 | ThreadStarted/TurnStarted/TurnCompleted/TurnPlanUpdated/TurnDiffUpdated 等 notifications | v2 schema | VERIFIED（schema 级） |
| 真实进程 conformance | 未执行（需授权/模型调用） | — | BLOCKED（门禁后执行） |

## 4. cc-connect（v1.4.1）

| 能力 | 实测 | 证据 | 状态 |
|---|---|---|---|
| Management API | :9820，token 认证，/api/v1/* | core/management.go、docs/management-api.md | VERIFIED（配置存在） |
| 出站发送（已有 session） | POST /api/v1/projects/{name}/send | management.go:1115 | VERIFIED（源码级） |
| 内部 unix socket | ~/.cc-connect/run/api.sock（/send、/relay/send） | core/api.go | VERIFIED（存在性） |
| 飞书卡片/更新/回调 | platform/feishu 完整实现 | feishu.go、card.go | VERIFIED（源码级） |
| ACP agent 后端 | agent/acp 已注册 | agent/acp/agent.go:17 | VERIFIED（源码级） |
| Delivery API（窄出站接口） | **不存在**；现有 /send 仅向已有 session 发文本 | 源码审计 | 需补 patch（Phase 6） |
| 真实发送 | 未执行 | — | BLOCKED（授权门禁） |

## 5. 飞书

| 能力 | 实测 | 证据 | 状态 |
|---|---|---|---|
| app 凭据存在 | cc-connect config 2 套、feishu.json 2 份、reasonix bot.feishu | 存在性审计 | VERIFIED（存在） |
| 真实 API 调用 | 未执行 | — | BLOCKED（授权门禁） |
| lark-oapi SDK | 1.7.1 在 research-agent-orchestrator venv 中；本项目经 uv 可装 | 环境审计 | VERIFIED |

## 6. 数据与路径

| 项 | 要求 | 实测 | 状态 |
|---|---|---|---|
| 数据库 | SQLite WAL | SQLAlchemy 2.0.51 已装 | VERIFIED |
| 系统路径 /var/lib/researchd 等 | 无 root | — | 不可用 → user 路径（见 assumptions） |
| XDG 路径 | ~/.local/share 等 | /home 只读 | 不可用 → `.data/` |
