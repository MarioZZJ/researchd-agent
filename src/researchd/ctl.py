"""researchctl — ops, query, recovery, diagnostics CLI.

Talks to the running service over its UDS internal API (or TCP fallback).
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import click
import httpx

from .config import default_settings


def _base_url() -> str:
    settings = default_settings()
    if settings.api.socket_path:
        return f"http+unix://{settings.api.socket_path}"
    return f"http://{settings.api.tcp_host}:{settings.api.tcp_port}"


def _client() -> httpx.Client:
    settings = default_settings()
    transport = None
    if settings.api.socket_path:
        transport = httpx.HTTPTransport(
            uds=str(settings.api.socket_path),
        )
    # the bearer token is REQUIRED on every transport (server-side B-08
    # enforcement incl. UDS), so send it whenever it is configured
    headers = {}
    if settings.api.token:
        headers["Authorization"] = f"Bearer {settings.api.token}"
    # TCP base URL MUST honor the configured port (a bare http://localhost
    # would silently hit :80 and never reach the service)
    base = "http://localhost"
    if not settings.api.socket_path:
        base = f"http://{settings.api.tcp_host}:{settings.api.tcp_port}"
    return httpx.Client(transport=transport, headers=headers, base_url=base, timeout=10.0)


_MUTATING_METHODS = {"POST", "PATCH", "PUT", "DELETE"}


def _call(method: str, path: str, **kw) -> dict:
    settings = default_settings()
    if method.upper() in _MUTATING_METHODS and not settings.api.token:
        raise click.ClickException(
            "mutating call requires RESEARCHD_API__TOKEN (fail-closed: "
            "never send a state-changing request without the bearer token)"
        )
    with _client() as client:
        resp = client.request(method, path, **kw)
        if resp.status_code >= 400:
            detail = resp.json().get("detail", resp.text) if resp.headers.get("content-type", "").startswith("application/json") else resp.text
            raise click.ClickException(f"{method} {path}: HTTP {resp.status_code}: {detail}")
        return resp.json()


@click.group()
def main() -> None:
    """researchctl — researchd operations CLI."""


@main.command("project")
@click.argument("action", type=click.Choice(["list", "status", "create"]))
@click.argument("project_id", required=False)
@click.option("--name", default=None, help="name for create")
def project_cmd(action: str, project_id: str | None, name: str | None) -> None:
    """project list | status <id> | create --name X"""
    if action == "list":
        data = _call("GET", "/v1/projects")
        for p in data.get("projects", []):
            click.echo(f"{p['project_id']}  {p['status']}  {p['name']}")
    elif action == "status":
        if not project_id:
            raise click.ClickException("project status requires <project-id>")
        click.echo(json.dumps(_call("GET", f"/v1/projects/{project_id}/status"), ensure_ascii=False, indent=2))
    else:
        if not name:
            raise click.ClickException("create requires --name")
        click.echo(json.dumps(_call("POST", "/v1/projects", json={"project_id": project_id, "name": name}), ensure_ascii=False))


@main.command("task")
@click.argument("action", type=click.Choice(["list"]))
@click.argument("project_id")
def task_cmd(action: str, project_id: str) -> None:
    """task list <project-id>"""
    data = _call("GET", f"/v1/projects/{project_id}/tasks")
    for t in data.get("tasks", []):
        click.echo(f"{t['task_id']}  {t['status']}  {t['objective'][:80]}")


@main.command("decision")
@click.argument("action", type=click.Choice(["list"]))
@click.argument("project_id")
@click.option("--open", "only_open", is_flag=True, default=False)
def decision_cmd(action: str, project_id: str, only_open: bool) -> None:
    """decision list <project-id> [--open]"""
    data = _call("GET", f"/v1/projects/{project_id}/decisions")
    for d in data.get("decisions", []):
        if only_open and d["status"] not in ("OPEN", "ANSWERED"):
            continue
        click.echo(f"{d['decision_id']}  {d['status']}  v{d['version']}  {d['question'][:80]}")


@main.command()
@click.argument("project_id")
def pause(project_id: str) -> None:
    """pause <project-id>"""
    click.echo(json.dumps(_call("POST", f"/v1/projects/{project_id}/pause"), ensure_ascii=False))


@main.command()
@click.argument("project_id")
def resume(project_id: str) -> None:
    """resume <project-id>"""
    click.echo(json.dumps(_call("POST", f"/v1/projects/{project_id}/resume"), ensure_ascii=False))


@main.command()
def reconcile() -> None:
    """reconcile — trigger orphan reconciliation"""
    click.echo(json.dumps(_call("POST", "/v1/reconcile"), ensure_ascii=False))


@main.command("outbox")
@click.argument("action", type=click.Choice(["retry", "list"]))
@click.option("--limit", default=20)
def outbox_cmd(action: str, limit: int) -> None:
    """outbox retry|list"""
    # Phase 3 lands the full outbox admin; here we surface pending counts via reconcile
    if action == "list":
        click.echo(json.dumps(_call("POST", "/v1/reconcile"), ensure_ascii=False))
    else:
        click.echo(json.dumps(_call("POST", "/v1/reconcile"), ensure_ascii=False))


@main.command()
@click.argument("project_id")
def export(project_id: str) -> None:
    """export <project-id> — project state export (Phase 9 full impl)"""
    data = _call("GET", f"/v1/projects/{project_id}/status")
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


@main.command("delivery")
@click.argument("action", type=click.Choice(["test"]))
def delivery_cmd(action: str) -> None:
    """delivery test — send + in-place update a real cc-connect card.

    Sends an interactive card to the service's CONFIGURED staging target
    (delivery=cc_connect + token/project/session_key from the env file) and
    PATCH-updates it in place; fails loudly when cc-connect is not configured.
    """
    if action == "test":
        click.echo(json.dumps(_call("POST", "/v1/delivery/test", json={}), ensure_ascii=False, indent=2))


@main.command("document")
@click.argument("action", type=click.Choice(["create", "test"]))
@click.option("--project-id", default="", help="project id (test defaults to the project's own document)")
@click.option("--document-id", default="", help="explicit staging feishu docx document id (test only)")
@click.option("--title", default="", help="optional title override (create only)")
def document_cmd(action: str, project_id: str, document_id: str, title: str) -> None:
    """document create --project-id <id> — create the project's feishu docx
    once and persist the receipt (idempotent).

    document test [--project-id <id>] [--document-id <id>] — block-level
    docx round-trip on the project's own document by default (no external id
    required); requires RESEARCHD_LARK_APP_ID/SECRET on the service."""
    if action == "create":
        if not project_id:
            raise click.ClickException("document create requires --project-id")
        click.echo(json.dumps(
            _call("POST", "/v1/document/create", json={"project_id": project_id, "title": title}),
            ensure_ascii=False, indent=2,
        ))
    else:
        body = {"project_id": project_id, "document_id": document_id}
        click.echo(json.dumps(
            _call("POST", "/v1/document/test", json=body),
            ensure_ascii=False, indent=2,
        ))


@main.command()
def doctor() -> None:
    """doctor — environment + service diagnostics"""
    from .persistence.transaction import make_engine, verify_pragmas

    settings = default_settings()
    click.echo(f"data_dir : {settings.data_dir}")
    click.echo(f"db_path  : {settings.db_path}")
    click.echo(f"socket   : {settings.api.socket_path}")
    click.echo(f"tcp      : {settings.api.tcp_host}:{settings.api.tcp_port} (token={'set' if settings.api.token else 'none'})")
    socket_path = Path(settings.api.socket_path)
    if socket_path.exists():
        click.echo(f"service  : socket present ({socket_path})")
    else:
        click.echo("service  : socket NOT present (service not running?)")
    try:
        engine = make_engine(settings.db_path, read_only=True)
        pragmas = verify_pragmas(engine, read_only=True)
        click.echo(f"db       : {pragmas} (read-only check)")
        from sqlalchemy import inspect

        tables = set(inspect(engine).get_table_names())
        core = {"alembic_version", "projects", "tasks", "runs", "evidence", "events", "outbox"}
        missing = sorted(core - tables)
        click.echo(f"schema   : {len(tables)} tables" + (f", MISSING {missing}" if missing else ", core tables present"))
        engine.dispose()
    except Exception as exc:  # noqa: BLE001
        click.echo(f"db       : ERROR {exc}")
    # permissions: data root 0700, socket 0600, lock 0600, db 0600
    for label, path, want in (
        ("data_dir", settings.data_dir, 0o700),
        ("socket", settings.api.socket_path, 0o600),
        ("lock", str(Path(settings.data_dir) / "researchd.lock"), 0o600),
        ("db", settings.db_path, 0o600),
    ):
        p = Path(path)
        if p.exists():
            mode = p.stat().st_mode & 0o777
            flag = "ok" if mode == want else f"EXPECTED {oct(want)[2:]}"
            click.echo(f"perms    : {label} {oct(mode)[2:]} ({flag})")
    try:
        data = _call("GET", "/healthz")
        click.echo(f"healthz  : {data}")
    except Exception as exc:  # noqa: BLE001
        click.echo(f"healthz  : UNREACHABLE ({exc})")


if __name__ == "__main__":
    main()
