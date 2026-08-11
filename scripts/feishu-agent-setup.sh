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

AUTH="Authorization: Bearer $TOKEN"
DEVICE_CODE="${2:-}"

echo "== cc-connect setup: project=$PROJECT base=$BASE =="

if [ -z "$DEVICE_CODE" ]; then
  echo ">> 请求飞书账号服务注册（PersonalAgent / client_secret / open_id）..."
  BEGIN=$(curl -sS --max-time 20 -X POST -H "$AUTH" "$BASE/api/v1/setup/feishu/begin")
  DEVICE_CODE=$(printf '%s' "$BEGIN" | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['device_code'])")
  QR_URL=$(printf '%s' "$BEGIN" | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['qr_url'])")
  INTERVAL=$(printf '%s' "$BEGIN" | python3 -c "import json,sys; print(json.load(sys.stdin)['data'].get('interval', 5))")
  echo
  echo "############################################################"
  echo "#  请在浏览器打开以下链接，用飞书 App 扫码确认创建智能体：  #"
  echo "############################################################"
  echo
  echo "  $QR_URL"
  echo
  echo "（扫码确认后脚本会自动轮询，最多 15 分钟；device_code=$DEVICE_CODE）"
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
  POLL=$(curl -sS --max-time 20 -X POST -H "$AUTH" -H "Content-Type: application/json" \
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
  echo "ERROR: 响应缺少 app_id/app_secret: $POLL" >&2; exit 5
fi

echo
echo "== 扫码完成：智能体已创建（凭据仅本次显示，请妥善保存）=="
echo "  platform       = $PLATFORM"
echo "  app_id         = $APP_ID"
echo "  owner_open_id  = $OWNER"
echo "  app_secret     = $APP_SECRET"
echo

# ── save 到项目配置 ──
SAVE_JSON=$(python3 - "$PROJECT" "$APP_ID" "$APP_SECRET" "$PLATFORM" "$OWNER" <<'PYEOF'
import json, sys
project, app_id, app_secret, platform, owner = sys.argv[1:6]
print(json.dumps({
    "project": project, "app_id": app_id, "app_secret": app_secret,
    "platform_type": platform, "owner_open_id": owner,
}))
PYEOF
)
echo ">> 保存到项目 $PROJECT ..."
BAK="$CONFIG.bak-$(date +%Y%m%d-%H%M%S)"
cp "$CONFIG" "$BAK" && echo "   已备份配置到 $BAK（旧凭据可回滚）"
SAVE=$(curl -sS --max-time 20 -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d "$SAVE_JSON" "$BASE/api/v1/setup/feishu/save")
echo "   $(printf '%s' "$SAVE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('data', d).get('message', d))")"

echo
echo "== 下一步 =="
echo "  1) 重启 cc-connect:  systemctl --user restart cc-connect"
echo "  2) 查看状态:        systemctl --user status cc-connect"
echo "  3) 在飞书里给该机器人发条消息验证收发"
echo
echo "== 权限检查（参考飞书开放平台文档 open.feishu.cn）=="
echo "  扫码注册的智能体应用已带基础权限；如需完整能力，到"
echo "  开发者后台(open.feishu.cn) 该应用 → 权限管理，确认/开通："
echo "    - im:message                 获取与发送单聊、群组消息"
echo "    - im:message:send_as_bot     以应用身份发送消息（机器人发消息）"
echo "    - im:message:readonly        读取消息（事件处理/审计）"
echo "    - im:chat / im:chat:readonly 获取群组信息"
echo "    - im:resource                上传/下载图片、文件等消息资源"
echo "    - contact:user.base:readonly 获取用户基本信息（open_id 等）"
echo "  事件订阅（长连接模式）需订阅 im.message.receive_v1 等消息事件；"
echo "  权限变更后需在「版本管理与发布」创建新版本并发布生效。"
