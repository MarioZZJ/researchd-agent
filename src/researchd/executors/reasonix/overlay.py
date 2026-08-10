"""Isolated REASONIX_HOME overlay (IMPLEMENTATION.md §15.2).

The user's global ~/.reasonix is read-only in this environment and must never
be modified. reasonix's ACP `session/new` requires a writable sessions
directory, so each Reasonix instance runs with REASONIX_HOME pointing into the
restricted run directory.

The overlay is MINIMAL: only the provider blocks (which carry the api keys
needed to run) are copied from the global config — bot/, MCP, speech and other
unrelated secrets stay out. The overlay file is 0600, lives inside the data
dir, and never enters Git, logs, or reports. Subprocesses get an environment
WHITELIST (not a full copy of os.environ) and a restricted working directory.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

OVERLAY_DIRNAME = "rx-overlay"

ENV_WHITELIST = ("PATH", "HOME", "REASONIX_HOME", "TERM", "LANG", "LC_ALL", "TZ")

_REQUIRED_PROVIDER_KEYS = {"name"}


class OverlayError(RuntimeError):
    pass


def _minimal_config(global_config: Path) -> str:
    """Extract [default_model] + [[providers]] blocks VERBATIM from the global
    config (text-level slicing, so nested TOML structures stay byte-identical).
    Everything else (bot, MCP, speech, …) is excluded."""
    lines = global_config.read_text().splitlines()
    blocks: list[list[str]] = []
    current: list[str] | None = None
    default_model = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("default_model ="):
            default_model = line
        if stripped.startswith("[[providers]]"):
            current = [line]
            blocks.append(current)
            continue
        if current is not None:
            if stripped.startswith("[") or stripped.startswith("[["):
                # ANY other TOML table header ends the provider block —
                # including [[mcp...]], [providers.*] sub-tables, [bot] etc.
                current = None
            else:
                current.append(line)
    if not blocks:
        raise OverlayError("global reasonix config has no [[providers]] blocks")
    out: list[str] = []
    if default_model is not None:
        out.append(default_model)
        out.append("")
    for block in blocks:
        out.extend(block)
        out.append("")
    return "\n".join(out)


def ensure_overlay(data_dir: str | Path) -> Path:
    """Create the minimal isolated overlay. Returns the overlay path."""
    overlay = Path(data_dir) / OVERLAY_DIRNAME
    overlay.mkdir(parents=True, exist_ok=True)
    os.chmod(overlay, 0o700)

    global_config = Path.home() / ".reasonix" / "config.toml"
    if not global_config.exists():
        raise OverlayError(
            f"global reasonix config {global_config} not found; "
            "cannot build isolated overlay (never modifying ~/.reasonix)"
        )
    dst = overlay / "config.toml"
    dst.write_text(_minimal_config(global_config))
    os.chmod(dst, 0o600)
    (overlay / "sessions").mkdir(parents=True, exist_ok=True)
    os.chmod(overlay / "sessions", 0o700)
    return overlay


def overlay_env(overlay: Path) -> dict:
    """Restricted environment for a reasonix subprocess (whitelist only)."""
    env = {k: v for k, v in os.environ.items() if k in ENV_WHITELIST}
    env["REASONIX_HOME"] = str(overlay)
    env["HOME"] = str(overlay)  # keep the agent's home inside the restricted dir
    return env


def overlay_workdir(overlay: Path) -> Path:
    """Restricted working directory for executor subprocesses."""
    work = overlay / "work"
    work.mkdir(parents=True, exist_ok=True)
    return work
