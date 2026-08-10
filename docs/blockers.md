# Blockers（当前实施阻塞项）

> 状态：`OPEN`（阻塞）/ `GATED`（代码已完成，仅缺授权或外部凭据）/ `RESOLVED`。
> 规则：任何 BLOCKED 都附精确的补测/解除命令。阻塞项不阻止其余工作推进。

## B-01 真实飞书/cc-connect 出站发送 — GATED

- 现象：飞书 app 凭据存在（cc-connect config 2 套、feishu.json 2 份），但使用凭据做真实发送属于外部可见操作。
- 解除条件：用户/门禁授权 + 提供目标 chat id（项目群、PI Inbox）。
- 解除后补测：`researchctl delivery test --to <chat-id>`；`POST /api/v1/projects/{p}/deliveries`。
- 期间方案：FakeDeliveryPort 全链路确定性测试（Phase 6），真实项在 compatibility-matrix 标 BLOCKED。

## B-02 Codex App Server 真实 conformance — GATED

- 现象：协议 schema 完整（v2），但真实 turn 会调用付费/外部模型。
- 解除条件：授权后执行 `uv run pytest tests/conformance -m codex`（真实 transport 标记）。
- 期间方案：fake transport conformance 全绿 + 真实能力矩阵记录为 schema-verified。

## B-03 Reasonix 真实 conformance — GATED（低风险）

- 现象：隔离 REASONIX_HOME 下 session 可创建；但真实 prompt 会调用 gateway 模型（付费）。
- 解除条件：授权后执行 `uv run pytest tests/conformance -m reasonix`（真实 transport 标记）。
- 期间方案：fake transport conformance 全绿；最小真实握手已在 Phase 0 亲自完成（initialize/session lease）。

## B-04 /home 只读导致的标准路径不可用 — RESOLVED(by A-01/A-08)

- 现象：/var/lib/researchd、/run/researchd、XDG 路径不可写。
- 处理：数据根收敛到 `.data/`（A-01），UDS 放 `.data/run/`（A-08），systemd user unit 指向这些路径。

## B-05 无 sudo — RESOLVED(by 设计)

- 现象：`sudo` 不可用（no new privileges）。
- 处理：全部 user systemd + user 路径，文档 §22 的"等价 XDG user 路径"按 A-01 落地。

## B-07 systemd user unit 无法持久安装（已验证）

- 现象：`cp deploy/systemd/researchd.service ~/.config/systemd/user/` → `Read-only file system`；`$XDG_RUNTIME_DIR/systemd/user`（/run/user/3001）同样只读；无 sudo（no new privileges）。
- 证据：`mount` 显示 `/home`、`/run/user/3001` 均为 ro；`~/.local`、`~/.config` 只读；`sudo -n true` 失败。
- 已完成等价验证：
  - `systemd-analyze verify deploy/systemd/researchd.service` → UNIT-SYNTAX-OK；
  - 真实进程 kill -9 崩溃 → 重启后 readyz 恢复（等价于 Restart=on-failure）；
  - 服务启动/优雅停止/healthz/readyz/journald 日志路径在 docs/operations.md 记录。
- 解除条件：宿主将 /home（或至少 ~/.config/systemd/user）挂载为 rw，或提供 sudo。
- 解除后执行：`cp deploy/systemd/researchd.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now researchd`。

## B-08 Executor 无 OS 级进程隔离（威胁模型 T4）

- 现象：reasonix/codex Executor 与 researchd service 同 uid 运行；本机无 root、无 bwrap/landlock 可用（sudo 不可用、容器 no-new-privileges）。
- 影响：同 uid 的 Executor 在 OS 层面**可读可写** `.data/`（DB/socket/workspace），可 rename/unlink DB、替换 workspace/symlink、绕过 advisory lock 与路径门控——"唯一写者/结构化输出门控"仅是协作式代码约束，威胁模型 T4 的隔离目标依赖 OS 边界，本机无法强制。
- 已缓解：Executor env 白名单（不注入飞书/cc-connect token）；overlay/codex-home 0600 且 gitignored；API socket 0600 + 写接口 token（同 uid 可读 token 文件，属协作层）；结构化输出 schema 门控。
- 解除条件：宿主提供独立 uid 或 sandbox（bwrap/unshare/landlock/seccomp）。
