#!/usr/bin/env bash
# Install the verified cc-connect Delivery-API binary (GATED: run only after
# the user approves touching the LIVE cc-connect install).
#
#   bash scripts/cc-connect-install.sh /tmp/cc-connect-verify/bin/cc-connect
#
# - backs up the current binary + records its SHA-256 (rollback manifest)
# - installs the new binary, restarts cc-connect.service, verifies healthz
# - rollback: bash scripts/cc-connect-rollback.sh
set -euo pipefail

BIN_DIR="${CC_CONNECT_BIN_DIR:-$HOME/.local/lib/node_modules/cc-connect/bin}"
BIN="$BIN_DIR/cc-connect"
NEW="${1:?usage: cc-connect-install.sh <new-binary-path>}"
MANIFEST="$BIN_DIR/.researchd-rollback.json"

if [ ! -x "$NEW" ]; then
  echo "ERROR: new binary not executable: $NEW" >&2; exit 2
fi
if [ ! -f "$BIN" ]; then
  echo "ERROR: current binary missing: $BIN" >&2; exit 2
fi

OLD_SHA=$(sha256sum "$BIN" | cut -d' ' -f1)
NEW_SHA=$(sha256sum "$NEW" | cut -d' ' -f1)
TS=$(date +%Y%m%d-%H%M%S)
BAK="$BIN.cc-connect.bak-$TS"

echo ">> backing up $BIN -> $BAK (sha256=$OLD_SHA)"
cp -p "$BIN" "$BAK"
echo "{\"installed_at\": \"$TS\", \"backup\": \"$BAK\", \"old_sha256\": \"$OLD_SHA\", \"new_sha256\": \"$NEW_SHA\"}" > "$MANIFEST"
chmod 600 "$MANIFEST"

echo ">> installing (sha256=$NEW_SHA)"
cp "$NEW" "$BIN"
chmod +x "$BIN"

echo ">> restarting cc-connect.service"
systemctl --user restart cc-connect
sleep 3
if ! systemctl --user is-active --quiet cc-connect; then
  echo "ERROR: cc-connect.service failed to start — rolling back" >&2
  bash "$(dirname "$0")/cc-connect-rollback.sh"
  exit 3
fi
TOKEN=$(sed -n '/\[management\]/,/^\[/p' "$HOME/.cc-connect/config.toml" | sed -n 's/^[[:space:]]*token[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
PORT=$(sed -n '/\[management\]/,/^\[/p' "$HOME/.cc-connect/config.toml" | sed -n 's/^[[:space:]]*port[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p' | head -1)
PORT="${PORT:-9820}"
CODE=$(curl -sS --max-time 5 -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/api/v1/status")
echo ">> management api /status -> HTTP $CODE"
[ "$CODE" = "200" ] || { echo "ERROR: management api not healthy (HTTP $CODE)" >&2; exit 3; }
echo "== installed OK (rollback: bash scripts/cc-connect-rollback.sh) =="
