# researchd v0.1.1 完成报告（live-readiness）

分支：`v0.1.1-live-readiness`（自 master 创建）
HEAD：`9c65028`；测试基线：**215 passed + 6 skipped**

状态标注：**IMPLEMENTED**（代码+本地测试完成）/ **LIVE VERIFIED**（真实环境
验证通过）/ **GATED**（已实现，等待宿主授权/权限后即可验证）/ **FAILED**（未达标）。

---

## 1. 控制平面 → 真实科研闭环（对照任务清单）

| # | 交付项 | 状态 | 证据 |
|---|---|---|---|
| 一.1 | `_build_delivery_port` 支持 fake/cc_connect，token 走安全配置、不进日志/DB/Artifact/Executor env；base_url 限 loopback HTTP/显式 HTTPS（SecretStr） | **IMPLEMENTED** | service.py；`tests/unit/test_delivery_cc_connect.py` |
| 一.2 | CcConnectDeliveryPort：真实 interactive card（卡片 1.0，`cmd:` 按钮协议 + after_click 中性反馈）；send/update/幂等键/platform_message_id 回执；空 session_key fail-closed | **LIVE VERIFIED** | delivery.py；RD测试 群实测：卡片发送 + PATCH 原地更新（`researchctl delivery test` → updated:true） |
| 三.1 | cc-connect patch：卡片 payload、编译+go test、独立分支安装 | **IMPLEMENTED**（干净克隆 + 双 patch 验证） | `integrations/cc-connect/patch/{delivery-api,inbound-messageid,card-json}.patch` 在干净克隆 v1.4.1（5d4c96d）上 apply + `go build -tags no_web` + `go test ./core/ ./agent/acp/` 全通过 |
| 三.2 | cc-connect 真实 POST/PATCH/重复 POST/崩溃恢复实测 | **LIVE VERIFIED**（POST/PATCH/重复幂等；崩溃恢复依赖 delivery uuid 幂等，代码级保证） | curl POST 200（platform_message_id）、同 key 重复 POST replayed、`researchctl delivery test` PATCH 更新 |
| 二 | FeishuDocClient：lark-oapi list/create/update_block + 真实创建/读取/共享/增量投影 | **LIVE VERIFIED** | researchd 应用创建项目 Docx `K9WqdRyIHoG54xxgx5sczr3Fn8c`（幂等）；共享（群 view + PI full_access，成员列表验证）；document test 块级往返全 true；增量投影 5 sections；幂等（无变化不重排队）；人工修改保护（sync 不覆盖人类内容） |
| 三.3 | researchctl UDS+TCP 都带 Bearer token | **IMPLEMENTED** | ctl.py；回归测试 |
| 三.4 | `researchctl delivery test` / `document test`（可实际执行） | **LIVE VERIFIED** | delivery test 真实卡片往返；document create 真实创建（幂等）；document test 待共享权限 |
| 三.5 | docs/pilot.md、README、operations.md 与 CLI 对齐 | **IMPLEMENTED** | 三文档已更新（含 §9c 飞书接入角色/权限） |
| 三.6 | `researchd pilot create` 修复 | **IMPLEMENTED** | cli.py；回归测试 |
| 四 | ContextPackageBuilder：planner/worker/auditor 独立包；保存 objects、hash、时间、token 估计；data_dir 规范化注入 | **IMPLEMENTED** | application/context_package.py + ContextPackageRepo |
| 五 | Reasonix overlay：workspace cwd、配置白名单、skills 白名单、resolved 记录；bwrap 文件系统隔离 + fail-closed | **LIVE VERIFIED**（隔离探针） | overlay.py/transport.py/adapter.py；`tests/integration/test_sandbox_isolation.py`（真实 bwrap 探针：DB/凭据不可见、workspace 可写） |
| 六/十 | Auditor 调度闭环：REVIEW→auditor→ACCEPT→VERIFIED→criteria PASS→COMPLETED；REVISE→READY；幂等+崩溃恢复；receipt 恢复不重复调用 | **IMPLEMENTED** | audit_gate.py + receipt 恢复测试 |
| 六 | deterministic live smoke（含重启恢复不重复模型调用/Evidence） | **LIVE VERIFIED**（真实 reasonix，`RESEARCHD_RUN_REAL_SMOKE=1` 全绿：直接链路 74s + service 重启链路 135s） | `tests/e2e/test_live_smoke.py`（语义等待真实 planner 输出、六维重启快照零重复） |
| 七 | Reporter：移除阈值 milestone；报告仅基于真实状态；结论引用证据、动作引用真实 Task | **IMPLEMENTED** | extensions.py `check_milestones` + reporter.py |
| 八 | ACP 身份：缺失 fail-closed+诊断；Decision answer 保留真实 platform user id/version/幂等键；真实 message_id 幂等 | **LIVE VERIFIED**（群消息闭环；按钮点击端到端 GATED） | agent.py + cc-connect inbound-messageid.patch；testbot→researchd 群回复实测 |
| 九 | 飞书接入两应用职责分离 + 白名单 + 防循环 | **LIVE VERIFIED** | researchd=cli_aaf007476338dd2c（被测）、testbot=cli_aaf9998d25f89bcf（PI 测试驱动）；allow_chat/allow_from 白名单；`scripts/testbot-smoke.sh` 真实 SMOKE PASS |

## 2. 顺带修复的真实缺陷（本阶段）

- `invocations.metadata_json` / `projection_states.snapshot_json` 列缺失（历史手工建表与 migration 不一致）→ 线上 DB ALTER 补齐。
- FeishuConfig 读 `LARK_APP_ID/SECRET` 但 env 曾写 `RESEARCHD_LARK_*` → 统一为代码实际读取的键并注释。
- lark-oapi `create_document` 响应解析：document_id 在 `data.document.document_id`（原读 `data.document_id` 为空）。
- 卡片 payload 曾带 `"schema": "2.0"` + 1.0 `tag:action` 按钮 → 飞书拒绝（"cards of schema V2 no longer support this capability"）→ 改纯卡片 1.0。
- cc-connect `isCardJSON` 只认 schema/body（卡 2.0）→ `card-json.patch` 放宽识别卡 1.0（config）。
- `CcConnectDeliveryPort` 未解析 `{"data": {...}}` 包装 → 永远取不到 platform_message_id。
- docx 共享曾导致创建整体失败 → best-effort + shared 标记 + 重放补共享。
- `DocumentCreateResponse` 定义在路由函数之后 → ForwardRef 解析失败 → 上移。
- cc-connect 运行配置写入只读 `/home` → `~/.cache/cc-connect-live`（0600）+ transient unit + 一键恢复脚本。

## 3. 测试命令与结果

```bash
uv run pytest -q        # 215 passed, 6 skipped（真实平台 conformance 门控项；真实 smoke 需 RESEARCHD_RUN_REAL_SMOKE=1）
go build -tags no_web ./cmd/cc-connect && go test ./core/ ./agent/acp/   # cc-connect 双 patch
uv run researchctl doctor            # 只读健康检查
```

## 4. LIVE VERIFIED 与 GATED 边界

**LIVE VERIFIED（真实环境已验证）**：
- **testbot → researchd 群消息闭环**：testbot（cli_aaf9998d25f89bcf）发 `@researchd /research status <marker>` → researchd（cli_aaf007476338dd2c）以 text 回复真实项目状态，发送者/内容断言通过（`scripts/testbot-smoke.sh` SMOKE PASS）。
- **Decision 卡片真实发送 + PATCH 原地更新**：`researchctl delivery test`（interactive 卡 + 按钮 + after_click）→ `platform_message_id` 回执 → 更新确认；同 key 重复 POST 幂等（replayed）。
- **Docx 真实创建（researchd 应用）**：`researchctl document create --project-id researchd` → `K9WqdRyIHoG54xxgx5sczr3Fn8c`，幂等重跑同 id，读 blocks 成功。
- **bwrap 文件系统隔离探针**：reasonix 子进程内 researchd.db / ~/.cc-connect / ~/.reasonix 不可见、workspace 可写（集成测试）。
- cc-connect 双 patch 干净克隆编译 + go test 全通过；patch 版实例（9820）承载 researchd project（acp）运行正常。

**GATED（代码就绪，等待宿主授权后执行）**：
1. ~~真实 Reasonix 模型 smoke~~ → **LIVE VERIFIED**（2026-08-12，本机直接运行）：`RESEARCHD_RUN_REAL_SMOKE=1 uv run pytest tests/e2e/test_live_smoke.py` **4/4 PASSED**（148s）——真实 planner（gpt-5.6-sol）产出唯一任务 → worker（deepseek-v4-flash）在 workspace 创建 `out/result.json`（完整 sha256 与磁盘一致）→ 独立 auditor run → audit gate（review_submitted → audit.accepted → completed 事件链）→ service 真实重启后 task/invocation/run/artifact/evidence/outbox 六维快照**完全一致**（零重复模型调用/artifact/evidence）；无 bwrap/acp 子进程残留。真实链路修复链：bwrap 挂载顺序（nvm 在 home mask 之上）→ `REASONIX_HOME/.cache` 可写 lease → 未知 planner role fallback worker → 安全加固（namespace-local `/proc`、去宿主 `~/.cache` bind、transcript dirfd 逐组件 O_NOFOLLOW + O_NONBLOCK）→ 模型自由格式 artifact 路径规范化（basename 前缀/绝对路径 → root 相对，仅当文件真实存在）→ 最终 4/4 全绿。
2. ~~Decision 按钮点击端到端~~ → **LIVE VERIFIED**（2026-08-12）：手工构造验证决策 D-E31E7247E0D447988EA8E90B（标注验证用途）→ 报告流程自动发决策卡（按钮 value=cmd:/decision ...）→ 真实点击（ou_8c1a4e0a...）→ cc-connect 保留点击者身份 dispatch → service cmd_decision（成员/approval/version 校验）→ decision ANSWERED → 原卡 PATCH 为「✅ 已记录你的选择」→ 重复点击幂等 no-op。
3. interdisciplinary-citation-pilot 项目与真实 D-002 决策（必须等 smoke 通过）→ smoke 已通过，pilot 项目待执行。

## 5. 发布 v0.1.1 的剩余判据（未满足即不发布）

- [x] researchd 应用开通 `docs:doc` 并发布/审批（共享 Docx 到测试群/PI 已 LIVE VERIFIED）
- [x] Reasonix 真实 Worker 在项目 workspace 创建 Artifact（真实 smoke LIVE VERIFIED：`out/result.json` sha256 与磁盘一致）
- [x] 独立 Auditor 真实运行（真实 smoke：独立 auditor run + review_submitted → audit.accepted 事件链）
- [x] Evidence provenance 可追溯（真实链路：evidence VERIFIED/CANDIDATE + invocations/runs/context packages 持久化）
- [x] Decision 按钮点击端到端（LIVE VERIFIED：真实点击 → ANSWERED → 卡片 PATCH → 重复点击幂等）
- [x] Feishu Docx 真实增量同步 + 人工修改保护 conformance（LIVE VERIFIED）
- [x] 服务重启无重复模型调用/Evidence/消息（真实 service 重启六维快照完全一致；Fake 链路常跑）
- [x] 原始 Executor 输出无直达飞书路径（代码审计通过：outbox 仅 ReportSpec body）
- [x] 全部单元、集成、恢复、e2e 与本地 conformance 测试通过（215+6；真实 smoke 4/4）
