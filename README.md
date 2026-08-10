# researchd

可持久运行的科研控制系统（v0.1）：由持久状态、任务调度、证据追溯、人工门控和可恢复执行构成的研究操作系统。

```
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

## 入口

```text
researchd service      # 长期常驻服务，唯一数据库 Writer
researchd acp          # cc-connect 启动的轻量 ACP 入站 shim
researchctl            # 运维、查询、恢复和诊断 CLI
```

## 快速开始

```bash
uv sync --all-groups
uv run alembic upgrade head
uv run researchctl doctor
uv run researchd service --data-dir .data
```

## 文档

- `IMPLEMENTATION.md` — 冻结的实施契约（不可更改）
- `IMPLEMENTATION_STATUS.md` — 完成判据逐项追踪
- `docs/` — 环境审计、兼容性矩阵、假设、阻塞项、架构、威胁模型、运维、恢复等

## 安全

- 只有 `researchd service` 写数据库；秘密不进 Git/日志；不发送原始 Executor 输出。
- 真实外部操作（飞书、付费模型）默认 GATED，见 `docs/blockers.md`。
