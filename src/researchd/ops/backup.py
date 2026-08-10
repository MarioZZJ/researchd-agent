"""Backup / restore / export for researchd (IMPLEMENTATION.md §27, Phase 9).

- backup: SQLite ONLINE backup via the sqlite3 Python API (WAL-safe, no
  service stop needed) + tarball of every project workspace root recorded in
  the database (projects.workspace_root) + a manifest.
- restore: restores a backup into a FRESH directory (never over the live
  copy); a DRY-RUN validates the archive first. Tar members are pre-checked
  for path escapes, and the restore happens in a staging dir that is only
  renamed into place after everything succeeded.
- export: deterministic JSON export of one project's state, read inside a
  single BEGIN transaction so the snapshot is consistent.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# core tables that must exist (and be non-empty structurally) in a valid backup
CORE_TABLES = (
    "alembic_version",
    "projects",
    "tasks",
    "runs",
    "evidence",
    "events",
    "outbox",
)


@dataclass
class BackupResult:
    backup_dir: Path
    db_backup: Path
    workspaces_archive: Path | None = None
    manifest: dict = field(default_factory=dict)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _check_db_source(db_path: Path) -> None:
    """Refuse to 'back up' a path that does not exist or is not a file —
    sqlite3.connect() would silently CREATE an empty database otherwise."""
    if not db_path.exists():
        raise FileNotFoundError(f"database {db_path} does not exist")
    if not db_path.is_file():
        raise ValueError(f"database path {db_path} is not a regular file")


def _project_workspace_roots(db_path: Path) -> list[Path]:
    """Workspace roots recorded in the DB (projects.workspace_root)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT workspace_root FROM projects "
            "WHERE workspace_root IS NOT NULL AND workspace_root <> ''"
        ).fetchall()
    finally:
        conn.close()
    roots = []
    for (root,) in rows:
        p = Path(root)
        if p.is_dir():
            roots.append(p)
    return roots


def backup(
    *,
    data_dir: str | Path,
    db_path: str | Path,
    backup_dir: str | Path,
    include_workspaces: bool = True,
) -> BackupResult:
    """Online SQLite backup + project workspace tarball + manifest."""
    data_dir = Path(data_dir)
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    _check_db_source(db_path)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now()

    db_backup = backup_dir / f"researchd-{stamp}.db"
    # online backup API: consistent snapshot even while the service writes
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(db_backup)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    # the snapshot must be structurally valid before we call it a backup
    info = validate_backup(db_backup)
    if info["integrity"] != "ok":
        raise RuntimeError("backup failed integrity check")

    manifest = {
        "created_at": stamp,
        "db": str(db_path),
        "db_backup": str(db_backup),
        "db_bytes": db_backup.stat().st_size,
        "workspaces": [],
    }

    workspaces_archive = None
    if include_workspaces:
        roots = _project_workspace_roots(db_path)
        if roots:
            workspaces_archive = backup_dir / f"workspaces-{stamp}.tar.gz"
            with tarfile.open(workspaces_archive, "w:gz") as tar:
                for root in roots:
                    tar.add(root, arcname=f"workspaces/{root.name}")
                    manifest["workspaces"].append(
                        {"name": root.name, "root": str(root)}
                    )
            manifest["workspaces_bytes"] = workspaces_archive.stat().st_size

    manifest_path = backup_dir / f"manifest-{stamp}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    manifest["manifest"] = str(manifest_path)
    return BackupResult(
        backup_dir=backup_dir,
        db_backup=db_backup,
        workspaces_archive=workspaces_archive,
        manifest=manifest,
    )


def validate_backup(db_backup: str | Path) -> dict:
    """Verify a backup archive is readable and structurally consistent."""
    db_backup = Path(db_backup)
    _check_db_source(db_backup)
    conn = sqlite3.connect(f"file:{db_backup}?mode=ro", uri=True)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    missing = [t for t in CORE_TABLES if t not in tables]
    return {"tables": sorted(tables), "integrity": integrity, "missing_core": missing}


def _safe_extract_members(tar: tarfile.TarFile, staging: Path) -> None:
    """Extract only safe members: no absolute paths, no '..', no links."""
    staging = staging.resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk():
            raise RuntimeError(f"tar member {member.name!r} is a link; refusing")
        target = (staging / member.name).resolve()
        if not str(target).startswith(str(staging) + os.sep):
            raise RuntimeError(f"tar member {member.name!r} escapes the staging dir")
    tar.extractall(staging)


def restore(
    *,
    db_backup: str | Path,
    target_dir: str | Path,
    workspaces_archive: str | Path | None = None,
    dry_run: bool = True,
    live_db: str | Path | None = None,
    live_data_dir: str | Path | None = None,
) -> dict:
    """Restore a backup into a FRESH target directory.

    - dry_run only validates (DB integrity + core tables + tar safety).
    - The target must not exist (race-free: we create it with O_EXCL).
    - live_db/live_data_dir (when given) are resolved and refused as targets,
      so a restored copy can never clobber the live database.
    """
    db_backup = Path(db_backup)
    target = Path(target_dir).resolve()
    if live_db is not None:
        live = Path(live_db).resolve()
        if target == live or target == live.parent:
            raise RuntimeError(f"refusing to restore onto the live database {live}")
    if live_data_dir is not None:
        live_dir = Path(live_data_dir).resolve()
        if target == live_dir or target == live_dir.parent:
            raise RuntimeError(f"refusing to restore onto the live data dir {live_dir}")

    info = validate_backup(db_backup)
    if info["integrity"] != "ok":
        raise RuntimeError("backup failed integrity check")
    if info["missing_core"]:
        raise RuntimeError(f"backup missing core tables: {info['missing_core']}")

    workspaces_ok = True
    if workspaces_archive:
        arc = Path(workspaces_archive)
        if not arc.exists():
            raise FileNotFoundError(f"workspaces archive {arc} does not exist")
        try:
            with tarfile.open(arc, "r:gz") as tar:
                _safe_extract_members(tar, target)  # validation pass (no writes)
        except tarfile.TarError as exc:
            raise RuntimeError(f"workspaces archive is corrupt: {exc}") from exc

    if dry_run:
        return {**info, "restored": False, "dry_run": True, "workspaces_ok": workspaces_ok}

    # race-free target creation: nobody can have created it between the
    # emptiness check and now
    try:
        target.mkdir(parents=False)
    except FileExistsError as exc:
        raise RuntimeError(f"target {target} already exists; refusing to restore over data") from exc

    staging = target.with_name(target.name + f".staging-{os.getpid()}")
    staging.mkdir(parents=True)
    try:
        shutil.copy2(db_backup, staging / "researchd.db")
        if workspaces_archive:
            with tarfile.open(workspaces_archive, "r:gz") as tar:
                _safe_extract_members(tar, staging)
        # atomic publish: staging -> target (target is an empty dir we own)
        target.rmdir()
        os.rename(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(target, ignore_errors=True)
        raise
    return {**info, "restored": True, "dry_run": False, "target": str(target)}


def export_project(db_path: str | Path, project_id: str) -> dict:
    """Deterministic JSON export of one project's persisted state.

    All reads happen inside one BEGIN transaction (single WAL snapshot), and
    every result is ordered by (created_at, id) so output is deterministic.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        project = conn.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise KeyError(f"project {project_id!r} not found")
        payload = {
            "project": dict(project),
            "tasks": [dict(r) for r in conn.execute(
                "SELECT task_id, status, contract_json, created_at FROM tasks "
                "WHERE project_id = ? ORDER BY created_at, task_id",
                (project_id,))],
            "runs": [dict(r) for r in conn.execute(
                "SELECT run_id, task_id, status, executor, outcome, error_message "
                "FROM runs WHERE project_id = ? ORDER BY created_at, run_id",
                (project_id,))],
            "evidence": [dict(r) for r in conn.execute(
                "SELECT evidence_id, type, status, statement FROM evidence "
                "WHERE project_id = ? ORDER BY created_at, evidence_id",
                (project_id,))],
            "claims": [dict(r) for r in conn.execute(
                "SELECT claim_id, text, evidence_state, review_level, use_state "
                "FROM claims WHERE project_id = ? ORDER BY created_at, claim_id",
                (project_id,))],
            "decisions": [dict(r) for r in conn.execute(
                "SELECT decision_id, status, question, answer, fingerprint "
                "FROM decisions WHERE project_id = ? ORDER BY created_at, decision_id",
                (project_id,))],
            "issues": [dict(r) for r in conn.execute(
                "SELECT issue_id, status, title, severity FROM issues "
                "WHERE project_id = ? ORDER BY created_at, issue_id",
                (project_id,))],
        }
        return payload
    finally:
        conn.close()


def export_project_file(db_path: str | Path, project_id: str, out: str | Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(export_project(db_path, project_id), indent=2, ensure_ascii=False, default=str)
    )
    return out
