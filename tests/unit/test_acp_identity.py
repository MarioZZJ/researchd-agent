"""ACP identity tests (IMPLEMENTATION.md §3.2, §15.3): fail-closed identity,
env injection precedence, decision answers keep platform user/version."""

from __future__ import annotations

import asyncio

from researchd.acp.agent import AcpServer
from researchd.config import Settings


def _server(monkeypatch=None):
    s = Settings()
    if monkeypatch:
        monkeypatch.delenv("CC_PROJECT", raising=False)
        monkeypatch.delenv("CC_SESSION_KEY", raising=False)
        monkeypatch.delenv("CC_USER_ID", raising=False)
    return AcpServer(s)


def _clean_env(monkeypatch):
    monkeypatch.delenv("CC_PROJECT", raising=False)
    monkeypatch.delenv("CC_SESSION_KEY", raising=False)
    monkeypatch.delenv("CC_USER_ID", raising=False)


def _call(server, method, params=None):
    return asyncio.run(
        server.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    )


def test_session_new_fails_closed_without_identity(monkeypatch):
    _clean_env(monkeypatch)
    server = _server()
    resp = _call(server, "session/new", {"sessionConfig": {"interaction_profile": "fast"}})
    assert "error" in resp
    assert resp["error"]["code"] == -32602
    assert "cc-connect identity missing" in resp["error"]["message"]
    assert "never auto-mapping unknown users to PI" in resp["error"]["message"]
    assert server.sessions == {}


def test_session_new_accepts_explicit_session_config(monkeypatch):
    _clean_env(monkeypatch)
    server = _server()
    resp = _call(
        server,
        "session/new",
        {"sessionConfig": {"cc_project": "p", "cc_session_key": "k", "cc_user_id": "ou_1"}},
    )
    assert "result" in resp
    sid = resp["result"]["sessionId"]
    assert server.sessions[sid].cc_user_id == "ou_1"
    assert server.sessions[sid].cc_project == "p"


def test_session_new_accepts_env_injection(monkeypatch):
    """cc-connect injects CC_PROJECT/CC_SESSION_KEY/CC_USER_ID into the ACP
    subprocess env; the shim must accept exactly that path."""
    monkeypatch.setenv("CC_PROJECT", "proj-env")
    monkeypatch.setenv("CC_SESSION_KEY", "feishu:oc:ou_2")
    monkeypatch.setenv("CC_USER_ID", "ou_2")
    server = _server()
    resp = _call(server, "session/new", {"sessionConfig": {}})
    assert "result" in resp, resp
    sid = resp["result"]["sessionId"]
    session = server.sessions[sid]
    assert session.cc_project == "proj-env"
    assert session.cc_session_key == "feishu:oc:ou_2"
    assert session.cc_user_id == "ou_2"


def test_partial_session_config_is_rejected(monkeypatch):
    """An identity is ATOMIC: overriding only cc_user_id while inheriting the
    env project/session key is rejected (half-spoofing impossible)."""
    monkeypatch.setenv("CC_PROJECT", "proj-env")
    monkeypatch.setenv("CC_SESSION_KEY", "k-env")
    monkeypatch.setenv("CC_USER_ID", "ou-env")
    server = _server()
    resp = _call(
        server,
        "session/new",
        {"sessionConfig": {"cc_user_id": "ou-spoof"}},
    )
    assert "error" in resp
    assert "cc-connect identity missing" in resp["error"]["message"]
    assert server.sessions == {}


def test_full_session_config_wins_over_env(monkeypatch):
    monkeypatch.setenv("CC_PROJECT", "proj-env")
    monkeypatch.setenv("CC_SESSION_KEY", "k-env")
    monkeypatch.setenv("CC_USER_ID", "ou-env")
    server = _server()
    resp = _call(
        server,
        "session/new",
        {"sessionConfig": {"cc_project": "p-explicit", "cc_session_key": "k-explicit", "cc_user_id": "ou-explicit"}},
    )
    assert "result" in resp, resp
    sid = resp["result"]["sessionId"]
    session = server.sessions[sid]
    assert session.cc_project == "p-explicit"
    assert session.cc_session_key == "k-explicit"
    assert session.cc_user_id == "ou-explicit"
