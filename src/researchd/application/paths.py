"""Path safety: artifact paths must resolve inside the project workspace root
(IMPLEMENTATION.md §22, §25.9). Rejects '..' escapes and symlinks pointing
outside the root.
"""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path


class PathEscapeError(ValueError):
    pass


def normalize_artifact_path(workspace_root: str | Path, path: str) -> str:
    """Model outputs are free-form, so an artifact path may arrive relative
    to the cwd's basename ('ws/out/result.json' from cwd '.../ws') or as an
    absolute path inside the root. Rewrite it to root-relative ONLY when the
    target file actually exists; otherwise return it untouched so the
    registration gate accepts or rejects it (no boundary is ever widened)."""
    root = Path(workspace_root).resolve()
    p = str(path).replace("\\", "/")
    if p.startswith("/"):
        try:
            rel = Path(p).resolve().relative_to(root)
        except ValueError:
            return path  # outside the root: let the gate reject it
        if (root / rel).is_file():
            return str(rel)
        return path
    parts = p.split("/")
    if len(parts) >= 2 and parts[0] == root.name:
        stripped = "/".join(parts[1:])
        if stripped and (root / stripped).is_file():
            return stripped
    return path


def safe_resolve(workspace_root: str | Path, rel_path: str) -> Path:
    """Resolve rel_path inside workspace_root with escape/symlink protection."""
    root = Path(workspace_root).resolve()
    candidate = (root / rel_path).resolve(strict=False)
    if candidate != root and root not in candidate.parents:
        raise PathEscapeError(f"path {rel_path!r} escapes workspace root {root}")
    return candidate


def check_artifact_file(workspace_root: str | Path, rel_path: str, *, max_bytes: int = 2 * 1024**3) -> dict:
    """Validate a real artifact file: existence, size, MIME, SHA-256. Returns provenance dict."""
    path = safe_resolve(workspace_root, rel_path)
    if not path.is_file():
        raise FileNotFoundError(f"artifact file does not exist: {rel_path}")
    stat = path.stat()
    if stat.st_size > max_bytes:
        raise ValueError(f"artifact too large: {stat.st_size} bytes (max {max_bytes})")
    mime, _ = mimetypes.guess_type(str(path))
    sha = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
    return {
        "path": rel_path,
        "abs_path": str(path),
        "sha256": sha.hexdigest(),
        "size_bytes": stat.st_size,
        "mime_type": mime or "application/octet-stream",
    }


def check_symlink_escape(workspace_root: str | Path, rel_path: str) -> None:
    """Walk each component; any symlink whose target leaves the root is rejected."""
    root = Path(workspace_root).resolve()
    current = root
    for part in Path(rel_path).parts:
        current = current / part
        if current.is_symlink():
            target = current.resolve()
            if target != root and root not in target.parents:
                raise PathEscapeError(f"symlink {current} escapes workspace root")
