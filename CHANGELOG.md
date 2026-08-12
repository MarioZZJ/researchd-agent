# Changelog

## [0.1.0] — 2026-08-10

### Phase 0 — 环境审计与骨架

- 初始化 Git 仓库、`.gitignore`、`AGENTS.md`、`pyproject.toml`（uv，Python 3.12）
- 完成本机环境审计（docs/environment-audit.md）与兼容性矩阵（docs/compatibility-matrix.md）
- 记录假设（docs/assumptions.md）与阻塞项（docs/blockers.md）
- 亲自验证 reasonix ACP 握手（v1.21.2）与 codex app-server v2 协议 schema（0.146.0）
- 确认 cc-connect v1.4.1 能力边界（Management API / 飞书 / ACP 后端；工作树保持干净）

## [0.1.1] — 2026-08-12

### live-readiness：真实闭环验证与发布

- 真实 reasonix smoke 4/4（直接链路 + service 重启六维快照零重复）
- pilot 真实闭环 LIVE VERIFIED：planner DAG → T1–T4 全 COMPLETED（独立 auditor
  ACCEPT）→ feishu docx 投影 → 决策卡入群 → 点击 ANSWERED + 卡 PATCH → 幂等
- 真实平台：decision 卡片发送/PATCH、testbot 群消息闭环、docx 创建/共享/增量投影
- pilot 暴露并修复的真实缺陷（详见 docs/completion-report.md §2b）：
  planner 依赖链持久化与 dispatch 依赖门、worker-BLOCKED 自动 requeue（有界）、
  transport 超时可重试 interrupt、畸形 decision candidate 不再打崩 tick、
  目录 artifact 声明跳过、同任务 artifact supersede、reporter 按卡 lint 跳过、
  决策-证据实时链接端点
- CLI/API：`researchd pilot create` 派生 workspace_root + 导入 OPEN 决策；
  `researchctl decision link`；POST /v1/decisions/{id}/evidence
- 测试基线：233 passed + 6 skipped
