"""Isolated REASONIX_HOME overlay (IMPLEMENTATION.md §15.2).

The user's global ~/.reasonix is read-only in this environment and must never
be modified. reasonix's ACP `session/new` requires a writable sessions
directory, so each Reasonix instance runs with REASONIX_HOME pointing into the
restricted run directory.

The overlay is a WHITELIST:
- top-level config keys actually used by researchd (default_model,
  planner_model, subagent_model, subagent_effort, subagent_models,
  max_subagent_depth, max_subagent_concurrency, max_parallel_writers) plus
  the [[providers]] blocks (which carry the api keys needed to run);
- a whitelisted set of user skills (reviewer, deep-research) copied into the
  overlay's skills dir; builtin skills ship with the reasonix binary and need
  no copying.

bot/, MCP, speech, telemetry and unrelated secrets stay out. The overlay file
is 0600, lives inside the data dir, and never enters Git, logs, or reports.
Subprocesses get an environment WHITELIST (not a full copy of os.environ) and
a working directory: the project workspace (per run) or the restricted
overlay work dir as fallback.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path

OVERLAY_DIRNAME = "rx-overlay"

ENV_WHITELIST = ("PATH", "HOME", "REASONIX_HOME", "TERM", "LANG", "LC_ALL", "TZ")

_REQUIRED_PROVIDER_KEYS = {"name"}

# top-level config keys researchd actually needs (whitelist; everything else
# — bot, MCP, speech, telemetry, theme, desktop — is excluded)
TOP_LEVEL_KEYS = (
    "default_model",
    "planner_model",
    "subagent_model",
    "subagent_effort",
    "subagent_models",
    "max_subagent_depth",
    "max_subagent_concurrency",
    "max_parallel_writers",
)

# user skills that may be mounted into the overlay (must exist under
# ~/.reasonix/skills/<name>/SKILL.md); builtin skills (explore/research/
# review/security-review) ship with reasonix and need no copying
ALLOWED_SKILLS = ("reviewer", "deep-research")

SKILL_MANIFEST = "SKILL.md"


class OverlayError(RuntimeError):
    pass


def _minimal_config(global_config: Path) -> str:
    """Extract whitelisted top-level keys VERBATIM + [[providers]] blocks
    VERBATIM from the global config (text-level slicing, so nested TOML
    structures stay byte-identical). Everything else is excluded."""
    lines = global_config.read_text().splitlines()
    blocks: list[list[str]] = []
    current: list[str] | None = None
    top_level: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key in TOP_LEVEL_KEYS:
            top_level.append(line)
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
    for line in top_level:
        out.append(line)
    if top_level:
        out.append("")
    for block in blocks:
        out.extend(block)
        out.append("")
    return "\n".join(out)


def installed_skills(overlay: Path) -> list[str]:
    """Skill names actually mounted in the overlay (sorted, stable)."""
    skills_dir = overlay / "skills"
    if not skills_dir.is_dir():
        return []
    return sorted(p.name for p in skills_dir.iterdir() if (p / SKILL_MANIFEST).is_file())


# files allowed inside a mounted skill (everything else is dropped —
# skill folders may contain credentials, notes, or other unvetted files)
ALLOWED_SKILL_FILES = ("SKILL.md", "README.md")


def _install_skills(overlay: Path, global_skills: Path) -> list[str]:
    """Copy whitelisted user skills into the overlay — WHITELIST FILES ONLY
    (SKILL.md/README.md); any other file inside a skill folder is dropped so
    unvetted content (secrets, notes) never reaches the executor. Returns the
    mounted names. Missing skills are skipped."""
    skills_dir = overlay / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(skills_dir, 0o700)
    mounted: list[str] = []
    for name in ALLOWED_SKILLS:
        src = global_skills / name
        if not (src / SKILL_MANIFEST).is_file():
            continue
        dst = skills_dir / name
        if dst.exists():
            shutil.rmtree(dst)
        dst.mkdir()
        for fname in ALLOWED_SKILL_FILES:
            f = src / fname
            if f.is_file():
                shutil.copy2(f, dst / fname)
                os.chmod(dst / fname, 0o600)
        mounted.append(name)
    return mounted


def ensure_overlay(data_dir: str | Path, *, skills: bool = True) -> Path:
    """Create the whitelisted isolated overlay. Returns the overlay path."""
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
    if skills:
        _install_skills(overlay, Path.home() / ".reasonix" / "skills")
    return overlay


def overlay_env(overlay: Path) -> dict:
    """Restricted environment for a reasonix subprocess (whitelist only)."""
    env = {k: v for k, v in os.environ.items() if k in ENV_WHITELIST}
    env["REASONIX_HOME"] = str(overlay)
    env["HOME"] = str(overlay)  # keep the agent's home inside the restricted dir
    return env


def overlay_workdir(overlay: Path) -> Path:
    """Restricted working directory for executor subprocesses (fallback when
    no project workspace is configured)."""
    work = overlay / "work"
    work.mkdir(parents=True, exist_ok=True)
    return work
