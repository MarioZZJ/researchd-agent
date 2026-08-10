"""Codex HOME overlay (IMPLEMENTATION.md §23 Phase 5).

~/.codex is read-only in this environment; codex app-server needs a writable
state dir (SQLite) plus auth. We give each instance an isolated CODEX_HOME
inside the restricted data dir.

VERIFIED on this host (codex-cli 0.146.0): copying ~/.codex/config.toml makes
thread/start fail with "failed to load configuration" (the config references
files that do not exist in a minimal home), while an EMPTY home (or one with
only auth files) works end-to-end. Therefore only auth files are copied —
codex runs with its built-in defaults and per-call model/effort overrides.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

CODEX_HOME_DIRNAME = "codex-home"

# auth files copied from ~/.codex (nothing else; config.toml is NOT copied —
# see module docstring)
_COPY_FILES = ("auth.json", "auth.chatgpt.json")


class CodexHomeError(RuntimeError):
    pass


def ensure_codex_home(data_dir: str | Path) -> Path:
    home = Path(data_dir) / CODEX_HOME_DIRNAME
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    global_home = Path.home() / ".codex"
    if global_home.exists():
        for name in _COPY_FILES:
            src = global_home / name
            if src.is_file():
                dst = home / name
                shutil.copyfile(src, dst)
                os.chmod(dst, 0o600)
    return home
