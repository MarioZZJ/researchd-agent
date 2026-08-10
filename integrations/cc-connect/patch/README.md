# cc-connect Delivery API 补丁

为 `cc-connect`（v1.4.1，源码 `/home/zhengzj22/cc-connect`）增加窄出站 Delivery API，
供 `researchd` 投递报告并原地更新卡片。**不修改外部仓库工作树**——补丁以可应用
patch 交付。

## 补丁内容（`delivery-api.patch`）

4 处改动：

1. `core/interfaces.go` — 新增可选接口：
   - `MessageHandleSender.SendHandle(ctx, rctx, content) (string, error)`：发送并返回平台消息 id；
   - `MessageUpdaterByID.UpdateMessageByID(ctx, messageID, content) error`：按消息 id 原地更新。
2. `platform/feishu/feishu.go` — 实现两个接口：
   - `SendHandle`（新消息路径复用 `createMessageHandle`，返回 `resp.Data.MessageId`）；
   - `UpdateMessageByID`（复用 `patchCardMessage`，仅需 message id）。
3. `core/engine.go` — `SendToSessionWithHandle` + `UpdateMessageByIDInSession`。
4. `core/management.go` — 路由（幂等语义：write-ahead reservation）：
   - `POST /api/v1/projects/{name}/deliveries`：`{"session_key","message","idempotency_key"}` → `{"platform_message_id","idempotency_key"}`
   - `PATCH /api/v1/projects/{name}/deliveries/{platform_message_id}`：`{"session_key","message"}` → 原地更新
   - 同 `idempotency_key` 重放返回同一 `platform_message_id`（`~/.cc-connect/deliveries.json` 持久化，进程内 `sync.Mutex` 串行化）；
   - 发送前先写入 `__in_flight__` 占位：发送与落盘之间崩溃时，重试同 key 返回 409（in progress），不会重复发送；
   - 发送失败自动清除占位，允许重试。

## 安装

```bash
cd /home/zhengzj22/cc-connect
git apply /home/zhengzj22/Documents/researchd-agent/integrations/cc-connect/patch/delivery-api.patch
# 构建并重启（需 Go 1.25 工具链；本机无 Go，见下方验证说明）
make build && make install   # 或按项目 CONTRIBUTING 的构建方式
systemctl --user restart cc-connect
```

## 回滚

```bash
cd /home/zhengzj22/cc-connect
git apply --check -R /home/zhengzj22/Documents/researchd-agent/integrations/cc-connect/patch/delivery-api.patch
git apply -R /home/zhengzj22/Documents/researchd-agent/integrations/cc-connect/patch/delivery-api.patch
make build && make install   # 重新构建并安装（仅 reverse patch 不会更新已安装二进制）
systemctl --user restart cc-connect
# 验证：curl -s http://127.0.0.1:9820/api/v1/status 恢复补丁前行为
```

> 注意：`deliveries.json`（`~/.cc-connect/deliveries.json`，0600）是补丁产生的幂等映射；
> 回滚后可保留（无害）或删除。

## 验证

```bash
# 补测命令（安装后）：
curl -s -X POST http://127.0.0.1:9820/api/v1/projects/<project>/deliveries \
  -H "Content-Type: application/json" -H "Authorization: Bearer $CC_TOKEN" \
  -d '{"session_key":"<key>","message":"<text>","idempotency_key":"smoke-1"}'
curl -s -X PATCH http://127.0.0.1:9820/api/v1/projects/<project>/deliveries/<platform_message_id> \
  -H "Content-Type: application/json" -H "Authorization: Bearer $CC_TOKEN" \
  -d '{"session_key":"<key>","message":"<updated text>"}'
```

## 状态

- **B-06（本机无 Go 工具链，docker registry 不可达）**：patch 未在真实 Go 编译器中验证。
  已人工核对全部引用（`resp.Data.MessageId` 指针解引用、`replyMessage`/`buildReplyContent`/
  `patchCardMessage`/`resolveOutboundSessionTarget` 签名与现有调用一致）。
  解除条件：具备 Go 1.25 后执行 `go build ./...` 再安装。
- 真实发送（POST/PATCH 到飞书）为外部操作，GATED（B-01），首版由 FakeDeliveryPort 验证。
