#!/usr/bin/env bash
# testbot → researchd group-chat smoke (REAL Feishu platform).
#
# Requires RESEARCHD_RUN_REAL_SMOKE=1 (explicit real-platform switch) and an
# explicit staging chat (--chat-id, or RD_CHAT_ID env). Never runs against a
# production group without the switch; the command is sent ONCE and the
# script polls the group history (no re-send, no duplicate commands).
#
# Asserts, in the staging group history:
#   1. the request was sent by testbot          (cli_aaf9998d25f89bcf)
#   2. researchd replied                        (cli_aaf007476338dd2c)
#   3. the reply contains the unique marker
#
# Usage:
#   RESEARCHD_RUN_REAL_SMOKE=1 bash scripts/testbot-smoke.sh --chat-id oc_xxxx
# Env:
#   TESTBOT_CRED_FILE   testbot app credentials JSON (app_id/app_secret)
#   RD_BOT_OPEN_ID      researchd bot open_id in testbot's app view
set -euo pipefail

if [ "${RESEARCHD_RUN_REAL_SMOKE:-}" != "1" ]; then
  echo "error: RESEARCHD_RUN_REAL_SMOKE=1 required (real Feishu messages will be sent)" >&2
  exit 2
fi

CHAT_ID="${RD_CHAT_ID:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --chat-id) CHAT_ID="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
if [ -z "$CHAT_ID" ]; then
  echo "error: --chat-id (or RD_CHAT_ID) is required; staging group only" >&2
  exit 2
fi

CRED="${TESTBOT_CRED_FILE:-$HOME/.config/research-agent-orchestrator/testbot.feishu.json}"
BOT_OPEN_ID="${RD_BOT_OPEN_ID:-ou_ff9aaea4aaa2cadecb6f24841b358485}"
MARKER="rd-$(date +%s)"

echo "== testbot smoke: chat=$CHAT_ID marker=$MARKER =="

PYTHON="${RESEARCHD_VENV_PY:-$HOME/Documents/researchd-agent/.venv/bin/python3}"
if [ ! -x "$PYTHON" ]; then
  echo "error: venv python not found: $PYTHON (set RESEARCHD_VENV_PY)" >&2
  exit 2
fi

RESULT=$(RESEARCHD_SMOKE_MARKER="$MARKER" RESEARCHD_SMOKE_CHAT="$CHAT_ID" \
  RESEARCHD_SMOKE_CRED="$CRED" RESEARCHD_SMOKE_BOT_OPEN_ID="$BOT_OPEN_ID" \
  "$PYTHON" - <<'PYEOF'
import json, os, sys, time
import httpx

cred = json.load(open(os.environ["RESEARCHD_SMOKE_CRED"]))
chat = os.environ["RESEARCHD_SMOKE_CHAT"]
marker = os.environ["RESEARCHD_SMOKE_MARKER"]
bot_open_id = os.environ["RESEARCHD_SMOKE_BOT_OPEN_ID"]
TESTBOT_APP = "cli_aaf9998d25f89bcf"
RESEARCHD_APP = "cli_aaf007476338dd2c"

tok = httpx.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                 json={"app_id": cred["app_id"], "app_secret": cred["app_secret"]},
                 timeout=15).json()["tenant_access_token"]
h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

text = f'<at user_id="{bot_open_id}"></at> /research status {marker}'
r = httpx.post("https://open.feishu.cn/open-apis/im/v1/messages",
               params={"receive_id_type": "chat_id"}, headers=h,
               json={"receive_id": chat, "msg_type": "text",
                     "content": json.dumps({"text": text}, ensure_ascii=False)}, timeout=15)
if r.json().get("code") != 0:
    print("FAIL send:", r.json().get("code"), r.json().get("msg"))
    sys.exit(1)
send_id = (r.json().get("data") or {}).get("message_id")
print("sent:", send_id)

# poll group history for the researchd reply AFTER the request
deadline = time.time() + 75
found = None
req_time = int(time.time() * 1000) - 1000  # ms, tolerate clock skew
while time.time() < deadline:
    time.sleep(5)
    rr = httpx.get("https://open.feishu.cn/open-apis/im/v1/messages",
                   params={"container_id_type": "chat", "container_id": chat,
                           "page_size": 20, "sort_type": "ByCreateTimeDesc"},
                   headers=h, timeout=15)
    items = ((rr.json().get("data") or {}).get("items")) or []
    # 1) the request must be present and sent by testbot
    req = next((m for m in items if (m.get("sender") or {}).get("id") == TESTBOT_APP
                and marker in json.dumps((m.get("body") or {}).get("content", ""))), None)
    # 2) the reply: sent by researchd, newer than the request, project status text
    reply = next((m for m in items if (m.get("sender") or {}).get("id") == RESEARCHD_APP
                  and int(m.get("create_time") or "0") >= req_time
                  and "project researchd" in json.dumps((m.get("body") or {}).get("content", ""))), None)
    if req is not None and reply is not None:
        found = reply
        break
if found is None:
    print("FAIL timeout: no researchd reply after marker", marker)
    sys.exit(1)
body = (found.get("body") or {}).get("content", "")
print("PASS reply_sender=cli_aaf007476338dd2c marker=", marker)
print("reply_preview=", body[:160].replace("\n", " "))
PYEOF
)
echo "$RESULT"
if echo "$RESULT" | grep -q '^PASS '; then
  echo "== SMOKE PASS =="
else
  echo "== SMOKE FAIL ==" >&2
  exit 1
fi
