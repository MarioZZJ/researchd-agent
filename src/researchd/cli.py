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


if __name__ == "__main__":
    main()


@main.command()
@click.option("--project-id", required=True, help="pilot project id (default: interdisciplinary-citation-pilot)")
@click.option("--question", default="", help="research question")
@click.option("--import-decision", default="", help="import a decision as <id>=<answer> (e.g. D-001=A)")
@click.option("--db", "db_path", default=None, help="database path (default: settings)")
@click.pass_context
def pilot(ctx: click.Context, project_id: str, question: str, import_decision: str, db_path: str | None) -> None:
    """Bootstrap the pilot project (idempotent). Creates the ACTIVE project
    and optionally imports a pre-decided decision (IMPLEMENTATION.md §24)."""
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
            ProjectRepo(uow.session).save(
                Project(project_id=project_id, name=project_id, description=question or "")
            )
            print(f"project {project_id} created")
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
        uow.commit()
    lock.release()
