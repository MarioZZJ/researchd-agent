#!/usr/bin/env bash
# Rebuild the cc-connect-live transient unit (researchd project + delivery patch).
# The sandbox mounts /home read-only, so no persistent systemd unit can be
# written; this script recreates the transient unit after reboot / manual stop.
# Config + data live in ~/.cache/cc-connect-live (0600, writable).
set -euo pipefail

BIN="$HOME/.cache/cc-connect-researchd/bin/cc-connect-patched"
CFG="$HOME/.cache/cc-connect-live/config.toml"
ENV_FILE="$HOME/Documents/researchd-agent/deploy/researchd.env"

if [ ! -x "$BIN" ]; then
  echo "error: patched binary not found: $BIN" >&2
  echo "build it first: see integrations/cc-connect/README.md" >&2
  exit 1
fi
if [ ! -f "$CFG" ]; then
  echo "error: live config not found: $CFG" >&2
  exit 1
fi

if systemctl --user is-active cc-connect-live >/dev/null 2>&1; then
  echo "cc-connect-live already active" >&2
  exit 0
fi

TOKEN=""
if [ -f "$ENV_FILE" ]; then
  TOKEN=$(grep '^RESEARCHD_API__TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2 || true)
fi

ARGS=(systemd-run --user --unit=cc-connect-live --property=Restart=on-failure
  --working-directory="$HOME")
if [ -n "$TOKEN" ]; then
  ARGS+=(--setenv="RESEARCHD_API__TOKEN=$TOKEN")
fi
ARGS+=("$BIN" --config "$CFG")

"${ARGS[@]}"
echo "cc-connect-live started (transient unit)"
