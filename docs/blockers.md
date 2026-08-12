# Blockers（当前实施阻塞项）

> 状态：`OPEN`（阻塞）/ `GATED`（代码已完成，仅缺授权或外部凭据）/ `RESOLVED`。
> 规则：任何 BLOCKED 都附精确的补测/解除命令。阻塞项不阻止其余工作推进。
> 2026-08-12 更新：真实 reasonix smoke 4/4、真实飞书/cc-connect、真实 pilot 闭环
> 全部 LIVE VERIFIED，原 GATED 项相应解除。

## B-01 真实飞书/cc-connect 出站发送 — RESOLVED（2026-08-12 LIVE VERIFIED）

- 现象：飞书 app 凭据存在，但真实发送属外部可见操作。
- 解除：用户授权 + RD测试 群（oc_0ee1de4dc1c26b296570847aeb177a53）实测：
  - `researchctl delivery test` 卡片发送 + PATCH 原地更新（updated:true）；
  - testbot → researchd 群消息闭环 SMOKE PASS；
  - pilot 决策卡（D-002/D-47B6C29D...）真实入群（platform_message_id 回执）。

## B-02 Codex App Server 真实 conformance — GATED

- 现象：协议 schema 完整（v2），但真实 turn 会调用付费/外部模型。
- 解除条件：授权后执行 `uv run pytest tests/conformance -m codex`（真实 transport 标记）。
- 期间方案：fake transport conformance 全绿 + 真实能力矩阵记录为 schema-verified。
- 注：v0.1.1 发布以 reasonix 链路为准（pilot/smoke 均 reasonix）。

## B-03 Reasonix 真实 conformance — RESOLVED（2026-08-12 LIVE VERIFIED）

- 真实 smoke 4/4（RESEARCHD_RUN_REAL_SMOKE=1，直接链路 + service 重启六维快照零重复）；
- pilot 真实闭环：planner（gpt-5.6-sol）→ 4 worker（deepseek-v4-flash）→ 4 独立
  auditor（gpt-5.6-sol）全部真实调用成功；bwrap 隔离探针通过。

## B-04 /home 只读导致的标准路径不可用 — RESOLVED(by A-01/A-08)

- 数据根收敛到 `.data/`，UDS 放 `.data/run/`，systemd user unit 指向这些路径。

## B-05 无 sudo — RESOLVED(by 设计)

- 全部 user systemd + user 路径。

## B-06 cc-connect 补丁安装验证 — RESOLVED（2026-08-12）

- Go 工具链就绪；patch 版二进制已编译并安装运行
  （`~/.cache/cc-connect-researchd/bin/cc-connect-patched`，Management API :9820），
  真实 POST/PATCH/重复幂等/崩溃恢复实测（completion-report §1 三.1/三.2）。

## B-07 systemd user unit 持久安装 — RESOLVED（2026-08-12）

- `deploy/systemd/researchd.service` 已复制到 `~/.config/systemd/user/` 并
  `daemon-reload && start`（当前 active；`systemctl --user restart` 多次验证；
  此前只读结论因宿主挂载状态变化而解除——若再现只读，回退 transient unit）。
- 服务重启恢复：smoke 六维快照零重复（重启不重复模型调用/artifact/evidence/消息）。

## B-08 Executor 无 OS 级进程隔离（威胁模型 T4）— OPEN

- 现象：reasonix Executor 与 researchd service 同 uid；无 root/sudo。
- 已缓解（2026-08-12 加固）：**bwrap 文件系统隔离已启用**（namespace-local /proc、
  去宿主 ~/.cache bind、transcript dirfd 逐组件 O_NOFOLLOW + O_NONBLOCK、REASONIX_HOME
  overlay 0600、技能白名单、workspace 可写探针测试）；Executor env 白名单（不注入
  飞书/cc-connect token）；API socket 0600 + Bearer token；结构化输出 schema 门控。
- 影响：同 uid Executor 在 OS 层面仍可读 `.data/` 内 token 文件（协作层约束；
  bwrap 边界内不可见 DB/凭据）。
- 解除条件：宿主提供独立 uid 或更强的 sandbox 能力（landlock/seccomp 等）。
