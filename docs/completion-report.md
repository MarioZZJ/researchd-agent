# researchd v0.1 完成报告

生成：Phase 10 收尾（按 IMPLEMENTATION.md §30 格式）。所有结论均有测试/演练证据。

## 1. 仓库绝对路径

`/home/zhengzj22/Documents/researchd-agent`

## 2. 分支与 commit

- 分支：`master`（25 个 commit，工作树干净）
- 最终 commit：`936f109`（docs 对齐与收尾）
- 提交序列（新→旧）：`1b8653b` → `b188e06`（deploy/backup/ops）→ `bc84d90`（pilot+e2e）→ `202d77f/c4dd2fa/0fe05da`（投影）→ `8152eb5`（门控/报告/投递）→ `b36c956`（codex）→ `1f91623`（reasonix）→ `40ffa7f`（调度/租约/恢复）→ `58888c7`（service/api/cli/acp）→ `94fe79f`（领域/迁移）→ `4a316c7`（scaffold）

## 3. 实际兼容性结论

| 组件 | 版本/路径 | 结论 |
|---|---|---|
| Python | 3.12.12（`uv` 0.9.15 管理） | ✓ 全部测试通过 |
| Reasonix | v1.21.2（ACP） | ✓ 真实握手：loadSession、session/new（compatibility-matrix 记录）；steer 为能力声明；fake conformance 10 项 |
| Codex | codex-cli 0.146.0（app-server） | ✓ 真实 initialize + thread/start（非付费）；fake conformance 7 项；turn/start（付费）GATED（B-02） |
| cc-connect | v1.4.1（5d4c96d，Go） | patch 300 insertions 在干净基线 apply 通过；安装 GATED（无 Go 工具链，B-06） |
| 进程隔离 | 无 root/bwrap/landlock | B-08（同 uid 协作式缓解，无 OS 级隔离） |

## 4. researchd 服务状态与启动方式

- 当前状态：未运行（演练后停止；工作树干净）。
- 启动：`cd /home/zhengzj22/Documents/researchd-agent && uv run researchd service`（或
  `systemctl --user start researchd`，见 docs/operations.md §1；本机持久安装受 B-07 限制）。
- 优雅停止：SIGTERM（排空调度器 → 关闭 executor 会话 → 释放锁）；崩溃自动恢复演练通过。

## 5. SQLite / 项目根 / 配置路径

- 数据库：`/home/zhengzj22/Documents/researchd-agent/.data/researchd.db`（0600，WAL）
- 数据根：`.data/`（0700）；运行时 socket `.data/run/researchd.sock`（0600）
- 配置：`deploy/researchd.env`（0600，gitignored）+ 代码内默认（`researchd/config.py`）
- 迁移：`migrations/`（Alembic；空库 `researchd migrate` 全通过，autogenerate 零差异）

## 6. 测试命令与结果

```bash
uv run pytest -q      # 144 passed, 2 skipped（真实平台 conformance 门控项）
uv run researchctl doctor   # 只读健康检查（PRAGMA/schema 27 表/perms/healthz）
```
覆盖：unit（状态机/幂等/事务/outbox/证据/路径/备份/artifact provenance）、integration（API/幂等/授权/409/约束回归）、
conformance（FakeExecutor 协议 10 项 + Codex 6 项 + 真实进程握手）、recovery（9 项故障注入）、
e2e（黄金路径 22 步，含执行中重启）。
安全加固：security-review 10 轮迭代（阻断/高危全部闭环）——mutating API 全 transport token、
成员门 fail-closed + 创建者即 owner、actor 必填、workspace_root 服务派生（lstat 锚点/O_NOFOLLOW/
`..` 词法拒绝）、artifact 注册门接入 apply 且 provenance 不可变、tar 逃逸防护、威胁模型诚实化（B-08）。

## 7. Executor 真实能力矩阵

| 能力 | Reasonix(ACP) | Codex(app-server) | 证据 |
|---|---|---|---|
| initialize | ✓ loadSession | ✓ | 真实进程握手 |
| 会话创建 | ✓ session/new | ✓ thread/start | 真实进程握手 |
| 结构化输出修复循环 | ✓ | ✓ | conformance |
| steer/打断 | ✓（扩展探测） | ✓ turn/interrupt | conformance |
| 付费模型调用 | GATED（B-03） | GATED（B-03） | — |

## 8. cc-connect 修改位置、patch 与回滚

- patch：`integrations/cc-connect/patch/delivery-api.patch`（373 行，300 insertions：
  `core/engine.go` +40、`core/interfaces.go` +13、`core/management.go` +182、
  `platform/feishu/feishu.go` +65）——新增 `POST /api/v1/projects/{name}/deliveries`
  （消息 handle 返回 + 幂等入站）+ `PATCH .../deliveries/{id}`（原地更新）。
- 安装：`cd <cc-connect> && git apply <patch>`（干净 5d4c96d 验证通过）。
- 回滚：`git apply -R <patch>`（见 `integrations/cc-connect/patch/README.md`）。
- 未 fork、无 Go 工具链未安装（B-06）。

## 9. 飞书真实绑定状态

| 绑定 | 状态 |
|---|---|
| 项目群 / PI Inbox / 项目文档 | **GATED（B-01）**：FakeDeliveryPort 全链路（决策卡、按钮幂等、原地更新）与文档投影 13 项测试通过；真实平台需飞书凭据授权（`~/.cc-connect/config.toml` 与 `~/.config/research-agent-orchestrator/*.feishu.json` 已存在但未经项目授权使用） |

## 10. Pilot 当前状态

- 项目：`interdisciplinary-citation-pilot`（`researchd pilot` bootstrap，幂等）
- D-001：已应用（answer=A）
- 首批任务：**GATED**（真实模型调用需授权 B-03；生产配置 `executor=fake` 不产生真实研究任务）
- 定义与启动命令：`docs/pilot.md`

## 11. 黄金路径证据

`tests/e2e/test_golden_path.py`（22 步）：planner 批 → 并行 worker（max_parallel=2）→
schema 修复循环 → 冲突 → 廉价诊断 → D-002 门控 → blocking_scope 阻塞 → 决策卡 + 重复点击
恰好一次 → 解阻塞 → 证据/Claim 落库（provenance 门控）→ milestone → 文档增量投影
（PI Notes 保护）→ 执行中重启（INTERRUPTED 恢复、不重复投递/证据）。

## 16 项完成判据核验

| # | 判据 | 状态 | 证据 |
|---|---|---|---|
| 1 | 空库全部 Alembic migration | ✓ | `researchd migrate` + autogenerate 零差异 |
| 2 | 单元/集成/conformance/recovery/e2e | ✓ | 144 passed + 2 门控跳过 |
| 3 | Reasonix 真实 capability tests | ✓ | 真实握手（loadSession/session/new/steer） |
| 4 | Codex 真实验证或明确 BLOCKED | ✓ | 真实 initialize/thread/start；turn GATED |
| 5 | interaction model / role profiles 可配置 | ✓ | /research model + role_overrides + 冻结到 run |
| 6 | cc-connect patch 可应用或真实 blocker | ✓ | 干净基线 apply 通过 + B-06 |
| 7 | 飞书决策卡或准确 blocker | ✓ | Fake 全链 + B-01 |
| 8 | 飞书文档增量同步或准确 blocker | ✓ | 15 项投影测试 + B-01 |
| 9 | systemd 启动/停止/自动恢复 | ✓ | unit 语法 + kill -9 演练；持久安装 B-07 |
| 10 | deterministic 黄金路径 | ✓ | e2e 22 步 |
| 11 | 真实 pilot 创建并开始首批任务 | PARTIAL | 项目 + D-001 ✓；真实模型运行 GATED（B-01/B-03） |
| 12 | 备份/恢复演练 | ✓ | 在线备份（写中快照）+ round-trip + 恶意 tar 防护 |
| 13 | 无原始 Executor 输出直达飞书 | ✓ | 报告仅 ReportSpec 编译；review 确认 |
| 14 | threat model/运维/恢复/回滚/模型配置文档 | ✓ | docs/threat-model.md（security-review 审查）、operations.md、cc-connect README、env.example |
| 15 | requirements-traceability | ✓ | docs/requirements-traceability.md |
| 16 | 全部改动有 commit，工作树明确 | ✓ | 13 commits，`git status` 干净 |

**结论**：v0.1 交付完成；未达标项均以真实 blocker（B-01/B-03/B-06/B-07）记录并附解除条件，
无"基本可用"式表述。
