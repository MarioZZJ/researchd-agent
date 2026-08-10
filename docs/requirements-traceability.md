# requirements-traceability（IMPLEMENTATION.md → 代码 → 测试）

每条冻结要求映射到实现与测试证据。行号引用 IMPLEMENTATION.md 对应章节。

## 1. 运行时与架构

| 要求（章节） | 实现 | 测试 |
|---|---|---|
| 可替换 Executor Adapter（§2） | `executors/base.py`（ExecutorAdapter/WorkResult/PlannerResult/AuditResult）、`executors/{fake,reasonix,codex}/` | `tests/conformance/`（三 adapter）、`tests/unit/test_executor_schema.py` |
| 状态机不得绕过（§7, §25.1） | `domain/state_machine.py` + 各 domain transition（COMPLETED/VERIFIED 仅经门控方法） | `tests/unit/test_state_machines.py`（RUNNING→COMPLETED 拒绝等） |
| 乐观并发（§25.1） | `persistence/repositories.py` BaseRepo.version 校验 | `tests/unit/test_transactions.py` |
| 事务 + outbox（§8） | `persistence/transaction.py`、`persistence/outbox.py`（IN_FLIGHT 租约/fencing/回收） | `tests/unit/test_transactions.py`、`tests/unit/test_outbox.py` |

## 2. 幂等（§25.2）

| 重复输入 | 实现 | 测试 |
|---|---|---|
| 同一飞书消息 | inbound_messages.idempotency_key 唯一 | `tests/integration/test_api_phase2.py::test_inbound_*` |
| 同一 Decision 按钮 | /decision 幂等键 + 版本指纹 | `test_decision_reapply_is_noop` |
| 同一 Executor 结果 | run-applied 事件唯一键 | `tests/e2e/test_golden_path.py`（重启不重复证据） |
| 同一 Outbox delivery | outbox.idempotency_key + IN_FLIGHT 唯一 | `test_outbox_*`、`test_outbox_claim_and_backoff` |
| 同一 Decision answer | decisions.answer 唯一 + 版本校验 | `test_decision_version_conflict` |
| 重启后重复回调 | 上述全部持久化 | `tests/recovery/` |

## 3. 恢复（§25.3）

| 故障注入 | 实现 | 测试 |
|---|---|---|
| service 在 Worker 运行时退出 | orphan reconciliation（RUNNING→INTERRUPTED→requeue） | `tests/recovery/test_orphan_reconciliation.py`、e2e 执行中重启 |
| 投递后崩溃 | outbox 持久化 + 重投递（幂等） | `tests/recovery/test_outbox_recovery.py` |
| 重复回调 | 幂等键 | 同上 |
| kill -9 | 排他锁 flock 自动释放 + Restart | `docs/operations.md` 演练 |

## 4. Executor 兼容（§13-16）

| 要求 | 实现 | 证据 |
|---|---|---|
| Reasonix ACP 真实能力 | `executors/reasonix/`（overlay/transport/adapter） | 真实握手：loadSession、session/new（`docs/compatibility-matrix.md`）；conformance 10 项 |
| Codex App Server v1/v2 | `executors/codex/`（transport/adapter） | 真实 thread/start；conformance 6 项 |
| 结构化输出修复循环 | adapter.ensure_valid | conformance |
| 最小能力（§22） | overlay 白名单 + 独立 HOME/CODEX_HOME | `tests/conformance/test_overlay_isolation.py` |

## 5. 科学门控与报告（§17-18）

| 要求 | 实现 | 测试 |
|---|---|---|
| Evidence VERIFIED 需真实 provenance | `application/evidence_validation.py`（artifact/run/code/data 门） | `tests/unit/test_evidence_validation.py` |
| 决策门控（阈值/直接证据/冲突） | `application/decision_gate.py` | `tests/unit/test_decision_gate.py` |
| 报告确定性（禁 AI slop） | `reporting/reporter.py`（ReportSpec 编译 + FINALIZED 快照） | `tests/unit/test_reporter.py`、e2e 报告断言 |
| 无原始输出直达飞书 | 报告仅来自 ReportSpec；DeliveryPort 窄接口 | review 两轮 + `tests/conformance/` |

## 6. 飞书（§20）

| 要求 | 实现 | 状态 |
|---|---|---|
| 决策卡发送/点击/幂等/原地更新 | `delivery/`（FakeDeliveryPort 全链） | Fake ✓；真实 GATED（B-01） |
| 文档增量同步 | `projections/feishu_doc.py`（compile_sections/hash 收敛/PI Notes 保护/TOCTOU） | 13 项测试 ✓；真实 GATED（B-01） |
| cc-connect 窄补丁 | `integrations/cc-connect/patch/delivery-api.patch`（373 行） | 可应用；安装 GATED（B-06） |

## 7. 配置（§15）

| 要求 | 实现 | 测试 |
|---|---|---|
| interaction model 会话级 | ACP bind/model 仅会话级 + /research model | `tests/integration/` |
| role profiles | project.policy.role_overrides + contract.executor_profile 冻结到 run | `tests/conformance/test_profile_*` |

## 8. 安全（§22）

| 要求 | 实现 | 证据 |
|---|---|---|
| 唯一写者 | data-dir 排他锁 + UDS 0600 | `tests/unit/test_service_lock.py`、doctor |
| 路径逃逸拒绝 | safe_resolve/symlink 检查/tar 预检 | `tests/unit/test_evidence_validation.py`、`test_backup.py` |
| Executor 不可访问 secrets | overlay env 白名单 | `tests/conformance/test_overlay_isolation.py` |
| threat-model 文档 | `docs/threat-model.md` | security-review |
| secret 不进 Git | .gitignore + env 模板 | 人工核验 |

## 9. 部署与运维（§27）

| 要求 | 实现 | 状态 |
|---|---|---|
| systemd unit + 启动/停止/自动恢复 | `deploy/systemd/researchd.service` | 语法 ✓、kill -9 演练 ✓、持久安装 B-07 |
| 在线备份/恢复/导出 | `ops/backup.py` + researchctl | 7 项测试 + 真实演练 |
| doctor | `ctl.py`（PRAGMA/schema/perms/healthz） | 冒烟 ✓ |

## 10. 黄金路径（§26）与 Pilot（§24）

| 要求 | 实现 | 状态 |
|---|---|---|
| 22 步 deterministic e2e | `tests/e2e/test_golden_path.py` | ✓（含执行中重启） |
| pilot 项目 + D-001 | `researchd pilot` + `docs/pilot.md` | 项目已建（D-001 APPLIED）；真实模型运行 GATED（B-01/B-03） |
