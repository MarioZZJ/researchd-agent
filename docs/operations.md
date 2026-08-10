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
# TCP 形态（transport=tcp + token）：curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/healthz
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

见 `integrations/cc-connect/README.md`（patch 文件 + 安装/回滚命令；本机
无 Go 工具链 → patch 可应用但未安装，见 docs/blockers.md B-06）。

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
