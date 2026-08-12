#!/usr/bin/env bash
# Live reasonix smoke runner — MUST run OUTSIDE the bwrap sandbox, because the
# sandbox bind-mounts ~/.reasonix/.env to /dev/null (provider keys invisible ->
# gateway 401). Run it from the researchd-agent repo root, as the real user:
#
#   bash scripts/live-smoke.sh
#
# What it does: runs the deterministic live smoke (real planner/worker/auditor
# model calls, workspace-confined, audit gate, restart recovery) and prints
# the final assertions. Model costs are real (user-authorized).
set -euo pipefail
cd "$(dirname "$0")/.."

export RESEARCHD_RUN_REAL_SMOKE=1
export RESEARCHD_LOG_LEVEL=WARNING
echo "== sandbox check =="
if [ "$(readlink -f /proc/self/root)" != "/" ]; then
  echo "ERROR: running inside a sandbox/chroot; ~/.reasonix/.env is masked." >&2
  echo "Run this script OUTSIDE the sandbox (e.g. from a normal terminal)." >&2
  exit 3
fi
if [ ! -r "$HOME/.reasonix/.env" ]; then
  echo "ERROR: cannot read $HOME/.reasonix/.env (provider keys unreachable)." >&2
  exit 3
fi

echo "== running real reasonix live smoke (paid model calls) =="
uv run pytest tests/e2e/test_live_smoke.py::test_real_reasonix_live_smoke -q -s

echo
echo "== running service-process restart recovery verification =="
echo "   (real service child process; must NOT re-invoke the model,"
echo "    invocation/run/artifact/evidence counts must stay identical)"
uv run pytest tests/e2e/test_live_smoke.py::test_live_smoke_service_process_restart -q -s

echo
echo "== done: real planner/worker/auditor loop + restart recovery verified =="
echo "next: researchctl delivery test (needs cc_connect configured on the service)"
