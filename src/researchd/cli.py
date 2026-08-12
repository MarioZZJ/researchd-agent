"""researchd CLI: `researchd service`, `researchd acp`, and maintenance verbs."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import click

from .config import default_settings, load_settings_file


@click.group()
@click.option("--config", "config_path", type=click.Path(), default=None, help="settings file")
@click.option("--data-dir", type=click.Path(), default=None, help="override data directory")
@click.pass_context
def main(ctx: click.Context, config_path: str | None, data_dir: str | None) -> None:
    """researchd — durable research control system."""
    settings = load_settings_file(config_path)
    if data_dir:
        settings.data_dir = str(Path(data_dir).resolve())
    settings.resolve()
    ctx.ensure_object(dict)
    ctx.obj["settings"] = settings


@main.command()
@click.pass_context
def service(ctx: click.Context) -> None:
    """Run the long-lived service (the only database writer)."""
    from .service import run_service

    settings = ctx.obj["settings"]
    settings.ensure_dirs()
    asyncio.run(run_service(settings))


@main.command()
@click.pass_context
def acp(ctx: click.Context) -> None:
    """Serve the Agent Client Protocol over stdio (inbound shim for cc-connect)."""
    from .acp.agent import run_acp_stdio

    settings = ctx.obj["settings"]
    asyncio.run(run_acp_stdio(settings))


@main.command()
@click.option("--db", "db_path", default=None, help="database path (default: settings)")
@click.pass_context
def migrate(ctx: click.Context, db_path: str | None) -> None:
    """Run Alembic migrations to head (requires the service to be stopped)."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    from .persistence.locking import DataDirLock, DataDirLockedError

    settings = ctx.obj["settings"]
    target = db_path or settings.db_path
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    try:
        with DataDirLock(settings.data_dir):
            os.environ["RESEARCHD_DB"] = target
            cfg = AlembicConfig(str(Path(__file__).resolve().parent.parent.parent / "alembic.ini"))
            command.upgrade(cfg, "head")
    except DataDirLockedError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"migrated {target} to head")


@main.command()
@click.pass_context
def version(ctx: click.Context) -> None:
    """Print version."""
    from . import __version__

    click.echo(f"researchd {__version__}")


@main.group()
def pilot() -> None:
    """pilot — bootstrap pilot projects (idempotent)."""


def _derive_workspace_root(data_dir: str, project_id: str) -> str:
    """Service-derived workspace root (same derivation as the API route):
    <data_dir>/workspaces/<project_id>, created with a no-follow guard.

    The API route derives the root for projects it creates; `pilot create`
    runs offline (service stopped) and must derive it identically so the
    scheduler's fail-closed workspace checks (apply_result) can pass.
    """
    anchor = Path(data_dir) / "workspaces"
    anchor.mkdir(parents=True, exist_ok=True)
    candidate = anchor / project_id
    if candidate.is_symlink():
        raise click.ClickException(f"workspace root {candidate} is a symlink (refusing)")
    candidate.mkdir(exist_ok=True)
    return str(candidate.resolve())


@pilot.command("create")
@click.option("--project-id", required=True, help="pilot project id")
@click.option("--question", default="", help="research question")
@click.option("--owner-open-id", default="", help="REAL PI platform open_id (owner member; REQUIRED — the synthetic 'pi' owner is never auto-created)")
@click.option("--import-decision", default="", help="import a decision as <id>=<answer> (e.g. D-001=A)")
@click.option("--import-open-decision", default="", help="import an OPEN decision <id> (its decision card will be sent to the group)")
@click.option("--decision-question", default="", help="question/title for --import-open-decision (required with it)")
@click.option("--decision-body", default="", help="bottom-line for --import-open-decision (optional; shown on the card)")
@click.option("--link-decision-evidence", default="", help="link a decision to existing evidence as <id>=<evidence_id> (idempotent)")
@click.option("--db", "db_path", default=None, help="database path (default: settings)")
@click.pass_context
def pilot_create(ctx: click.Context, project_id: str, question: str, owner_open_id: str, import_decision: str, import_open_decision: str, decision_question: str, decision_body: str, link_decision_evidence: str, db_path: str | None) -> None:
    """Bootstrap the pilot project (idempotent). Creates the ACTIVE project
    with a service-derived workspace root and optionally imports decisions
    (APPLIED via --import-decision <id>=<answer>, or OPEN via
    --import-open-decision <id>, IMPLEMENTATION.md §24).

    The owner member MUST be a real PI open_id: no synthetic 'pi' owner is
    auto-created (fail-closed membership)."""
    from .domain.decision import Decision, DecisionOption
    from .domain.enums import DecisionStatus
    from .domain.project import Project
    from .persistence.repositories import DecisionRepo, ProjectRepo
    from .persistence.transaction import UnitOfWork, make_session_factory

    settings = ctx.obj["settings"]
    if db_path:
        settings.db_path = str(Path(db_path).resolve())
    settings.ensure_dirs()
    # bootstrap requires the exclusive writer lock: the service must NOT be
    # running (researchd service is the only writer at runtime)
    from .persistence.locking import DataDirLock, DataDirLockedError

    try:
        lock = DataDirLock(settings.data_dir)
        lock.acquire()
    except DataDirLockedError as exc:
        raise click.ClickException(
            f"{exc} — stop the service first (systemctl --user stop researchd)"
        ) from exc
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{settings.db_path}", connect_args={"check_same_thread": False})
    # bootstrap: ensure schema exists (production runs `researchd migrate`)
    from .persistence.models import Base

    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with UnitOfWork(factory) as uow:
        existing = ProjectRepo(uow.session).get_by_project_id(project_id)
        if existing is None:
            workspace_root = _derive_workspace_root(settings.data_dir, project_id)
            ProjectRepo(uow.session).save(
                Project(
                    project_id=project_id,
                    name=project_id,
                    description=question or "",
                    workspace_root=workspace_root,
                )
            )
            print(f"project {project_id} created (workspace_root={workspace_root})")
            # provision the owner member (fail-closed membership gate §22)
            # with the REAL PI open_id ONLY — the synthetic 'pi' owner is
            # never auto-created; without --owner-open-id the project is
            # created member-less and the PI must be added explicitly
            from .persistence.models import ProjectMemberRow

            if owner_open_id:
                uow.session.add(
                    ProjectMemberRow(
                        id=f"PM-{project_id}-{owner_open_id[:24]}",
                        member_id=f"PM-{project_id}-{owner_open_id[:24]}",
                        project_id=project_id,
                        platform_user_id=owner_open_id, role="owner",
                        can_approve_decisions=True,
                    )
                )
                print(f"project {project_id} created (owner member {owner_open_id!r} provisioned)")
            else:
                print(
                    f"project {project_id} created WITHOUT owner member — pass "
                    "--owner-open-id <real PI open_id> to provision one"
                )
        else:
            # idempotently provision the owner member on existing projects
            from .persistence.models import ProjectMemberRow

            # backfill the service-derived workspace root on pre-existing
            # pilot projects (created before the API derivation existed)
            if not existing.workspace_root:
                existing.workspace_root = _derive_workspace_root(settings.data_dir, project_id)
                ProjectRepo(uow.session).save(existing)
            if owner_open_id:
                members = uow.session.execute(
                    ProjectMemberRow.__table__.select().where(
                        ProjectMemberRow.project_id == project_id,
                        ProjectMemberRow.platform_user_id == owner_open_id,
                    )
                ).first()
                if members is None:
                    uow.session.add(
                        ProjectMemberRow(
                            id=f"PM-{project_id}-{owner_open_id[:24]}",
                            member_id=f"PM-{project_id}-{owner_open_id[:24]}",
                            project_id=project_id,
                            platform_user_id=owner_open_id, role="owner",
                            can_approve_decisions=True,
                        )
                    )
                    print(f"project {project_id} exists (owner member {owner_open_id!r} provisioned)")
                else:
                    print(f"project {project_id} already exists (owner member present)")
            else:
                print(f"project {project_id} already exists (no-op)")
        if import_decision:
            decision_id, _, answer = import_decision.partition("=")
            if DecisionRepo(uow.session).get_by_decision_id(decision_id) is None:
                DecisionRepo(uow.session).save(
                    Decision(
                        decision_id=decision_id,
                        project_id=project_id,
                        category="other",
                        question="pilot 定位",
                        options=[
                            DecisionOption(option_id="A", label="描述性定位"),
                            DecisionOption(option_id="B", label="因果解释"),
                        ],
                        status=DecisionStatus.APPLIED,
                        decision_version=1,
                        answer=answer,
                    )
                )
                print(f"decision {decision_id} imported (answer={answer})")
            else:
                print(f"decision {decision_id} already exists (no-op)")
        if decision_question and not import_open_decision:
            raise click.ClickException("--decision-question/--decision-body require --import-open-decision")
        if import_open_decision:
            if not decision_question:
                raise click.ClickException("--import-open-decision requires --decision-question")
            if DecisionRepo(uow.session).get_by_decision_id(import_open_decision) is None:
                DecisionRepo(uow.session).save(
                    Decision(
                        decision_id=import_open_decision,
                        project_id=project_id,
                        category="other",
                        question=decision_question,
                        trigger="pilot bootstrap",
                        why_material=decision_body or "",
                        recommendation=decision_body or "验证决策（等待 PI 选择）",
                        options=[
                            DecisionOption(
                                option_id="A", label="批准（approve）",
                                description="继续验证闭环", scientific_consequence="无（验证用途）",
                            ),
                            DecisionOption(
                                option_id="B", label="拒绝（reject）",
                                description="中止验证闭环", scientific_consequence="无（验证用途）",
                            ),
                        ],
                        status=DecisionStatus.OPEN,
                        decision_version=1,
                    )
                )
                print(f"decision {import_open_decision} imported (OPEN)")
            else:
                print(f"decision {import_open_decision} already exists (no-op)")
        if link_decision_evidence:
            decision_id, _, evidence_id = link_decision_evidence.partition("=")
            if not evidence_id:
                raise click.ClickException("--link-decision-evidence must be <decision_id>=<evidence_id>")
            decision = DecisionRepo(uow.session).get_by_decision_id(decision_id)
            if decision is None:
                raise click.ClickException(f"decision {decision_id!r} not found")
            refs = list(decision.evidence_refs or [])
            if evidence_id not in refs:
                refs.append(evidence_id)
                decision.evidence_refs = refs
                DecisionRepo(uow.session).save(decision)
                print(f"decision {decision_id} evidence linked (+{evidence_id})")
            else:
                print(f"decision {decision_id} evidence already linked (no-op)")
        uow.commit()
    lock.release()


@main.command("backup")
@click.option("--backup-dir", default=None, help="backup destination (default: <data_dir>/backups)")
@click.option("--no-workspaces", is_flag=True, help="skip the workspaces tarball")
@click.pass_context
def backup_cmd(ctx: click.Context, backup_dir: str | None, no_workspaces: bool) -> None:
    """Online SQLite backup + workspaces tarball (safe while the service runs)."""
    settings = ctx.obj["settings"]
    settings.ensure_dirs()
    from .ops.backup import backup

    dest = backup_dir or str(Path(settings.data_dir) / "backups")
    result = backup(
        data_dir=settings.data_dir,
        db_path=settings.db_path,
        backup_dir=dest,
        include_workspaces=not no_workspaces,
    )
    print(f"backup written: {result.db_backup}")
    if result.workspaces_archive:
        print(f"workspaces:     {result.workspaces_archive}")
    print(f"manifest:       {result.manifest.get('manifest')}")


@main.command("restore")
@click.option("--db-backup", required=True, help="path to the .db backup file")
@click.option("--workspaces", default=None, help="optional workspaces tarball")
@click.option("--target-dir", required=True, help="FRESH target directory (never the live dir)")
@click.option("--apply", is_flag=True, help="actually restore (default: validate only)")
@click.pass_context
def restore_cmd(ctx: click.Context, db_backup: str, workspaces: str | None, target_dir: str, apply: bool) -> None:
    """Validate (or apply) a backup into a fresh directory."""
    settings = ctx.obj["settings"]
    from .ops.backup import restore

    result = restore(
        db_backup=db_backup,
        target_dir=target_dir,
        workspaces_archive=workspaces,
        dry_run=not apply,
        live_db=settings.db_path,
        live_data_dir=settings.data_dir,
    )
    print(f"integrity: {result['integrity']}")
    missing = result.get("missing_core") or []
    print(f"tables ({len(result['tables'])}): {', '.join(result['tables'][:6])}{'…' if len(result['tables']) > 6 else ''}")
    if missing:
        print(f"missing core tables: {missing}")
    if result.get("restored"):
        print(f"restored to {result['target']}")


@main.command("export")
@click.option("--project-id", required=True, help="project to export")
@click.option("--out", default=None, help="output file (default: <project_id>.export.json)")
@click.pass_context
def export_cmd(ctx: click.Context, project_id: str, out: str | None) -> None:
    """Deterministic JSON export of one project's persisted state."""
    settings = ctx.obj["settings"]
    from .ops.backup import export_project_file

    target = out or f"{project_id}.export.json"
    path = export_project_file(settings.db_path, project_id, target)
    print(f"exported {project_id} -> {path}")


if __name__ == "__main__":
    main()
