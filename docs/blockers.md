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
