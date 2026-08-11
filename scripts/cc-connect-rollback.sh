#!/usr/bin/env bash
# Rollback a cc-connect binary install to the pre-install state.
# Requires the manifest written by cc-connect-install.sh.
set -euo pipefail

BIN_DIR="${CC_CONNECT_BIN_DIR:-$HOME/.local/lib/node_modules/cc-connect/bin}"
BIN="$BIN_DIR/cc-connect"
MANIFEST="$BIN_DIR/.researchd-rollback.json"

if [ ! -f "$MANIFEST" ]; then
  echo "ERROR: no rollback manifest at $MANIFEST" >&2; exit 2
fi
BAK=$(python3 -c "import json; print(json.load(open('$MANIFEST'))['backup'])")
OLD_SHA=$(python3 -c "import json; print(json.load(open('$MANIFEST'))['old_sha256'])")

if [ ! -f "$BAK" ]; then
  echo "ERROR: backup missing: $BAK" >&2; exit 2
fi
CUR_SHA=$(sha256sum "$BIN" | cut -d' ' -f1)
echo ">> restoring $BAK (old sha256=$OLD_SHA, current=$CUR_SHA)"
cp -p "$BAK" "$BIN"
chmod +x "$BIN"

echo ">> restarting cc-connect.service"
systemctl --user restart cc-connect
sleep 3
systemctl --user is-active --quiet cc-connect || { echo "ERROR: rollback restart failed" >&2; exit 3; }
RESTORED_SHA=$(sha256sum "$BIN" | cut -d' ' -f1)
[ "$RESTORED_SHA" = "$OLD_SHA" ] || { echo "ERROR: sha mismatch after rollback" >&2; exit 3; }
echo "== rolled back to sha256=$OLD_SHA =="
