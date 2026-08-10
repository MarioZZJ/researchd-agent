"""Phase 2 integration tests: internal API over UDS, inbound idempotency,
deterministic commands, interaction vs policy isolation."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from researchd.application.commands import UnknownCommand, parse_command
from researchd.config import ApiConfig, InteractionConfig, Settings
from researchd.persistence.transaction import init_db, make_engine, make_session_factory

from researchd.api.app import create_app

import uvicorn


@pytest.fixture()
def api_env(tmp_path):
    """Start the FastAPI app on a UDS inside tmp_path, return client helpers."""
    settings = Settings(
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "test.db"),
        api=ApiConfig(socket_path=str(tmp_path / "researchd.sock")),
        interaction=InteractionConfig(),
    ).resolve()
    settings.ensure_dirs()
    engine = make_engine(settings.db_path)
    init_db(engine)
    app = create_app(settings)

    server = uvicorn.Server(uvicorn.Config(app, uds=settings.api.socket_path, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if Path(settings.api.socket_path).exists():
            break
        time.sleep(0.05)
    transport = httpx.HTTPTransport(uds=settings.api.socket_path)
    client = httpx.Client(transport=transport, base_url="http://localhost", timeout=5.0)
    yield {"client": client, "settings": settings, "server": server, "factory": make_session_factory(engine)}
    client.close()
    server.should_exit = True
    thread.join(timeout=5)


def test_healthz_and_readyz(api_env):
    r = api_env["client"].get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    r = api_env["client"].get("/readyz")
    assert r.status_code == 200 and r.json()["status"] == "ready"


def test_create_and_list_project(api_env):
    c = api_env["client"]
    r = c.post("/v1/projects", json={"project_id": "pilot-1", "name": "pilot"})
    assert r.status_code == 200
    pid = r.json()["project_id"]
    assert pid == "pilot-1"
    r = c.get("/v1/projects")
    assert any(p["project_id"] == "pilot-1" for p in r.json()["projects"])
    # duplicate create -> 409
    assert c.post("/v1/projects", json={"project_id": "pilot-1", "name": "x"}).status_code == 409


def test_project_status_pause_resume(api_env):
    c = api_env["client"]
    c.post("/v1/projects", json={"project_id": "p1", "name": "one"})
    r = c.get("/v1/projects/p1/status")
    assert r.json()["status"] == "ACTIVE"
    assert c.post("/v1/projects/p1/pause").json()["status"] == "PAUSED"
    assert c.post("/v1/projects/p1/resume").json()["status"] == "ACTIVE"


def test_inbound_decision_flow_and_idempotency(api_env):
    c = api_env["client"]
    c.post("/v1/projects", json={"project_id": "p2", "name": "two"})
    # create an OPEN decision directly in the DB (decision gate lands Phase 6)
    from researchd.domain.decision import Decision, DecisionOption
    from researchd.persistence.repositories import DecisionRepo
    from researchd.persistence.transaction import UnitOfWork

    with UnitOfWork(api_env["factory"]) as uow:
        DecisionRepo(uow.session).save(
            Decision(
                decision_id="D-001", project_id="p2", status="OPEN", question="choose",
                options=[DecisionOption(option_id="A", label="A"), DecisionOption(option_id="B", label="B")],
                decision_version=3,
            )
        )
        uow.commit()

    msg = {
        "message_id": "feishu-msg-1",
        "platform": "feishu",
        "cc_project": "p2",
        "text": "/decision D-001 A --version 3",
        "actor": {"type": "human", "platform_user_id": "ou_1"},
    }
    r = c.post("/v1/inbound/messages", json=msg)
    assert r.status_code == 200, r.text
    assert r.json()["reply"].startswith("decision D-001 answered")
    assert r.json()["duplicate"] is False

    # same message again -> idempotent no-op
    r2 = c.post("/v1/inbound/messages", json=msg)
    assert r2.status_code == 200
    assert r2.json()["duplicate"] is True

    # button click on an already-answered decision -> no-op, current state returned
    r3 = c.post("/v1/decisions/D-001/answer", json={"option_id": "A", "version": 3})
    assert r3.status_code == 200
    assert r3.json()["applied"] is False


def test_commands_route(api_env):
    c = api_env["client"]
    c.post("/v1/projects", json={"project_id": "p3", "name": "three"})
    r = c.post("/v1/projects/p3/commands", json={"text": "/research status"})
    assert r.status_code == 200
    assert r.json()["command"] == "status"

    r = c.post("/v1/projects/p3/commands", json={"text": "/research config set role.analysis_worker reasonix_worker"})
    assert r.status_code == 200
    assert "affects future runs only" in r.json()["reply"]

    r = c.post("/v1/projects/p3/commands", json={"text": "garbage text"})
    assert r.status_code == 400


def test_interaction_profile_does_not_change_policy(api_env):
    """Session-level interaction profile must never persist into project policy."""
    c = api_env["client"]
    c.post("/v1/projects", json={"project_id": "p4", "name": "four"})
    # the service handler only acknowledges; the shim applies it to the session
    r = c.post("/v1/projects/p4/commands", json={"text": "/research model interaction deep"})
    assert r.status_code == 200
    assert "session" in r.json()["reply"].lower()
    # role policy untouched
    r = c.post("/v1/projects/p4/commands", json={"text": "/research config show"})
    assert "role_overrides={}" in r.json()["reply"]


# ---------------------------------------------------------------- parser unit-ish
def test_parse_command_variants():
    assert parse_command("/research status").name == "status"
    cmd = parse_command("/decision D-002 B --version 3")
    assert cmd.name == "decision" and cmd.args == ["D-002", "B"] and cmd.flags == {"--version": "3"}
    assert parse_command("/research model interaction fast").args == ["interaction", "fast"]
    with pytest.raises(UnknownCommand):
        parse_command("hello there")
    with pytest.raises(UnknownCommand):
        parse_command("/research nope")


# ---------------------------------------------------------------- ACP shim
def test_acp_shim_handshake_and_prompt(api_env):
    """Drive the ACP stdio server against a running service."""
    from researchd.acp.agent import AcpServer

    settings = api_env["settings"]
    server = AcpServer(settings)
    init = __import__("asyncio").run(server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))
    assert init["result"]["agentCapabilities"]["sessionCapabilities"]["new"] == {}
    assert "interaction_profile" in init["result"]["configOptions"]

    new = __import__("asyncio").run(
        server.handle({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"sessionConfig": {"interaction_profile": "fast"}}})
    )
    sid = new["result"]["sessionId"]
    session = server.sessions[sid]

    c = api_env["client"]
    c.post("/v1/projects", json={"project_id": "p5", "name": "five"})

    # bind is a SESSION-level command handled by the shim (verified via service)
    bind = __import__("asyncio").run(
        server.handle({"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {"sessionId": sid, "prompt": "/research bind project p5"}})
    )
    assert bind.get("error") is None, bind
    assert session.cc_project == "p5"
    assert "bound" in bind["result"]["message"]["content"][0]["text"]

    # binding an unknown project fails and leaves the session unbound
    bad = __import__("asyncio").run(
        server.handle({"jsonrpc": "2.0", "id": 4, "method": "session/prompt", "params": {"sessionId": sid, "prompt": "/research bind project nope"}})
    )
    assert "not found" in bad["result"]["message"]["content"][0]["text"]

    # status works through the bound session
    status = __import__("asyncio").run(
        server.handle({"jsonrpc": "2.0", "id": 5, "method": "session/prompt", "params": {"sessionId": sid, "prompt": "/research status"}})
    )
    assert status.get("error") is None, status
    text = status["result"]["message"]["content"][0]["text"]
    assert "p5" in text

    # model interaction updates the SESSION (never the project policy)
    mi = __import__("asyncio").run(
        server.handle({"jsonrpc": "2.0", "id": 6, "method": "session/prompt", "params": {"sessionId": sid, "prompt": "/research model interaction deep"}})
    )
    assert session.interaction_profile == "deep"
    st = c.get("/v1/projects/p5/status").json()
    assert st["status"] == "ACTIVE"  # policy untouched

    close = __import__("asyncio").run(
        server.handle({"jsonrpc": "2.0", "id": 7, "method": "session/close", "params": {"sessionId": sid}})
    )
    assert close["result"]["sessionId"] == sid
    assert sid not in server.sessions


def test_acp_shim_unknown_method(api_env):
    from researchd.acp.agent import AcpServer

    server = AcpServer(api_env["settings"])
    resp = __import__("asyncio").run(server.handle({"jsonrpc": "2.0", "id": 9, "method": "nope", "params": {}}))
    assert resp["error"]["code"] == -32601


# ---------------------------------------------------------------- TCP + auth
def test_tcp_transport_requires_token(tmp_path):
    """TCP fallback: 401 without a bearer token, 200 with it."""
    from researchd.config import ApiConfig, InteractionConfig, Settings

    settings = Settings(
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "test.db"),
        api=ApiConfig(transport="tcp", token="s3cret", tcp_port=18777),
        interaction=InteractionConfig(),
    ).resolve()
    settings.ensure_dirs()
    engine = make_engine(settings.db_path)
    init_db(engine)
    app = create_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=settings.api.tcp_port, log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(1.0)
    try:
        base = f"http://127.0.0.1:{settings.api.tcp_port}"
        r = httpx.get(f"{base}/healthz", timeout=5)
        assert r.status_code == 200  # health is public
        r = httpx.get(f"{base}/v1/projects", timeout=5)
        assert r.status_code == 401  # no token
        r = httpx.get(f"{base}/v1/projects", headers={"Authorization": "Bearer wrong"}, timeout=5)
        assert r.status_code == 401
        r = httpx.get(f"{base}/v1/projects", headers={"Authorization": f"Bearer {settings.api.token}"}, timeout=5)
        assert r.status_code == 200
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_membership_gate_and_approval(api_env):
    """Once members exist, non-members are rejected; approval required for decisions."""
    from researchd.persistence.models import ProjectMemberRow
    from researchd.persistence.transaction import UnitOfWork as UoW2

    c = api_env["client"]
    c.post("/v1/projects", json={"project_id": "p9", "name": "nine"})
    from researchd.domain.decision import Decision, DecisionOption
    from researchd.persistence.repositories import DecisionRepo
    from researchd.persistence.transaction import UnitOfWork

    with UnitOfWork(api_env["factory"]) as uow:
        DecisionRepo(uow.session).save(
            Decision(
                decision_id="D-009", project_id="p9", status="OPEN", question="q",
                options=[DecisionOption(option_id="A", label="A")],
            )
        )
        uow.commit()
    # member exists: pi can approve; viewer cannot
    with UnitOfWork(api_env["factory"]) as uow:
        uow.session.add(
            ProjectMemberRow(
                id="M-1", member_id="M-1", project_id="p9", platform_user_id="ou_pi",
                role="pi", can_approve_decisions=True,
            )
        )
        uow.session.add(
            ProjectMemberRow(
                id="M-2", member_id="M-2", project_id="p9", platform_user_id="ou_viewer",
                role="member", can_approve_decisions=False,
            )
        )
        uow.commit()
    # stranger -> 403
    r = c.post("/v1/inbound/messages", json={
        "message_id": "m-stranger", "cc_project": "p9",
        "text": "/decision D-009 A", "actor": {"type": "human", "platform_user_id": "ou_stranger"},
    })
    assert r.status_code == 403
    # member without approval -> 403
    r = c.post("/v1/inbound/messages", json={
        "message_id": "m-viewer", "cc_project": "p9",
        "text": "/decision D-009 A", "actor": {"type": "human", "platform_user_id": "ou_viewer"},
    })
    assert r.status_code == 403
    # pi -> 200
    r = c.post("/v1/inbound/messages", json={
        "message_id": "m-pi", "cc_project": "p9",
        "text": "/decision D-009 A", "actor": {"type": "human", "platform_user_id": "ou_pi"},
    })
    assert r.status_code == 200
    assert "answered" in r.json()["reply"]
    # non-member pause -> 403
    r = c.post("/v1/inbound/messages", json={
        "message_id": "m-stranger2", "cc_project": "p9",
        "text": "/research pause", "actor": {"type": "human", "platform_user_id": "ou_stranger"},
    })
    assert r.status_code == 403


def test_decision_version_conflicts(api_env):
    """Bad --version forms: bare flag 400, mismatch 409, unknown option 400."""
    from researchd.domain.decision import Decision, DecisionOption
    from researchd.persistence.repositories import DecisionRepo
    from researchd.persistence.transaction import UnitOfWork

    c = api_env["client"]
    c.post("/v1/projects", json={"project_id": "p10", "name": "ten"})
    with UnitOfWork(api_env["factory"]) as uow:
        DecisionRepo(uow.session).save(
            Decision(
                decision_id="D-010", project_id="p10", status="OPEN", question="q",
                options=[DecisionOption(option_id="A", label="A")], decision_version=2,
            )
        )
        uow.commit()
    r = c.post("/v1/projects/p10/commands", json={"text": "/decision D-010 A --version"})
    assert r.status_code == 400
    r = c.post("/v1/projects/p10/commands", json={"text": "/decision D-010 A --version 99"})
    assert r.status_code == 409
    r = c.post("/v1/projects/p10/commands", json={"text": "/decision D-010 Z --version 2"})
    assert r.status_code == 400
    r = c.post("/v1/projects/p10/commands", json={"text": "/decision D-010 A --version 2"})
    assert r.status_code == 200


def test_intent_negation_scope():
    from researchd.acp.intents import classify_intent

    assert classify_intent("暂停项目", profile="fast") is not None
    assert classify_intent("不要暂停项目", profile="fast") is None
    assert classify_intent("请不要暂停项目", profile="fast") is None
    assert classify_intent("please don't pause", profile="fast") is None
    assert classify_intent("状态如何", profile="fast") is not None
    assert classify_intent("不暂停", profile="fast") is None
    assert classify_intent("给我看看状态", profile="deep") is not None


def test_acp_invalid_request_handling(api_env):
    from researchd.acp.agent import AcpServer

    server = AcpServer(api_env["settings"])
    r = __import__("asyncio").run(server.handle({"jsonrpc": "1.0", "method": "initialize", "id": 1}))
    assert r["error"]["code"] == -32600
    r = __import__("asyncio").run(server.handle({"jsonrpc": "2.0", "id": 1}))
    assert r["error"]["code"] == -32600
    # notification (no id) -> no response
    r = __import__("asyncio").run(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    assert r is None
