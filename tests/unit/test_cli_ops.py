"""CLI + ops-test endpoint regression tests (IMPLEMENTATION.md §18, §24)."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from researchd.api.routes import ops_test
from researchd.config import Settings


class FakePort:
    def __init__(self):
        self.delivered = []
        self.updated = []

    async def deliver(self, *, idempotency_key, kind, payload, attachments=None, project_id=None):
        self.delivered.append((idempotency_key, kind, payload))
        return "om_test_1"

    async def update(self, platform_message_id, payload):
        self.updated.append((platform_message_id, payload))


def _app(delivery="fake") -> FastAPI:
    settings = Settings()
    settings.scheduler.delivery = delivery
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = None
    app.state.delivery_port = FakePort()
    app.include_router(ops_test.router)
    return app


def test_delivery_test_requires_token_and_delivers():
    app = _app()
    app.state.settings.api.token = "sekrit"
    client = TestClient(app)
    # token configured but not sent -> 401
    assert client.post("/v1/delivery/test", json={}).status_code == 401
    app = _app(delivery="cc_connect")
    app.state.settings.api.token = "sekrit"
    client = TestClient(app)
    port = app.state.delivery_port
    resp = client.post(
        "/v1/delivery/test",
        json={"chat_id": "oc_staging"},
        headers={"Authorization": "Bearer sekrit"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["platform_message_id"] == "om_test_1"
    assert body["updated"] is True
    assert len(port.delivered) == 1 and len(port.updated) == 1
    payload = port.delivered[0][2]
    assert "researchd delivery test" in payload["title"]
    assert payload["buttons"][0]["value"].startswith("/decision")


def test_delivery_test_fails_closed_without_cc_connect():
    app = _app(delivery="fake")
    app.state.settings.api.token = "t"
    client = TestClient(app)
    resp = client.post("/v1/delivery/test", json={}, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 503
    assert "cc_connect" in resp.json()["detail"]


def test_document_test_round_trip_requires_token():
    app = _app()
    app.state.settings.api.token = "t"
    client = TestClient(app)
    # no credentials configured on the service -> clear 502 with diagnostic
    resp = client.post("/v1/document/test", json={"document_id": "doc"}, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 502
    assert "credentials missing" in resp.json()["detail"]


def test_ctl_client_sends_bearer_token_on_uds(monkeypatch):
    """B-08: researchctl must send the bearer token on EVERY transport."""
    from researchd import ctl

    class FakeSettings:
        class Api:
            socket_path = "/tmp/fake.sock"
            token = "tok-123"
            tcp_host = "127.0.0.1"
            tcp_port = 8777

        api = Api()

    captured = {}

    class FakeHTTPTransport:
        def __init__(self, uds=None):
            captured["uds"] = uds

    class FakeClient:
        def __init__(self, transport=None, headers=None, base_url=None, timeout=None):
            captured["headers"] = headers
            captured["transport"] = transport

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, path, **kw):
            return type("R", (), {"status_code": 200, "json": lambda self: {}, "text": "", "headers": {"content-type": "application/json"}})()

    monkeypatch.setattr(ctl, "default_settings", lambda: FakeSettings())
    monkeypatch.setattr("researchd.ctl.httpx.HTTPTransport", FakeHTTPTransport)
    monkeypatch.setattr("researchd.ctl.httpx.Client", FakeClient)
    ctl._call("GET", "/healthz")
    # token attached even though the transport is a UDS socket
    assert captured["headers"] == {"Authorization": "Bearer tok-123"}
    assert captured["uds"] == "/tmp/fake.sock"


def test_pilot_create_cli_available():
    """researchd pilot create must be a registered command (was defined after
    main() invocation and silently missing)."""
    from click.testing import CliRunner

    from researchd.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["pilot", "--help"])
    assert result.exit_code == 0, result.output
    assert "create" in result.output
    result = runner.invoke(main, ["pilot", "create", "--help"])
    assert result.exit_code == 0
    assert "--project-id" in result.output
    assert "--import-open-decision" in result.output


def test_pilot_create_imports_open_decision_and_workspace(tmp_path):
    """pilot create must derive the service workspace root (A1) and import
    an OPEN decision D-002 with A/B options idempotently (A2)."""
    from pathlib import Path

    from click.testing import CliRunner
    from sqlalchemy import create_engine

    from researchd.cli import main
    from researchd.persistence.repositories import DecisionRepo, ProjectRepo
    from researchd.persistence.transaction import make_session_factory

    runner = CliRunner()
    db = str(tmp_path / "pilot.db")
    data_dir = str(tmp_path / "data")
    project_id = "interdisciplinary-citation-pilot"
    args = [
        "--data-dir", data_dir,
        "pilot", "create",
        "--project-id", project_id,
        "--owner-open-id", "ou_8c1a4e0a1e9bf230e2dd648b4a97259c",
        "--import-decision", "D-001=A",
        "--import-open-decision", "D-002",
        "--decision-question", "pilot 验证决策 D-002",
        "--decision-body", "验证用途：真实卡片点击闭环",
        "--db", db,
    ]
    result = runner.invoke(main, args)
    assert result.exit_code == 0, result.output
    factory = make_session_factory(create_engine(f"sqlite:///{db}"))
    with factory() as session:
        project = ProjectRepo(session).get_by_project_id(project_id)
        assert project is not None
        expected_root = str(tmp_path / "data" / "workspaces" / project_id)
        assert project.workspace_root == expected_root
        assert Path(expected_root).is_dir()
        d1 = DecisionRepo(session).get_by_decision_id("D-001")
        assert d1 is not None and d1.status.value == "APPLIED" and d1.answer == "A"
        d2 = DecisionRepo(session).get_by_decision_id("D-002")
        assert d2 is not None and d2.status.value == "OPEN"
        assert d2.project_id == project_id
        assert d2.question == "pilot 验证决策 D-002"
        assert d2.recommendation == "验证用途：真实卡片点击闭环"
        assert [o.option_id for o in d2.options] == ["A", "B"]
    # idempotent re-run: no duplicate project/decision/workspace side-effects
    result2 = runner.invoke(main, args)
    assert result2.exit_code == 0, result2.output
    with factory() as session:
        from sqlalchemy import func, select

        from researchd.persistence.models import DecisionRow, ProjectRow

        assert session.execute(select(func.count()).select_from(ProjectRow)).scalar() == 1
        assert session.execute(select(func.count()).select_from(DecisionRow)).scalar() == 2
        assert DecisionRepo(session).get_by_decision_id("D-002").status.value == "OPEN"
    # fail-closed: open-decision flags without the id
    result3 = runner.invoke(
        main,
        ["--data-dir", str(tmp_path / "data2"), "pilot", "create",
         "--project-id", "p-x", "--decision-question", "q", "--db", str(tmp_path / "x.db")],
    )
    assert result3.exit_code != 0
    assert "--import-open-decision" in result3.output
    # link a decision to evidence (idempotent; the linter validates existence
    # at report time, so the card cites real pilot evidence once it exists)
    result4 = runner.invoke(
        main,
        ["--data-dir", data_dir, "pilot", "create",
         "--project-id", project_id,
         "--link-decision-evidence", "D-002=E-TEST-EVIDENCE", "--db", db],
    )
    assert result4.exit_code == 0, result4.output
    with factory() as session:
        d2 = DecisionRepo(session).get_by_decision_id("D-002")
        assert d2.evidence_refs == ["E-TEST-EVIDENCE"]
    result5 = runner.invoke(
        main,
        ["--data-dir", data_dir, "pilot", "create",
         "--project-id", project_id,
         "--link-decision-evidence", "D-002=E-TEST-EVIDENCE", "--db", db],
    )
    assert result5.exit_code == 0, result5.output
    with factory() as session:
        d2 = DecisionRepo(session).get_by_decision_id("D-002")
        assert d2.evidence_refs == ["E-TEST-EVIDENCE"]  # no duplicate ref
    # fail-closed: link to a missing decision or empty evidence id
    result6 = runner.invoke(
        main,
        ["--data-dir", data_dir, "pilot", "create",
         "--project-id", project_id,
         "--link-decision-evidence", "D-NOPE=E-X", "--db", db],
    )
    assert result6.exit_code != 0
    assert "not found" in result6.output


def test_ctl_tcp_base_url_uses_configured_port(monkeypatch):
    """TCP fallback must use the configured tcp_host/tcp_port, never a bare
    http://localhost (which would silently hit :80)."""
    from researchd import ctl

    captured = {}

    class FakeSettings:
        class Api:
            socket_path = ""
            token = "tok"
            tcp_host = "127.0.0.1"
            tcp_port = 9999

        api = Api()

    class FakeClient:
        def __init__(self, transport=None, headers=None, base_url=None, timeout=None):
            captured["base_url"] = base_url

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, path, **kw):
            return type("R", (), {"status_code": 200, "json": lambda self: {}, "text": "", "headers": {"content-type": "application/json"}})()

    monkeypatch.setattr(ctl, "default_settings", lambda: FakeSettings())
    monkeypatch.setattr("researchd.ctl.httpx.Client", FakeClient)
    ctl._call("GET", "/healthz")
    assert captured["base_url"] == "http://127.0.0.1:9999"


def test_ctl_mutating_call_fails_closed_without_token(monkeypatch):
    """A state-changing researchctl call without a configured token must
    refuse locally (fail-closed), not send an unauthenticated request."""
    from researchd import ctl
    from researchd.config import Settings

    class FakeSettings:
        class Api:
            socket_path = "/tmp/x.sock"
            token = ""
            tcp_host = "127.0.0.1"
            tcp_port = 8777

        api = Api()

    monkeypatch.setattr(ctl, "default_settings", lambda: FakeSettings())
    with pytest.raises(Exception, match="RESEARCHD_API__TOKEN"):
        ctl._call("POST", "/v1/projects", json={"name": "x"})
