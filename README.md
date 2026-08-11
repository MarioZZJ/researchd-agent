# researchd

可持久化、可恢复、科学门控的研究自动化系统（v0.1）。

由 **Reasonix 主线程**按 `IMPLEMENTATION.md` 逐 Phase 实施：领域模型与事务、
服务/API/CLI/ACP 入口、调度与恢复、Reasonix/Codex Executor Adapter、
证据门控与确定性报告、飞书投影、黄金路径 e2e、部署与运维。

## 快速开始

```bash
uv sync --all-groups
uv run researchd migrate            # 空库跑全部 Alembic migration
uv run researchd service            # 唯一写者（data-dir 排他锁）
uv run researchctl doctor           # 只读健康检查
uv run researchd pilot create --project-id <id> --import-decision D-001=A   # 创建 pilot
uv run pytest -q                   # 168 passed + 3 门控跳过
```

## 文档索引

| 文档 | 内容 |
|---|---|
| `IMPLEMENTATION.md` | 实施契约（规格、判据、禁止事项） |
| `docs/completion-report.md` | v0.1.1 完成报告（IMPLEMENTED/LIVE VERIFIED/GATED/FAILED） |
| `docs/requirements-traceability.md` | 冻结要求 → 代码 → 测试 |
| `docs/threat-model.md` | 威胁模型（§22，security-review 审查） |
| `docs/operations.md` | 运维手册（备份/恢复/日志/doctor） |
| `docs/compatibility-matrix.md` | Reasonix/Codex/飞书/cc-connect 兼容矩阵 |
| `docs/blockers.md` | 真实 blocker（B-01 飞书、B-03 付费模型、B-06 cc-connect、B-07 systemd） || `docs/pilot.md` | Pilot 定义与 bootstrap |
| `docs/environment-audit.md` | Phase 0 环境审计 |
| `docs/adr/` | 架构决策记录 |

## 结构

```
src/researchd/
  domain/       领域对象与状态机（Task/Run/Evidence/Claim/Decision/Issue）
  persistence/  ORM（26 表）、Alembic、事务、outbox、排他锁
  executors/    FakeExecutor / ReasonixAdapter(ACP) / CodexAdapter(app-server)
  application/  命令、决策门、证据验证、结果落库
  scheduler/    调度循环、租约、诊断、投影、outbox sender
  reporting/    确定性报告（ReportSpec 编译，禁 AI slop）
  projections/  飞书文档投影（增量/hash 收敛/PI Notes 保护）+ 真实 FeishuDocClient
  api/          UDS/TCP 内部 API（入站幂等、授权、409、delivery/document test）
  integrations/ cc-connect Delivery 端口（真实 interactive card）
  ops/          备份/恢复/导出
  cli.py        入口：researchd service/acp/migrate/pilot create/backup/restore/export
  ctl.py        researchctl（doctor/delivery test/document test/交互）
migrations/     Alembic
 deploy/         systemd unit + env 模板
integrations/   cc-connect 窄补丁（Delivery API，已编译验证）
tests/          unit / integration / conformance / recovery / e2e
```

## 关键约束

- **唯一写者**：只有 `researchd service` 进程写库（排他锁强制）。
- **证据门控**：无真实 artifact/run/provenance 的 Evidence 不可 VERIFIED；
  Evidence 只有独立 Auditor ACCEPT 后才可能 VERIFIED。
- **审计闭环**：Task 必须经独立 Auditor 审查（ACCEPT）才能 COMPLETED。
- **报告确定性**：飞书报告 body 仅来自 ReportSpec 确定性编译，原始 Executor
  输出/思维链永不直达飞书。
- **真实平台 GATED**：飞书（B-01）与真实付费模型（B-03）需宿主授权后才启用；
  真实外部发送只发往用户显式提供的 staging chat/document。
