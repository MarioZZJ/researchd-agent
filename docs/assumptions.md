# Assumptions（记录采用的保守默认值）

> 每条记录：假设内容、理由、影响、复审点。按 IMPLEMENTATION.md §0.3，非关键不确定一律采用默认值继续，不阻塞实施。

## A-01 运行时数据目录 = 工作区内 `.data/`

- 假设：`researchd` 的 SQLite、runs、overlay、socket 全部放 `<repo>/.data/`（gitignored）。
- 理由：`/home` 整盘只读，/var/lib、XDG 路径均不可写；唯一可写且持久的挂载是 `researchd-agent` 工作区（`/tmp` 不持久）。
- 影响：部署文档、systemd unit、备份脚本都以 `.data/` 为数据根。
- 复审：获得新可写挂载后可迁移。

## A-02 Python 3.12 来自 /opt/anaconda3

- 假设：项目解释器固定为 `uv python find 3.12` 找到的 `/opt/anaconda3/bin/python3.12`（3.12.12），用 `uv sync` 管理，不依赖系统 python3（3.10）。
- 理由：IMPLEMENTATION.md 要求 3.12+；系统默认是 3.10。

## A-03 ReasonixAdapter 采用隔离 REASONIX_HOME overlay

- 假设：每个 Reasonix 实例进程以 `REASONIX_HOME=<run-dir>/rx-overlay/` 启动；overlay 内是全局 `~/.reasonix/config.toml` 的可写副本（含 gateway provider 与 api_key，仅供本进程使用，权限 0600，不进入 Git、不进日志）。
- 理由：`session/new` 需要可写 sessions 目录；全局目录只读且禁止修改。文档 §15.2 允许"命名实例 + 隔离配置 overlay"。
- 边界：overlay 不复制 bot/ 等无关配置；不使用全局 session 数据；不写入任何项目仓库。

## A-04 外部发送一律先经授权门禁

- 假设：真实飞书消息、cc-connect 调用、付费模型调用视为外部操作，在首次执行前走 Reasonix 授权门禁；此前全部以 Fake 完成并保持确定性测试绿。
- 影响：Phase 6/7 真实平台项标 BLOCKED(门禁)，但代码路径完整、可一键启用。

## A-05 cc-connect 补丁以 patch 文件交付

- 假设：不修改 `/home/zhengzj22/cc-connect` 工作树（detached HEAD，干净）；Delivery API 改动以可应用 patch 放在 `integrations/cc-connect/patch/`，含安装/回滚说明。
- 理由：外部仓库未提交改动不得覆盖；窄补丁可独立上游提交（文档 §19.2）。

## A-06 interaction 与 execution policy 严格隔离

- 假设：`/research model interaction <fast|deep|deterministic>` 只写 `project.interaction_profile` 会话级字段；`/research config set role.<role> <profile>` 写 `project.executor_policy`（只影响未来 Run）。两者互不覆盖。

## A-07 同步 SQLAlchemy + asyncio 调度循环

- 假设：数据库用 SQLAlchemy 2 同步 Session（SQLite 本地文件，写操作微秒级）；FastAPI 端点用 `def`（自动线程池）；调度循环在 asyncio 中直接调用同步 DB（每轮操作量小），不在 DB 层引入第二个异步驱动。
- 理由：SQLite 单写者模型 + 事务原子性要求下，同步驱动更简单可靠；仍满足"异步模型 asyncio"主体。
- 复审：若出现长时间 DB 阻塞，改 `asyncio.to_thread` 包裹（预留接口）。

## A-08 UDS socket 路径 = `.data/run/researchd.sock`

- 假设：内部 API 走 UDS；`/run/researchd/` 与 `$XDG_RUNTIME_DIR/researchd/` 均不可写，故使用 `.data/run/researchd.sock`。
- 影响：researchctl 与 systemd unit 使用同一路径；TCP fallback 永不启用（UDS 总是可行），因此不需要 Bearer token 的 TCP 路径（仍实现 token 校验供将来使用）。

## A-09 模型名与 profile 不硬编码

- 假设：示例配置（§15.2）只作为模板；实际 profile 从 Phase 0 实测的 gateway 模型清单生成，存于 `deploy/config.example.toml` 与模型配置文档。

## A-10 pilot 工作区位置

- 假设：pilot 项目工作区 = `.data/workspaces/interdisciplinary-citation-pilot/`（与代码仓库分离；模板来自 `templates/project/`）。
- 理由：文档要求项目工作区与代码仓库分离，且可写位置受限。

## A-11 pilot 真实运行的环境约束与观察（2026-08-12）

- 假设：真实数据源（OpenAlex）会限流/封禁宿主机（/works 查询被封 24h），
  真实模型会持续工作直到预算耗尽（单 turn 最长 60 分钟）。
- 影响：数据获取类任务的 success criteria 应包含「覆盖不足的量化记录与原因
  说明」兜底口径（模型自身 escalation 指引亦如此建议），否则环境内不可达的
  阈值（如引用解析率 ≥80%）会让任务永远无法 COMPLETED。
- 复审：pilot T1 的 c2 合同因此按 PI 授权放宽（详见执行报告，数据修复已披露）。

## A-12 真实模型输出是自由格式的（schema 校验之外的偏差常态）

- 假设：真实模型会声明目录 artifact（`data/raw/`）、在同任务后续 run 修改并
  重声明自己的交付物、使用枚举外 category（如 methodology）、引用 CANDIDATE
  evidence 或自由文本 task 标签。系统对这些偏差按「fail-closed per card /
  per declaration」处理（跳过而非整单拒绝；安全违规仍整单拒绝）。
- 影响：pilot 驱动的 7 项修复（completion-report §2b）均为此类偏差的系统级兜底；
  后续真实任务应预期这些偏差为常态而非异常。
