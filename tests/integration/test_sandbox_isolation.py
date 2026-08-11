"""Real bubblewrap isolation probe (integration): the reasonix subprocess argv
built by _bwrap_command must make researchd secrets unreadable while keeping
the overlay and the project workspace usable. Requires bwrap on PATH."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from researchd.executors.reasonix.transport import _bwrap_command

pytestmark = pytest.mark.integration


def _has_bwrap() -> bool:
    import shutil

    return shutil.which("bwrap") is not None


@pytest.mark.skipif(not _has_bwrap(), reason="bubblewrap not available")
def test_bwrap_masks_researchd_secrets_keeps_workspace(tmp_path):
    data = tmp_path / "data"
    overlay = data / "rx-overlay"
    overlay.mkdir(parents=True)
    ws = data / "ws"
    ws.mkdir()
    (data / "researchd.db").write_text("DB-SECRET-MARKER")
    (ws / "marker.txt").write_text("WS-VISIBLE")
    home = Path.home()

    cmd = _bwrap_command("/bin/sh", overlay, ws)
    assert cmd[-2:] == ["/bin/sh", "acp"]
    probe = cmd[:-2] + [
        "/bin/sh",
        "-c",
        "echo DB=$(cat %s/researchd.db 2>&1); "
        "echo CC=$(ls %s 2>&1 | head -1); "
        "echo RX=$(ls %s 2>&1 | head -1); "
        "echo SSH=$(ls %s 2>&1 | head -1); "
        "echo WS=$(cat %s/marker.txt 2>&1); "
        "echo OV=$(ls %s 2>&1 | wc -l)" % (data, home / ".cc-connect", home / ".reasonix", home / ".ssh", ws, overlay),
    ]
    r = subprocess.run(probe, capture_output=True, text=True, timeout=60)
    out = r.stdout
    assert "DB-SECRET-MARKER" not in out, "researchd DB leaked into executor namespace"
    db_line = [ln for ln in out.splitlines() if ln.startswith("DB=")]
    assert db_line and "No such file" in db_line[0], f"DB not masked: {out}"
    for prefix in ("CC=", "RX=", "SSH="):
        line = [ln for ln in out.splitlines() if ln.startswith(prefix)][0]
        assert line == prefix, f"{prefix} mask failed: {out}"
    assert any(ln == "WS=WS-VISIBLE" for ln in out.splitlines()), f"workspace unusable: {out}"
