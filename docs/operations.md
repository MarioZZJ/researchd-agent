# researchd 运维手册（IMPLEMENTATION.md §27）

生产形态：`researchd service` 作为 systemd service 运行，日志进 journald，
数据库与工作区位于 data 根，备份/恢复/导出由 `researchctl` 子命令完成。

## 1. 安装与启动

```bash
# 准备环境文件（模板 deploy/env.example；secret 永不进 Git）
cp deploy/env.example deploy/researchd.env   # 修改 RESEARCHD_API__TOKEN 等
chmod 600 deploy/researchd.env

# 安装 user unit
cp deploy/systemd/researchd.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now researchd
```

> **本机限制（B-07）**：本容器 `/home`、`/run/user/3001`、`~/.config` 均只读
> 且无 sudo，无法持久安装 unit。已完成等价验证：`systemd-analyze verify`
> 通过；真实进程 kill -9 后重启恢复 readyz。
> 有权限时的执行命令即上面的 4 行。

## 2. 数据库初始化与迁移

```bash
uv run researchd migrate                       # 空库跑全部 Alembic migration
# 服务运行期间 migrate 会被 data-dir 排他锁拒绝（预期行为）
```

## 3. 健康检查

```bash
curl --unix-socket .data/run/researchd.sock http://localhost/healthz
curl --unix-socket .data/run/researchd.sock http://localhost/readyz
# 所有写接口（POST/PATCH）无论 UDS/TCP 都要求 Bearer token（威胁模型 T4/B-08）：
curl --unix-socket .data/run/researchd.sock -H "Authorization: Bearer $TOKEN" \
  -X POST http://localhost/v1/projects -d '{"project_id":"p","name":"p"}'
```

## 4. 日志

```bash
journalctl --user -u researchd -f              # 跟踪
journalctl --user -u researchd --since "10 min ago"
```

## 5. 优雅停止 / 自动恢复

```bash
systemctl --user stop researchd                # SIGTERM → 排空调度器 → 关闭
                                              # executor 会话 → 释放 data-dir 锁
systemctl --user start researchd
systemctl --user restart researchd
```
`Restart=on-failure RestartSec=2`：进程崩溃（含 kill -9）后自动拉起；
孤儿 run 由 scheduler 在下次 tick 的 orphan reconciliation 回收（INTERRUPTED→requeue）。

## 6. 备份（SQLite 在线安全备份）

服务运行期间可直接备份（WAL 安全快照，不中断写入）：

```bash
uv run researchd backup                        # → .data/backups/researchd-<ts>.db + manifest
uv run researchd backup --backup-dir /mnt/backups
uv run researchd backup --no-workspaces        # 只要数据库
```
产物：`researchd-<ts>.db`（一致性快照）、`workspaces-<ts>.tar.gz`（可选）、
`manifest-<ts>.json`（清单）。建议 cron 每日执行。

## 7. 恢复演练（绝不覆盖唯一副本）

```bash
uv run researchd restore --db-backup backups/researchd-<ts>.db \
  --target-dir /tmp/restore-check              # 默认 dry-run：校验完整性
uv run researchd restore --db-backup backups/researchd-<ts>.db \
  --workspaces backups/workspaces-<ts>.tar.gz \
  --target-dir /tmp/restore-check --apply      # 真正恢复到全新目录
```
恢复目标必须为空目录；恢复后 `researchctl doctor` 验证。

## 8. 项目状态导出

```bash
uv run researchd export --project-id <id> --out <id>.export.json
```
确定性 JSON：project/tasks/runs/evidence/claims/decisions/issues。

## 9. cc-connect 补丁安装/回滚

见 `integrations/cc-connect/README.md`（patch 文件 + 安装/回滚命令）。
patch 已在独立克隆的 cc-connect v1.4.1（5d4c96d）分支上验证：`git apply` 干净、
`go build ./core/... ./platform/... ./agent/... ./cmd/...` 通过（`web/embed` 需预构建
前端 dist，为既有前置）、`go test ./core/ ./platform/feishu/` 全部通过（含新增
Delivery 幂等存储测试）。回滚 = 删除验证克隆（原仓库只读未动）。

## 9b. 真实投递/文档冒烟（researchctl）

```bash
# 发送一张真实 interactive card 到 staging chat，并立即 PATCH 原地更新
uv run researchctl delivery test [--chat-id oc_xxx]      # 服务端需 delivery=cc_connect + token
# 对显式提供的 staging 飞书文档做块级 create/update/read/delete 往返
uv run researchctl document test --document-id <docx_id>  # 服务端需 LARK_APP_ID/SECRET
```
两个命令都是显式、用户触发的 mutating 探测（Bearer token 保护），不会自动触发；
目标必须由用户显式提供。

## 9c. 飞书接入：应用角色、权限与接线

两个飞书应用职责严格分离（沙箱 `/home` 只读，cc-connect 运行配置放
`~/.cache/cc-connect-live/config.toml`，0600）：

| 角色 | 应用 | 用途 | 权限（应用身份） |
|---|---|---|---|
| researchd 机器人（被测） | `cli_aaf007476338dd2c` | 群消息收发 + 创建/更新项目 Docx | `im:message:readonly`、`im:message:send_as_bot`、`im:chat:readonly`、`docx:document`、`docs:doc`（共享文档到群/PI） |
| testbot（PI 测试驱动） | `cli_aaf9998d25f89bcf` | 群内自动发测试命令 + 轮询历史验收 | `im:message:send_as_bot`、`im:message:readonly`、`im:chat:readonly`；**不授予任何 docx/docs/drive 权限、不配事件订阅**（防机器人循环） |

权限变更流程：开发者后台 → 权限管理开通 → 「版本管理与发布」创建新版本 →
发布 → 管理员审批 → 生效。共享 Docx 协作者接口实测要求
`[drive:drive, drive:file, docs:doc]`，最小集为 `docs:doc`
（`docs:permission.member:create` 不在该接口要求列表中）。

接线与白名单：

- researchd 应用跑在 cc-connect（researchd project，agent=acp → `researchd acp`），
  `work_dir` = 本仓库；管理 API `:9820`（token 在 `[management]`）。
- 平台侧 `allow_chat` = 测试群 chat_id；`allow_from` = PI open_id 列表 +
  testbot 在 researchd 应用视角的 open_id（从事件日志 `message from
  unauthorized user user=<open_id>` 中取，先加白名单再重发）。**禁止
  `allow_from="*"`**。
- 免 @ 响应（`group_reply_all=true`）仅在「群白名单 + 发送者白名单」同时生效时启用。
- 防循环：testbot 不订阅事件、不接入 cc-connect，只发消息+读历史。
- 沙箱内 cc-connect 以 transient unit 运行（`/home` 只读无法写 systemd 配置）；
  崩溃自愈用 `Restart=on-failure`，一键恢复见 `scripts/cc-connect-live-start.sh`
  （重建 transient unit，配置/data 均在 `~/.cache/cc-connect-live/`）。
- 原 npm 版 cc-connect 及其配置/二进制在切换前已备份
  （`~/.cache/cc-connect.service.bak-*`、`~/.cache/cc-connect-config.toml.bak-*`、
  `~/.cache/cc-connect-original-*`），回滚 = 恢复备份并重建原 service。

## 10. 服务与配置权限检查

```bash
# 期望输出（services/doctor 也做只读检查）：
#   .data/               0700  （数据根）
#   .data/run/researchd.sock  0600（UDS）
#   deploy/researchd.env 0600  （含 secret 的环境文件）
#   .data/researchd.lock 0600  （排他锁）
ls -la .data/ .data/run/ deploy/researchd.env
```

## 11. researchctl doctor

```bash
uv run researchctl doctor
```
只读检查：数据库打开（mode=ro）、WAL/foreign_keys/busy_timeout/synchronous
PRAGMA、schema 存在、服务 socket 可达、data-dir 锁状态、权限位。

> 所有 mutating 调用（project create / pause / resume / reconcile / delivery test /
> document test）在 UDS 与 TCP 下都自动携带 Bearer token（B-08 服务端强制，
> researchctl 端已对齐）。
