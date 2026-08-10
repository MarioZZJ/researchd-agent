# AGENTS.md — researchd

研究控制系统 `researchd` 的实现契约与开发约定。完整契约见 `IMPLEMENTATION.md`（不可更改的冻结设计）。

## 项目是什么

- 持久化科研状态（Project/Question/Task/Run/Evidence/Claim/Decision/Issue…）+ 调度 + 科学决策门控 + 报告 + 文档投影。
- 三个入口：`researchd service`（唯一数据库 Writer）、`researchd acp`（入站 shim）、`researchctl`（运维 CLI）。
- 可替换 Executor Adapter：`ReasonixAdapter`（ACP）、`CodexAdapter`（App Server）、`FakeExecutor`（测试）。

## 常用命令

```bash
uv sync --all-groups          # 安装全部依赖（含 dev/feishu）
uv run pytest tests/unit -q   # 单元测试
uv run pytest -q              # 全部测试
uv run alembic upgrade head   # 迁移
uv run researchctl doctor     # 诊断
```

## 架构红线（违反即失败）

1. 只有 `researchd service` 写数据库；其余入口一律走内部 API。
2. 有科学意义的状态变化必须同一事务内：更新聚合 + 追加 events + 必要时插入 outbox。
3. `RUNNING → COMPLETED` 非法；必须经 `REVIEW`。Run 成功 ≠ Task 完成。
4. 所有 `idempotency_key` 唯一；重复输入只应用一次。
5. 不把原始 Executor 输出/日志/reasoning 发往飞书；不保存模型思维链。
6. 不修改用户全局 `~/.reasonix` 配置；Reasonix 运行时用隔离 `REASONIX_HOME` overlay。
7. 秘密不进 Git、不进日志、不进审计文档（只记键名）。

## 环境事实（Phase 0 已验证，勿假设）

- `/home` 只读挂载；可写位置：本仓库、`/tmp`、`~/.cache`。运行时数据放 `.data/`（gitignored）。
- `python3.12` 来自 `/opt/anaconda3/bin/python3.12`；用 `uv` 管理（不要用系统 pip，pip 不存在）。
- `reasonix v1.21.2`：ACP `session/new` 需要可写 sessions 目录 → 用 `REASONIX_HOME` 隔离 overlay。
- `codex-cli 0.146.0`：`app-server` 为 JSON-RPC，schema 生成自 `codex app-server generate-json-schema`。
- `cc-connect v1.4.1`：源码 `/home/zhengzj22/cc-connect`（工作树必须保持干净，改动用 patch 输出到 `integrations/cc-connect/patch/`）；Management API `:9820`（token）。
- systemd user services 可用（`systemctl --user`）；无 sudo → 全部 user 级部署。

## 测试约定

- 测试分 `unit/integration/conformance/recovery/e2e` 五类，用 pytest marker 标记。
- 真实外部操作（飞书发送、凭据使用、付费模型调用）先走授权门禁；本地一律 FakeDeliveryPort/FakeExecutor。
- 测试 fixture `tests/fixtures/golden_research_project/` 是 synthetic，严禁导入真实 pilot 数据。

## 提交约定

- 每阶段原子 commit；建议语义见 IMPLEMENTATION.md §23。
- 提交前 `git diff --check` 必须干净。
