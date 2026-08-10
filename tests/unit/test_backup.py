"""Phase 9: online backup, restore dry-run/apply, project export."""

from __future__ import annotations

import json
import os
import sqlite3
import tarfile
from pathlib import Path

import pytest

from researchd.domain.project import Project
from researchd.ops.backup import backup, export_project, restore, validate_backup
from researchd.persistence.repositories import ProjectRepo
from researchd.persistence.transaction import UnitOfWork


@pytest.fixture
def db(factory):
    with UnitOfWork(factory) as uow:
        ProjectRepo(uow.session).save(Project(project_id="P-BK", name="backup-me", metadata={}))
        uow.commit()
    return factory


def _engine_path(factory):
    return factory.kw["bind"].url.database


def test_online_backup_while_writing(factory, db, tmp_path):
    """backup() must produce a consistent snapshot even while the service
    is mid-transaction (we write rows between connect and backup)."""
    import sqlite3

    from researchd.domain.project import Project
    from researchd.persistence.repositories import ProjectRepo

    db_path = _engine_path(factory)
    _ensure_alembic_version(db_path)
    # simulate an in-flight write on a separate session (uncommitted)
    live_session = factory()
    ProjectRepo(live_session).save(Project(project_id="P-INFLIGHT", name="inflight", metadata={}))
    live_session.flush()
    try:
        result = backup(
            data_dir=Path(db_path).parent,
            db_path=db_path,
            backup_dir=tmp_path / "backups",
        )
    finally:
        live_session.rollback()
        live_session.close()

    # the snapshot sees only committed rows: P-BK present, P-INFLIGHT absent
    info = validate_backup(result.db_backup)
    assert info["integrity"] == "ok"
    assert "projects" in info["tables"]
    conn = sqlite3.connect(result.db_backup)
    ids = {r[0] for r in conn.execute("SELECT project_id FROM projects")}
    conn.close()
    assert "P-BK" in ids
    assert "P-INFLIGHT" not in ids
    assert result.manifest["db_bytes"] > 0


def _ensure_alembic_version(db_path):
    """ORM test DBs have no alembic_version; the backup validator expects it."""
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
    conn.commit()
    conn.close()

def _core_tables(conn):
    for t in ("alembic_version", "projects", "tasks", "runs", "evidence", "events", "outbox"):
        conn.execute(f"CREATE TABLE {t} (id TEXT PRIMARY KEY)")

def test_restore_dry_run_and_apply(tmp_path):
    import sqlite3

    src = tmp_path / "src.db"
    conn = sqlite3.connect(src)
    _core_tables(conn)
    conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY, v INTEGER)")
    conn.execute("INSERT INTO t VALUES ('a', 1)")
    conn.commit()
    conn.close()

    # dry-run: no files created
    target = tmp_path / "restored"
    res = restore(db_backup=src, target_dir=target, dry_run=True)
    assert res["dry_run"] and not res["restored"]
    assert not target.exists()

    # apply into a fresh dir
    res = restore(db_backup=src, target_dir=target, dry_run=False)
    assert res["restored"] and (target / "researchd.db").exists()
    conn = sqlite3.connect(target / "researchd.db")
    assert conn.execute("SELECT v FROM t WHERE id='a'").fetchone()[0] == 1
    conn.close()

    # refusing to restore over an existing dir (race-free: it exists now)
    with pytest.raises(RuntimeError):
        restore(db_backup=src, target_dir=target, dry_run=False)


def test_restore_refuses_live_target(tmp_path):
    import sqlite3

    src = tmp_path / "src.db"
    conn = sqlite3.connect(src)
    _core_tables(conn)
    conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    live_db = tmp_path / "live" / "researchd.db"
    with pytest.raises(RuntimeError, match="live database"):
        restore(db_backup=src, target_dir=live_db, dry_run=True, live_db=live_db)
    with pytest.raises(RuntimeError, match="live data dir"):
        restore(db_backup=src, target_dir=tmp_path / "live", dry_run=True, live_data_dir=tmp_path / "live")


def test_backup_refuses_missing_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup(
            data_dir=tmp_path,
            db_path=tmp_path / "nope.db",
            backup_dir=tmp_path / "backups",
        )


def test_restore_rejects_escaping_tar(tmp_path):
    import sqlite3

    src = tmp_path / "src.db"
    conn = sqlite3.connect(src)
    _core_tables(conn)
    conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        payload = b"evil"
        info = tarfile.TarInfo("../escaped.txt")  # path escape
        info.size = len(payload)
        tar.addfile(info, __import__("io").BytesIO(payload))
    with pytest.raises(RuntimeError, match="escapes"):
        restore(db_backup=src, target_dir=tmp_path / "out", workspaces_archive=evil, dry_run=True)
    # symlink member is also refused
    evil2 = tmp_path / "evil2.tar.gz"
    with tarfile.open(evil2, "w:gz") as tar:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc"
        tar.addfile(info)
    with pytest.raises(RuntimeError, match="link"):
        restore(db_backup=src, target_dir=tmp_path / "out2", workspaces_archive=evil2, dry_run=True)


def test_backup_restore_round_trip_with_workspaces(factory, db, tmp_path):
    """backup() then dry-run (no writes) then apply() must round-trip:
    DB core tables + workspace files present, target published atomically."""
    import sqlite3

    from researchd.domain.project import Project
    from researchd.persistence.repositories import ProjectRepo

    # a project whose workspace_root is a real directory (outside data_dir)
    ws = tmp_path / "project-workspace"
    ws.mkdir()
    (ws / "notes.md").write_text("synthetic note")
    (ws / "data.json").write_text('{"k": 1}')
    os.symlink(ws / "data.json", ws / "link.json")  # backup must skip links
    db_path = _engine_path(factory)
    _ensure_alembic_version(db_path)
    with UnitOfWork(factory) as uow:
        ProjectRepo(uow.session).save(
            Project(project_id="P-WS", name="ws", metadata={}, workspace_root=str(ws))
        )
        uow.commit()

    result = backup(
        data_dir=Path(db_path).parent,
        db_path=db_path,
        backup_dir=tmp_path / "backups",
    )
    assert result.workspaces_archive is not None

    # dry-run: nothing written
    target = tmp_path / "restored"
    res = restore(
        db_backup=result.db_backup,
        target_dir=target,
        workspaces_archive=result.workspaces_archive,
        dry_run=True,
        live_db=db_path,
        live_data_dir=Path(db_path).parent,
    )
    assert res["dry_run"] and not target.exists()

    # apply: DB + workspace restored
    res = restore(
        db_backup=result.db_backup,
        target_dir=target,
        workspaces_archive=result.workspaces_archive,
        dry_run=False,
        live_db=db_path,
        live_data_dir=Path(db_path).parent,
    )
    assert res["restored"] and (target / "researchd.db").exists()
    conn = sqlite3.connect(target / "researchd.db")
    assert conn.execute("SELECT project_id FROM projects WHERE project_id='P-WS'").fetchone()
    conn.close()
    # workspace files restored (under workspaces/ws-00-project-workspace), no links
    ws_files = sorted(str(p.relative_to(target)) for p in target.rglob("*") if p.is_file())
    assert any(f.endswith("notes.md") for f in ws_files)
    assert not any(f.endswith("link.json") for f in ws_files)


def test_export_project_deterministic(db, tmp_path):
    from researchd.domain.task import Task, TaskContract

    from researchd.persistence.repositories import TaskRepo

    with UnitOfWork(db) as uow:
        TaskRepo(uow.session).save(
            Task(
                task_id="T-BK1",
                project_id="P-BK",
                contract=TaskContract(task_id="T-BK1", role="worker", objective="export me"),
            )
        )
        uow.commit()
    first = export_project(_engine_path(db), "P-BK")
    second = export_project(_engine_path(db), "P-BK")
    assert first == second  # deterministic
    assert {"tasks", "runs", "evidence", "decisions"} <= set(first)
    assert first["tasks"][0]["task_id"] == "T-BK1"
    with pytest.raises(KeyError):
        export_project(_engine_path(db), "P-NOPE")
    # JSON-serializable
    json.dumps(first, default=str)
