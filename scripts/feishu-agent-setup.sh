#!/usr/bin/env bash
# 飞书扫码创建智能体（cc-connect setup）— 在你的真实终端运行（沙箱外）
#
#   bash scripts/feishu-agent-setup.sh [project] [device_code]
#
# 流程：
#   1. begin  → 打印 qr_url，用浏览器打开、飞书 App 扫码确认（扫码即在飞书
#      账号服务注册一个 PersonalAgent 智能体应用，自动下发 app_id/app_secret）
#   2. poll   → 每 5s 轮询直到 completed / denied / expired
#   3. save   → 把 app_id/app_secret 写入 ~/.cc-connect/config.toml 的项目配置
#   4. 提示重启 cc-connect.service（restart_required）
#
# 参数：project 默认取 config.toml 第一个 [[projects]] 名称；device_code 可在
# 轮询中断后续传（resume），不用重新扫码。
set -euo pipefail

CONFIG="${CC_CONNECT_CONFIG:-$HOME/.cc-connect/config.toml}"
BASE="${CC_CONNECT_BASE:-http://127.0.0.1:9820}"

if [ ! -r "$CONFIG" ]; then
  echo "ERROR: $CONFIG 不可读" >&2
  exit 2
fi

# ── 提取 management token（不打印）与端口 ──
TOKEN=$(sed -n '/\[management\]/,/^\[/p' "$CONFIG" | sed -n 's/^[[:space:]]*token[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
if [ -z "$TOKEN" ]; then
  echo "ERROR: 未在 $CONFIG 的 [management] 段找到 token" >&2
  exit 2
fi

PORT=$(sed -n '/\[management\]/,/^\[/p' "$CONFIG" | sed -n 's/^[[:space:]]*port[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p' | head -1)
if [ -n "$PORT" ]; then
  BASE="http://127.0.0.1:$PORT"
fi

PROJECT="${1:-}"
if [ -z "$PROJECT" ]; then
  PROJECT=$(sed -n '/\[\[projects\]\]/,/^\[\[/p' "$CONFIG" | sed -n 's/^[[:space:]]*name[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
fi
if [ -z "$PROJECT" ]; then
  echo "ERROR: 无法从 $CONFIG 推断项目名，请显式传参: $0 <project>" >&2
  exit 2
fi

# the management token NEVER appears in argv (ps-readable): a 0600 curl
# config file carries the Authorization header instead
CURL_CFG=$(mktemp /tmp/cc-setup-curl.XXXXXX)
chmod 600 "$CURL_CFG"
printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" > "$CURL_CFG"
trap 'rm -f "$CURL_CFG"' EXIT
unset TOKEN
DEVICE_CODE="${2:-}"

echo "== cc-connect setup: project=$PROJECT base=$BASE =="

if [ -z "$DEVICE_CODE" ]; then
  echo ">> 请求飞书账号服务注册（PersonalAgent / client_secret / open_id）..."
  BEGIN=$(curl -sS --max-time 20 --config "$CURL_CFG" -X POST "$BASE/api/v1/setup/feishu/begin")
  DEVICE_CODE=$(printf '%s' "$BEGIN" | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['device_code'])")
  QR_URL=$(printf '%s' "$BEGIN" | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['qr_url'])")
  INTERVAL=$(printf '%s' "$BEGIN" | python3 -c "import json,sys; print(json.load(sys.stdin)['data'].get('interval', 5))")
  echo
  echo "############################################################"
  echo "#  请用手机飞书 App 扫描下方二维码，确认创建智能体：        #"
  echo "#  （扫码即绑定 cc-connect，自动完成应用注册与授权）        #"
  echo "############################################################"
  echo
  # 终端直接渲染二维码（qr_url 已含 PersonalAgent 注册参数与权限）
  if [ -d ".venv" ] || command -v uv >/dev/null 2>&1; then
    (cd "$(dirname "$0")/.." && uv run --quiet python3 - "$QR_URL" <<'PYEOF'
import segno, sys
segno.make(sys.argv[1], error="m").terminal(compact=True, border=1)
PYEOF
) 2>/dev/null || {
      echo "（无法渲染二维码，请用链接扫码）"
      echo "  $QR_URL"
    }
  else
    echo "  $QR_URL"
  fi
  echo
  echo "  URL 备选: $QR_URL"
  echo "  （扫码确认后脚本会自动轮询，最多 15 分钟；device_code=$DEVICE_CODE）"
  echo
else
  INTERVAL=5
  echo ">> 使用已有 device_code 继续轮询（resume）"
fi

# ── 轮询直到 completed ──
STATUS="pending"
DEADLINE=$((SECONDS + 900))
while [ "$STATUS" = "pending" ]; do
  [ "$SECONDS" -ge "$DEADLINE" ] && { echo "ERROR: 轮询超时（15 分钟）。可重跑: $0 $PROJECT $DEVICE_CODE" >&2; exit 4; }
  sleep "$INTERVAL"
  POLL=$(curl -sS --max-time 20 --config "$CURL_CFG" -H "Content-Type: application/json" \
    -d "{\"device_code\": \"$DEVICE_CODE\"}" "$BASE/api/v1/setup/feishu/poll")
  STATUS=$(printf '%s' "$POLL" | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['status'])")
  echo "   ... status=$STATUS ($(date +%H:%M:%S))"
done

if [ "$STATUS" = "denied" ]; then
  echo "ERROR: 扫码被拒绝（access_denied）" >&2; exit 5
fi
if [ "$STATUS" = "expired" ]; then
  echo "ERROR: 授权码已过期，请重跑脚本重新扫码" >&2; exit 5
fi
if [ "$STATUS" != "completed" ]; then
  printf 'ERROR: 轮询异常: %s\n' "$POLL" >&2; exit 5
fi

APP_ID=$(printf '%s' "$POLL" | python3 -c "import json,sys; print(json.load(sys.stdin)['data'].get('app_id',''))")
APP_SECRET=$(printf '%s' "$POLL" | python3 -c "import json,sys; print(json.load(sys.stdin)['data'].get('app_secret',''))")
PLATFORM=$(printf '%s' "$POLL" | python3 -c "import json,sys; print(json.load(sys.stdin)['data'].get('platform','feishu'))")
OWNER=$(printf '%s' "$POLL" | python3 -c "import json,sys; print(json.load(sys.stdin)['data'].get('owner_open_id',''))")

if [ -z "$APP_ID" ] || [ -z "$APP_SECRET" ]; then
  # never echo the raw platform body: it may contain the secret
  echo "ERROR: 扫码响应缺少 app_id/app_secret（响应内容不显示）" >&2; exit 5
fi

echo
echo "== 扫码完成：智能体已创建 =="
echo "  platform       = $PLATFORM"
echo "  app_id         = $APP_ID"
echo "  owner_open_id  = $OWNER"
echo "  app_secret     = 已获取（只写入配置，不在终端/历史/日志显示）"
echo

# ── save 到项目配置 ──
echo ">> 保存到项目 $PROJECT ..."
BAK="$CONFIG.bak-$(date +%Y%m%d-%H%M%S)"
cp "$CONFIG" "$BAK" && chmod 600 "$BAK" && echo "   已备份配置到 $BAK（0600，旧凭据可回滚）"
# secret passes via a 0600 temp file (no argv, no env, no ps/history
# leakage): same-uid observers cannot read another process's file with
# 0600 perms; the file is removed immediately after use
SEC_FILE=$(mktemp /tmp/cc-setup-secret.XXXXXX)
chmod 600 "$SEC_FILE"
printf '%s' "$APP_SECRET" > "$SEC_FILE"
SAVE=$(python3 - "$PROJECT" "$APP_ID" "$PLATFORM" "$OWNER" "$SEC_FILE" <<'PYEOF' | curl -sS --max-time 20 --config "$CURL_CFG" -H "Content-Type: application/json" --data-binary @- "$BASE/api/v1/setup/feishu/save"
import json, sys
project, app_id, platform, owner, sec_file = sys.argv[1:6]
app_secret = open(sec_file).read().strip()
print(json.dumps({
    "project": project, "app_id": app_id,
    "app_secret": app_secret,
    "platform_type": platform, "owner_open_id": owner,
}))
PYEOF
)
rm -f "$SEC_FILE"
echo "   $(printf '%s' "$SAVE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('data', d).get('message', d))")"
unset APP_SECRET 2>/dev/null || true

echo
echo "== 下一步 =="
echo "  1) 重启 cc-connect:  systemctl --user restart cc-connect"
echo "  2) 查看状态:        systemctl --user status cc-connect"
echo "  3) 在飞书里给该机器人发条消息验证收发"
echo
echo "== 权限检查（参考飞书开放平台文档 open.feishu.cn）=="
echo "  两个应用职责不同，权限分开配置（应用身份权限）："
echo
echo "  A) researchd 机器人（被测应用，如 cli_aaf007476338dd2c）"
echo "    - im:message:readonly     接收/读取消息事件"
echo "    - im:message:send_as_bot  以应用身份回复"
echo "    - im:chat:readonly        读取群信息（白名单校验）"
echo "    - docx:document           创建/读取/更新 Docx 文档（聚合权限）"
echo "    - docs:doc                把文档共享给群/用户（实测：协作者接口要求"
echo "                              [drive:drive, drive:file, docs:doc]，其中"
echo "                              docs:doc 为最小集；docs:permission.member:create"
echo "                              不在共享接口要求列表中）"
echo "    - 事件订阅：im.message.receive_v1（长连接模式）"
echo
echo "  B) testbot 测试驱动（仅测试用，如 cli_aaf9998d25f89bcf）"
echo "    - im:message:send_as_bot  群内自动发送测试命令"
echo "    - im:message:readonly     轮询群历史验证回复（自动验收）"
echo "    - im:chat:readonly        核对测试群信息"
echo "    不给 testbot 授予 docx:/docs:/drive: 任何权限，不做事件订阅"
echo "    （避免机器人自动回复形成循环）。"
echo
echo "  权限变更后需在「版本管理与发布」创建新版本并发布、管理员审批后才生效；"
echo "  researchd 建议在 cc-connect 平台配置 allow_chat=测试群 + allow_from="
echo "  testbot 的 open_id（应用视角，从收到的事件日志中取），禁止 allow_from=\"*\"。"
