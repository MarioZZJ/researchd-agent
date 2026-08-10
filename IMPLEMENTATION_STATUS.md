# IMPLEMENTATION_STATUS

> 追踪 IMPLEMENTATION.md §29 完成判据。逐项标注状态：`DONE`（有证据）/ `IN_PROGRESS` / `PENDING` / `BLOCKED`（附原因与解除条件）。证据 = 测试命令 + 输出摘要 + commit。

## 完成判据追踪

| # | 判据 | 状态 | 证据 / 备注 |
|---|---|---|---|
| 1 | 空库可执行全部 Alembic migration | DONE | `RESEARCHD_DB=/tmp/mig-check/fresh.db uv run alembic upgrade head` 成功；autogenerate 一致性检查零 diff（26 表） |
| 2 | 单元、集成、conformance、recovery、e2e 测试通过 | PARTIAL | 111 passed + 2 门控跳过（Phase 1–6）；e2e 待做 |
| 3 | Reasonix Adapter 真实 capability tests 通过 | PARTIAL | 真实握手（native 二进制+最小 overlay）initialize/session/new 通过；付费 prompt conformance GATED（B-03） |
| 4 | Codex Adapter 真实验证或明确 BLOCKED | PARTIAL | 非付费握手（CODEX_HOME 隔离）通过；付费 turn conformance BLOCKED（B-02） |
| 5 | interaction model 和 project role profiles 可配置 | PENDING | Phase 2 |
| 6 | cc-connect 补丁实际安装验证或可应用 patch + blocker | PARTIAL | patch 可应用（373 行，write-ahead 幂等）；未安装验证（B-06 无 Go 工具链）；真实发送 GATED（B-01） |
| 7 | 飞书决策卡可发送/点击/幂等/原地更新，或准确 blocker | PARTIAL | FakeDeliveryPort 全链路（幂等/按钮/恰好一次）；真实卡 GATED（B-01） |
| 8 | 飞书项目文档可增量同步，或准确权限 blocker | PENDING | Phase 7（B-01） |
| 9 | systemd/user-systemd 启动、停止、自动恢复 | PENDING | Phase 9 |
| 10 | deterministic 黄金路径完整通过 | PENDING | Phase 8 |
| 11 | 真实 pilot 已创建并至少开始首批任务 | PENDING | Phase 8 |
| 12 | 数据库和项目目录备份恢复演练通过 | PENDING | Phase 9 |
| 13 | 不存在原始 Executor 输出直达飞书的代码路径 | DONE | 报告 body 仅来自 ReportSpec 确定性编译；review 两轮确认 |
| 14 | threat model、运维、恢复、回滚、模型配置文档齐全 | PENDING | Phase 6/9/10 |
| 15 | requirements-traceability 映射每项冻结要求 | PENDING | Phase 10 |
| 16 | 所有改动有 Git commit，工作树状态明确 | IN_PROGRESS | Phase 1 提交后更新 |

## 阶段状态

| Phase | 内容 | 状态 | commit |
|---|---|---|---|
| 0 | 环境审计与骨架 | DONE | `4a316c7` |
| 1 | 领域模型、迁移、持久层 | DONE | `94fe79f` |
| 2 | Service、API、CLI、ACP shim | DONE | `58888c7` |
| 3 | 调度、租约、锁、恢复 | DONE | `40ffa7f` |
| 4 | Reasonix ACP Adapter | DONE | `1f91623` |
| 5 | Codex App Server Adapter | DONE | `b36c956` |
| 6 | Decision Gate、Reporter、DeliveryPort | IN_PROGRESS | 待提交 |
| 7 | 飞书文档投影 | PENDING | — |
| 8 | 黄金路径与 Pilot | PENDING | — |
| 9 | 部署、备份、运维 | PENDING | — |
| 10 | 最终审查与收尾 | PENDING | — |

## 环境基线（Phase 0，2026-08-10）

- Python 3.12.12（uv 0.9.15）、git 2.34.1、systemd 249 user 可用、无 sudo
- reasonix v1.21.2：ACP 握手 VERIFIED；隔离 REASONIX_HOME 方案验证
- codex-cli 0.146.0：app-server v2 协议 schema 完整
- cc-connect v1.4.1：Management API + 飞书全能力；工作树干净（detached HEAD 5d4c96d）
- 飞书凭据存在 → 真实操作 GATED（B-01）
- 可写位置：本工作区、/tmp、~/.cache（/home 只读）→ 数据根 `.data/`
