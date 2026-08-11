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
