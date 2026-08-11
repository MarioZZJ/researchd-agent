# researchd v0.1.1 完成报告（live-readiness）

分支：`v0.1.1-live-readiness`（自 master `9b23de9` 创建）
HEAD：`9ef2745`；测试基线：**183 passed + 4 gated skipped**

状态标注：**IMPLEMENTED**（代码+本地测试完成）/ **LIVE VERIFIED**（真实环境
验证通过）/ **GATED**（已实现，等待宿主授权后即可验证）/ **FAILED**（未达标）。

---

## 1. 控制平面 → 真实科研闭环（对照任务清单）

| # | 交付项 | 状态 | 证据 |
|---|---|---|---|
| 一.1 | `_build_delivery_port` 支持 fake/cc_connect，token 走安全配置、不进日志/DB/Artifact/Executor env | **IMPLEMENTED** | service.py；`tests/unit/test_delivery_cc_connect.py`（fail-closed、header-only） |
| 一.2 | CcConnectDeliveryPort：真实富文本 + interactive card；按钮不降级；send/update/幂等键/platform_message_id 回执；原始 Executor 输出不经该端口 | **IMPLEMENTED** | delivery.py `build_card_payload`（schema 2.0 + `cmd:` 按钮协议 + session_key + after_click）；outbox 只发 ReportSpec 编译 body |
| 三.1 | cc-connect patch：卡片 payload、编译+go test、独立分支安装 | **IMPLEMENTED**（编译/测试已验证；真实发送 GATED） | `integrations/cc-connect/patch/delivery-api.patch` 在干净克隆 v1.4.1（5d4c96d）上 `git apply` 干净；`go build`（web/embed 除外，既有前置）+ `go test ./core/ ./platform/feishu/` 全通过（含 TestDelivery* 幂等测试） |
| 三.2 | cc-connect 真实 POST/PATCH/重复 POST/崩溃恢复实测 | **GATED** | 需要带飞书凭据运行 cc-connect + staging chat（B-01 类授权） |
| 二 | FeishuDocClient：lark-oapi list/create/update_block + 真实 conformance | **IMPLEMENTED**（真实 conformance GATED） | feishu_client.py（分页、marker 块、指数退避重试）；`tests/conformance/test_feishu_docx_conformance.py` 本地 3 项通过；真实场景（增量同步/人工修改/revision conflict/PI Notes 保护/清理）待 `RESEARCHD_DOC_TEST_ID` |
| 三.3 | researchctl UDS+TCP 都带 Bearer token | **IMPLEMENTED** | ctl.py；回归测试 `test_ctl_client_sends_bearer_token_on_uds` |
| 三.4 | `researchctl delivery test` / `document test`（可实际执行） | **IMPLEMENTED**（真实目标 GATED） | ops_test.py 端点 + ctl 命令 + 回归测试；delivery test 需 `delivery=cc_connect` 配置，document test 需 LARK 凭据 |
| 三.5 | docs/pilot.md、README、operations.md 与 CLI 对齐 | **IMPLEMENTED** | 三文档已更新（pilot create、researchctl status→project status、168/183 数量、cc-connect 验证记录、test 命令） |
| 三.6 | `researchd pilot create` 修复（命令定义在 main() 调用之后从未注册） | **IMPLEMENTED** | cli.py 两级命令；回归测试 `test_pilot_create_cli_available` |
| 三.7 | README 测试数量/HEAD/completion 报告漂移 | **IMPLEMENTED** | 本文档即修正后的真实报告 |
| 四 | ContextPackageBuilder：planner/worker/auditor 独立包；保存 objects、hash、生成时间、token 估计 | **IMPLEMENTED** | application/context_package.py + ContextPackageRepo（context_packages 行：objects_json/content_hash/token_estimate/role/run_id）；auditor 包不含 worker 自评（测试断言）；调度器接线（worker/planner），Run.metadata 冻结 context_id |
| 五 | Reasonix overlay：workspace cwd、配置白名单（9 键+providers）、skills 白名单（reviewer/deep-research）、Run 记录 resolved model/profile/effort/skills | **IMPLEMENTED** | overlay.py/transport.py/adapter.py（per-workspace 子进程 cwd）；`tests/unit/test_reasonix_overlay.py`（秘密排除、白名单、路由） |
| 六/十 | Auditor 调度闭环：REVIEW→auditor→ACCEPT→VERIFIED→criteria PASS→COMPLETED；REVISE→READY；BLOCK→决策门；REJECT→FAILED；幂等+崩溃恢复 | **IMPLEMENTED** | audit_gate.py + dispatch_audit_run/collect_audit；`tests/unit/test_audit_gate.py`（7）+ recovery 测试 |
| 六 | deterministic live smoke（含重启恢复不重复模型调用/Evidence） | **IMPLEMENTED**（FakeExecutor 常跑版本）；**GATED**（真实 reasonix 版本，`RESEARCHD_RUN_REAL_SMOKE=1`） | `tests/e2e/test_live_smoke.py`：workspace 固定输入→真实 Artifact+hash+路径校验→Evidence Candidate→audit→VERIFIED→REVIEW→COMPLETED→重启零重复；路径逃逸 fail-closed |
| 七 | Reporter：移除 Evidence 阈值 milestone；报告仅基于真实状态（新 Verified Evidence 逐条引用/Claim 变化/实质 Issue/Blocking Decision/真正 Milestone/Exception/Digest）；结论引用证据、动作引用真实 Task | **IMPLEMENTED** | extensions.py `check_milestones`（first-completed / first-decision-applied 事件，幂等）；reporter.py `_evidence_bottom_line`/`_active_task_actions`；空话扫描扩展；`tests/unit/test_reporter_live.py` |
| 八 | ACP 身份：cc_project/cc_session_key/cc_user_id 缺失 fail-closed+诊断；不自动映射未知用户为 PI；Decision answer 保留真实 platform user id/version/幂等键 | **IMPLEMENTED**（cc-connect 侧补丁已验证；端到端点击 GATED） | agent.py `_session_new` fail-closed + env 注入读取；cc-connect patch 注入 `CC_USER_ID`（Go 断言测试）；`tests/unit/test_acp_identity.py` |

## 2. 顺带修复的真实缺陷

- `researchd pilot` 从未注册（定义在 `main()` 调用之后）→ 注册为 `pilot create`。
- `researchctl` UDS 下不带 Bearer token（服务端 B-08 全 transport 强制 → 此前 mutating 调用必 401）。
- `_evaluate_decision_candidates` 用 `metadata_json={"decisions_evaluated": True}` 整列覆盖 → 丢失 run 的 role/skills/context_id；改为 `json_set` 合并。
- `apply_result` 在 register_artifact（内部已保存）之后重复保存 artifact → 配置 workspace_root 时 UNIQUE 冲突；改为单次保存。
- `_build_delivery_port` 只支持 fake；planner profile 传空 `{}`；milestone 阈值占位；FeishuDocClient 全 PENDING——均已实现。

## 3. 测试命令与结果

```bash
uv run pytest -q        # 183 passed, 4 skipped（真实平台 conformance/真实 smoke 门控项）
go test ./core/ ./platform/feishu/   # cc-connect patch（独立克隆，46s + 31s 全通过）
uv run researchctl doctor            # 只读健康检查
```

## 4. LIVE VERIFIED 与 GATED 边界

**LIVE VERIFIED（本阶段已完成的真实验证）**：
- cc-connect patch 在干净克隆上应用、编译（core/platform/agent/cmd）与 go test 全通过。
- researchd 全部单元/集成/恢复/e2e/本地 conformance 测试通过（183）。

**GATED（代码就绪，等待宿主授权后执行）**：
1. 真实 Reasonix 模型 smoke（planner/worker/auditor 真实调用）→ `RESEARCHD_RUN_REAL_SMOKE=1`（费用授权）。
2. cc-connect 真实运行（带飞书凭据）→ Delivery API POST/重复 POST/PATCH/崩溃恢复实测 → `researchctl delivery test`。
3. 真实飞书 staging 群发送/更新/按钮点击（Decision answer 端到端保留身份）。
4. Feishu Docx 真实 conformance → `RESEARCHD_DOC_TEST_ID`。
5. interdisciplinary-citation-pilot（必须等 1 通过后才启动）。
6. 真实 D-002 决策与里程碑。

## 5. 发布 v0.1.1 的剩余判据（未满足即不发布）

- [ ] Reasonix 真实 Worker 在项目 workspace 创建 Artifact（smoke GATED）
- [ ] 独立 Auditor 真实运行（同上）
- [ ] Evidence provenance 可追溯（代码已验证；真实链路 GATED）
- [ ] cc-connect 真实卡片发送/点击/原地更新（GATED）
- [ ] Decision exactly-once（本地已验证；真实点击 GATED）
- [ ] Feishu Docx 真实增量同步（GATED）
- [ ] 服务重启无重复模型调用/Evidence/消息（Fake 链路已验证；真实链路 GATED）
- [ ] 原始 Executor 输出无直达飞书路径（代码审计通过：outbox 仅 ReportSpec body）
- [x] 全部单元、集成、恢复、e2e 与本地 conformance 测试通过（183+4）
