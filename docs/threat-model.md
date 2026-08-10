# researchd 威胁模型（IMPLEMENTATION.md §22）

> 本文件由 security-review 审查（v0.1 收尾阶段）。

## 1. 资产与信任边界

| 资产 | 位置 | 机密性 | 完整性 | 可用性 |
|---|---|---|---|---|
| 研究数据（证据/声明/决策） | `.data/researchd.db`（0600） | 中 | 高 | 高 |
| 项目工作区 | `.data/workspaces/*`、projects.workspace_root | 中 | 高 | 中 |
| 事件/审计日志 | events 表 + journald | 低 | 高 | 低 |
| 配置与 secret | `deploy/researchd.env`（0600）、飞书凭据（cc-connect/reasonix 配置） | 高 | 高 | — |

信任边界：
1. **Executor 进程**（reasonix/codex/fake）— 不可信，可输出任意 JSON；
2. **入站消息**（API/ACP）— 不可信，可伪造；
3. **本地服务**（同 uid 进程）— 部分可信（UDS 0600 + data-dir 排他锁）；
4. **外部平台**（飞书/cc-connect）— 网络边界，credential 保护。

## 2. 威胁与缓解（映射到实现）

### T1 Executor 输出注入 / 状态库污染
- 缓解：JSON Schema 严格校验（`executors/schemas/*.json` + `validate_work_result`）；schema 错误 → 修复循环或 FAILED，绝不部分落库；`run-applied` 事件幂等门（同一 run 结果只应用一次）；Executor 无直接 SQL/写库路径（唯一写者 = service）。
- 测试：`tests/conformance/test_fake_executor_*`（修复循环/耗尽）、`tests/unit/test_idempotency*`。

### T2 越权执行（无授权模型调用）
- 缓解：执行 profile 白名单（scheduler `_resolve_profile`：contract > role_override > default）；`RESEARCHD_SCHEDULER__EXECUTOR=fake` 为默认；真实模型 GATED（docs/blockers.md B-03）。
- 测试：`test_profile_rejection`、`test_dispatch_respects_profile`。

### T3 飞书消息伪造 / 重复回调
- 缓解：入站幂等键（inbound_messages 唯一约束，重复 no-op）；`/decision` 版本指纹必填（缺失 400、不匹配 409、重复点击恰好一次）+ 成员/审批门（fail-closed：创建即 provision owner，非成员/缺身份一律 403/400）；**写接口与项目数据 GET 要求 Bearer token（UDS 同样强制，token 存 0600 env 且不在 Executor env 白名单）；actor 必填、无默认值，身份由持 token 的网关声明；项目创建时 workspace_root 由服务派生（限制在 data_dir/workspaces 下）**；消息解析仅提取受控字段（bind/model/id），不执行任意指令。
- 测试：`tests/integration/test_api_phase2.py`（幂等/409/400/401/403、UDS 无 token 401、actor 缺失 400）。

### T4 Executor 会话越权（跨项目污染）
- 缓解：每 run 独立会话（session/new 每 run）；最小 overlay（reasonix：仅复制 [[providers]]/default_model，环境白名单 + HOME 重定向，overlay 0600）；codex：独立 CODEX_HOME（0600，helper 拒绝 /tmp）；Executor env 白名单（PATH/HOME/REASONIX_HOME/TERM/LANG/LC_ALL/TZ），不注入飞书/cc-connect token；API socket 0600。
- **局限（B-08）**：本机无 root/bwrap/landlock，Executor 与 service 同 uid 运行，**没有 OS 级进程隔离**——同 uid 的 Executor 可读写 `.data/`（DB/workspace/socket）、rename/unlink 文件、读 0600 env（含 API token）、替换 symlink 绕过路径门控。当前"唯一写者/认证/路径门控"均为协作式约束，可被同 uid 恶意 Executor 绕过；缓解层级：协作式锁 + 最小暴露 + env 白名单 + 结构化输出 schema 门控。生产多租户环境必须加独立 uid 或 sandbox（bwrap/landlock/seccomp）。
- 测试：`tests/conformance/test_overlay_isolation.py`（无 key 文件、cwd 隔离）、`tests/integration/test_api_phase2.py::test_uds_mutating_endpoints_require_token`。

### T5 路径逃逸（artifact/workspace/restore）
- 缓解：`safe_resolve`（resolve 后必须位于项目根，拒绝 `..`）；artifact 注册检查 symlink 逃逸 + 项目根派生；restore tar 成员预检（链接/绝对/`..` 拒绝）+ staging 原子发布 + live 路径拒绝。
- 测试：`tests/unit/test_evidence_validation.py`、`tests/unit/test_backup.py`。

### T6 并发写冲突 / 状态丢失
- 缓解：version 乐观并发（rowcount=0 → 409）；data-dir 排他锁（唯一写者 service）；outbox IN_FLIGHT 租约 + attempts fencing + 回收。
- 测试：`tests/unit/test_transactions.py`、`tests/recovery/`。

### T7 机密泄漏（日志/报告/导出）
- 缓解：报告 body 仅来自 ReportSpec 确定性编译（无 Executor 原始输出/思维链直达飞书）；错误净化（`sanitize_validation_error` 只留 loc/type；API 对未预期异常返回固定错误码）；日志不打印 token；doctor 只读。
- 说明：reasonix overlay 会写入含 provider 配置的 config（0600，位于 gitignored `.data/`）；codex 复制 auth.json（0600）。这是运行所需的 runtime credential 文件，tracked tree 不含任何 secret。
- 测试：`tests/conformance/test_error_sanitization*`；review 确认无原始输出路径。

### T8 重启丢失 / 僵尸状态
- 缓解：outbox 持久化（重启后重新投递，idempotency_key 防重复）；orphan reconciliation（RUNNING 孤儿 → ORPHANED → task requeue）；执行中取消 → INTERRUPTED → requeue；kill -9 演练通过。
- 测试：`tests/recovery/test_scheduler_recovery.py`、`tests/e2e/test_golden_path.py`（执行中重启）。

### T9 Secret 进 Git
- 缓解：`.gitignore`（.data/、deploy/researchd.env）；模板只含占位符；凭据仅经 env 引用（`*_env` 模式）。
- 验证：`git grep` 无 token；`deploy/env.example` 审查。

## 3. 未缓解 / 剩余风险

| 风险 | 状态 |
|---|---|
| 真实 Executor（reasonix/codex）的 prompt 注入内容进入任务上下文 | 缓解为"结构化 schema + 门控"，未消除（模型自身能力边界）；B-03 |
| 飞书真实投递/文档同步 | GATED（B-01），Fake 全链路已测 |
| systemd 持久安装 | B-07（只读 /home），等价演练已过 |
| cc-connect 补丁安装 | B-06（无 Go 工具链），patch 可应用 |
| Executor 进程级隔离 | B-08（无 root/bwrap/landlock），协作式锁 + 最小暴露 |

## 4. 部署时的权限基线

`0700 .data/`；`0600 .data/researchd.db`、`researchd.lock`、`run/researchd.sock`、`deploy/researchd.env`（`researchctl doctor` 逐项检查）；UDS 优先于 TCP；TCP 必须配 token。
