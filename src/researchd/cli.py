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
