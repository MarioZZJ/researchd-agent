# 环境审计报告（Phase 0）

> 审计日期：2026-08-10。只记录键名、路径、存在性和脱敏端点；不含任何 secret 值。

## 1. 系统与容器

| 项 | 值 |
|---|---|
| OS | Linux/amd64，Ubuntu 24.04 风格容器（`/etc/os-release`） |
| 权限 | `uid=3001(zhengzj22)`，无 sudo（`no new privileges` 标志，sudo.conf 属主异常） |
| systemd | systemd 249，**user systemd 可用**（`systemctl --user is-system-running` = running） |
| 挂载 | `/home` 整盘 **只读**（ext4 ro）；`/tmp` rw；`/home/zhengzj22/Documents/researchd-agent` rw（独立挂载）；`~/.cache` rw；`/run/user/3001` 不可写；`/var/tmp` 不可写 |
| 磁盘 | /home 分区 60T，已用 5.4T，可用 52T |
| 可写位置 | `/tmp`、`researchd-agent` 工作区、`~/.cache` |

**推论**：XDG 数据路径（`~/.local/share` 等）不可写。researchd 的运行时数据（SQLite、runs、projects、sockets）必须放在本工作区内（`.data/`，gitignored）或 `/tmp`。这是部署文档的重要前提。

## 2. 工具链

| 工具 | 版本 | 路径 | 备注 |
|---|---|---|---|
| python3 | 3.10.12 | /usr/bin | 系统默认，不用 |
| python3.12 | 3.12.12 | /opt/anaconda3/bin/python3.12 | **本项目解释器（uv 管理）** |
| uv | 0.9.15 | ~/.local/bin/uv | 依赖管理可用，网络可达 pypi |
| git | 2.34.1 | /usr/bin/git | 仓库已初始化 |
| pip | 不存在 | — | 一律用 uv |

## 3. Reasonix（开发 harness 与运行时 Executor）

| 项 | 值 |
|---|---|
| 版本 | v1.21.2（node v24.18.1，~/.nvm/versions/node/v24.18.1/bin/reasonix） |
| 全局配置 | `~/.reasonix/config.toml`（config_version 5，**只读挂载上，不可修改**） |
| provider | `gateway`（openai kind，`http://10.0.0.2:48761/v1`，15 个模型：deepseek-v4-flash/pro、gpt-5.6-sol/terra/luna、gpt-5.3-codex-spark、gpt-5.5/5.4/5.4-mini、claude-*、grok-4.5；api_key 内联在全局 config.toml 中）；`deepseek`（api.deepseek.com，key 存在） |
| 默认模型 | `gateway/deepseek-v4-flash`；planner `gateway/gpt-5.6-sol` |
| subagent | `subagent_models = { review/security_review = gpt-5.6-sol, research = deepseek-v4-pro, explore = deepseek-v4-flash }` |
| sandbox | bash=enforce，write_roots=`~/Documents/researchd-agent` |
| 全局 `.env` | `~/.reasonix/.env` 是字符设备（/dev/null），实际凭据内联在 config.toml |

### Reasonix ACP 真实握手结果（亲自探测）

- `reasonix acp` stdio JSON-RPC；`initialize` 返回：
  - `agentCapabilities.loadSession=true`；`sessionCapabilities={list,resume,close,delete}`；
  - `promptCapabilities={image:false,audio:false,embeddedContext:true}`；
  - `mcpCapabilities={http:true,sse:false}`；
  - 扩展：`reasonix.io.sessionSteer → _reasonix.io/session/steer`、`sessionReloadExtensions`、`extensionSurface`。
- `session/new` 在默认 HOME 下失败：`mkdir ~/.reasonix/sessions: read-only file system`。
- **`REASONIX_HOME` 环境变量可重定向配置与 sessions 目录**（`REASONIX_HOME=/tmp/x reasonix doctor` 验证）。隔离 home 下 `session/new` 的 lease 创建成功，仅因缺 provider 配置报 `model not configured`。
- 结论：ReasonixAdapter 采用"隔离 REASONIX_HOME overlay"（把全局 config.toml 复制到受限运行目录 `.data/rx-overlay/`，key 不进入仓库、不修改全局），符合 IMPLEMENTATION.md §15.2 的进程级配置路径。

### 其他可用接口（备选/补充）

- `reasonix run`、`reasonix -p`（print 模式，json/stream-json 输出）
- `reasonix session list/show/status/recovery --json --dir PATH`
- `reasonix task list/show/status/events/stop/cancel --json`

## 4. Codex CLI / App Server

| 项 | 值 |
|---|---|
| 版本 | codex-cli 0.146.0（~/.local/bin/codex） |
| app-server | `codex app-server`（experimental）：`--listen stdio://|unix://|ws://`；子命令 daemon/proxy/generate-ts/generate-json-schema |
| 协议 schema | 已生成：`/tmp/codex-schema/`（v1 + v2，537 个 definition） |
| 协议形态 | JSON-RPC over stdio；v2 含完整 thread/turn 生命周期 |
| v2 关键方法 | `initialize`；thread: `thread/start`(ThreadStartParams: model/config/cwd/sandbox/approvalPolicy/baseInstructions)、`thread/resume`、`thread/fork`、`thread/list`、`thread/read`、`thread/rollback`、`thread/compact/start`；turn: `turn/start`(required: input+threadId；可选 model/effort/outputSchema/sandboxPolicy)、`turn/steer`(required: expectedTurnId)、`turn/interrupt`(threadId+turnId)；notifications: ThreadStarted/TurnStarted/TurnCompleted/TurnPlanUpdated/TurnDiffUpdated/ThreadStatusChanged/… |
| daemon 状态 | `~/.codex/app-server-daemon/` 有 pid/log（2026-08-09 活跃记录）；`app-server-control/` 有 control socket 痕迹 |

## 5. cc-connect

| 项 | 值 |
|---|---|
| 版本 | v1.4.1（Go；二进制 ~/.local/bin/cc-connect，npm 包装 → ~/.local/lib/node_modules/cc-connect/run.js） |
| 源码 | `/home/zhengzj22/cc-connect`（git，**detached HEAD 5d4c96d，工作树干净**） |
| 结构 | `cmd/cc-connect/`、`core/`（engine.go、management.go、api.go、webhook.go、bridge.go）、`platform/{feishu,...}`、`agent/{acp,codex,...}`、`daemon/`、`docs/management-api.md` |
| Management API | `:9820`，token 认证（Bearer/query），端点：status/restart/reload/config/settings/projects/sessions/send/providers/cron/heartbeat/bridge/agents/skills/setup |
| 出站发送 | `POST /api/v1/projects/{name}/send`（向**已有 session** 发消息，不建 session）；内部 unix socket `~/.cc-connect/run/api.sock` 有 `/send`、`/relay/send`；CLI `cc-connect send` |
| 飞书能力 | `platform/feishu/`：卡片（enable_feishu_card/progress_style=card）、`UpdateMessage`/`updateCardEntity`（消息原地更新）、按钮回调 `card.action.trigger`（官方 SDK dispatcher）、国内 WebSocket 长连接模式、Lark 国际 webhook 模式（encrypt_key） |
| ACP 后端 | `agent/acp/`（agent.go、session.go、rpc.go、mapping.go）已注册 `core.RegisterAgent("acp", New)`；配置中项目 agent.type 可用 "acp" |
| systemd | `~/.config/systemd/user/cc-connect.service` 已注册并 **enabled**（Restart=always） |
| 工作区规则 | 外部仓库未提交改动不可覆盖 → researchd 的 Delivery API 以 **patch 文件**形式输出到 `integrations/cc-connect/patch/`，不改动源码工作树 |

## 6. 飞书凭据存在性（只记存在性）

- `~/.cc-connect/config.toml`：每项目 `platforms.options` 内存在 app_id/app_secret（共 2 套，值未读）。
- `~/.config/research-agent-orchestrator/`：`research-assistant-agent.feishu.json`、`testbot.feishu.json` 存在。
- `~/.reasonix/config.toml`：`[bot.feishu]` 有 app_id，`app_secret_env="REASONIX_FEISHU_APP_SECRET"`（`~/.reasonix/.env` 存在但为 /dev/null 设备）。
- 环境变量：无 FEISHU/LARK 键。
- **结论**：凭据存在，但使用凭据做真实发送属于外部操作，必须走授权门禁。首版以 FakeDeliveryPort 完成确定性验证，真实平台项标记 BLOCKED(需门禁/chat id)。

## 7. 现有服务与生态（user systemd，勿干扰）

- `cc-connect.service`（enabled）：`~/.local/bin/cc-connect --config ~/.cc-connect/config.toml`
- `reasonix-bot.service`（enabled）：`reasonix bot start --channels feishu --dir /home/zhengzj22/dsh`
- `research-agent-controller.service/.timer`（enabled，每分钟）、`research-agent-feishu-gateway.service`、`research-agent-project@.service`、`research-agent-supervisor.service`（未 enable；指向 research-agent-orchestrator 的 venv）
- `~/.reasonix/projects/` 已有 4 个项目目录（含本仓库）。

## 8. Python 包现状

- `~/.local/lib/python3.12/site-packages`：仅 modelscope/py_spy/pylatexenc，无 pydantic/sqlalchemy/fastapi/alembic/lark。
- `Documents/research-agent-orchestrator/.venv`：lark_oapi-1.7.1、httpx-0.28.1、websockets-15.0.1（可参考其飞书集成方式，不直接复用）。
- 本项目依赖由 `uv sync` 安装（已验证成功：pydantic/sqlalchemy 2.0.51/fastapi/alembic/structlog 等）。

## 9. 关键结论

1. 全部本地确定性工作可在本工作区完成；运行时数据放 `.data/`。
2. Reasonix ACP 可经隔离 `REASONIX_HOME` 真实运行（Phase 4 落地）。
3. Codex App Server v2 协议完整，可真实实现 thread/turn 生命周期（Phase 5 落地）。
4. cc-connect 已有 Management API 与飞书全能力；Delivery API 以窄 patch 形式提供（Phase 6）。
5. 真实飞书/cc-connect 发送需授权门禁与 chat id；此前全部以 fake 完成并标注 BLOCKED 项。
