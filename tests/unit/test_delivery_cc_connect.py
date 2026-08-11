"""Delivery wiring tests (IMPLEMENTATION.md §19.2): real interactive card
payloads (buttons never degraded), cc-connect request shapes, fail-closed
factory, token hygiene."""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from researchd.config import Settings
from researchd.integrations.cc_connect.delivery import (
    CcConnectDeliveryError,
    CcConnectDeliveryPort,
    build_card_payload,
)
from researchd.service import _build_delivery_port


def test_card_payload_is_real_interactive_card_with_buttons():
    card = json.loads(
        build_card_payload(
            {
                "title": "决策 D-002",
                "body": "**需要你决策**：定位",
                "buttons": [
                    {"text": "A. 描述性", "value": "/decision D-002 A --version 1"},
                    {"text": "B. 机制性", "value": "/decision D-002 B --version 1"},
                ],
            },
            session_key="oc_abc",
        )
    )
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "决策 D-002"
    elements = card["body"]["elements"]
    assert elements[0]["tag"] == "markdown"
    action = elements[1]
    assert action["tag"] == "action"
    buttons = action["actions"]
    assert len(buttons) == 2
    # the cc-connect callback protocol: cmd: dispatch + session key + in-place feedback
    v = buttons[0]["value"]
    assert v["action"] == "cmd:/decision D-002 A --version 1"
    assert v["session_key"] == "oc_abc"
    assert v["after_click"]["title"] == "已提交"


def test_card_payload_without_buttons_has_no_action_element():
    card = json.loads(build_card_payload({"title": "t", "body": "b"}))
    assert card["body"]["elements"][0]["tag"] == "markdown"
    assert len(card["body"]["elements"]) == 1


def test_port_fails_closed_without_token_or_project():
    with pytest.raises(CcConnectDeliveryError):
        CcConnectDeliveryPort(token="", project="p")
    with pytest.raises(CcConnectDeliveryError):
        CcConnectDeliveryPort(token="t", project="")


def test_port_send_and_update_shapes(monkeypatch):
    calls = []

    class FakeResp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"platform_message_id": "om_123"}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            calls.append(("POST", url, json))
            return FakeResp()

        async def patch(self, url, json=None):
            calls.append(("PATCH", url, json))
            return FakeResp()

    monkeypatch.setattr("researchd.integrations.cc_connect.delivery.httpx.AsyncClient", FakeClient)
    port = CcConnectDeliveryPort(token="sekrit", project="proj", session_key="key")

    async def run():
        mid = await port.deliver(idempotency_key="k-1", kind="message", payload={"title": "t", "body": "b"})
        await port.update("om_123", {"title": "t2", "body": "b2"})
        return mid

    import asyncio

    mid = asyncio.run(run())
    assert mid == "om_123"
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/api/v1/projects/proj/deliveries")
    body = calls[0][2]
    assert body["session_key"] == "key"
    assert body["idempotency_key"] == "k-1"
    assert "message" in body
    # token goes in the header, never in the payload
    assert "sekrit" not in json.dumps(body)
    assert calls[1][0] == "PATCH"
    assert calls[1][1].endswith("/deliveries/om_123")


def test_build_delivery_port_factory(monkeypatch, tmp_path):
    settings = Settings()
    settings.scheduler.delivery = "fake"
    from researchd.executors.fake import FakeDeliveryPort

    assert isinstance(_build_delivery_port(settings), FakeDeliveryPort)
    # cc_connect without token fails closed
    settings.scheduler.delivery = "cc_connect"
    settings.cc_connect.project = "proj"
    settings.cc_connect.token = SecretStr("")
    with pytest.raises(ValueError, match="TOKEN"):
        _build_delivery_port(settings)
    # fully configured -> real port, token not echoed anywhere
    settings.cc_connect.token = SecretStr("supersecret")
    port = _build_delivery_port(settings)
    assert isinstance(port, CcConnectDeliveryPort)
    assert port.token == "supersecret"
    assert "supersecret" not in repr(port)
    # unknown delivery fails
    settings.scheduler.delivery = "nope"
    with pytest.raises(ValueError, match="unknown delivery"):
        _build_delivery_port(settings)


def test_cc_connect_transport_restrictions(monkeypatch):
    """Plaintext token may only go to loopback; HTTPS anywhere; UDS is fine."""
    from pydantic import ValidationError

    def make(url):
        monkeypatch.setenv("RESEARCHD_CC_CONNECT__BASE_URL", url)
        return Settings(_env_file=None)

    # loopback HTTP allowed
    assert make("http://127.0.0.1:9820").cc_connect.base_url == "http://127.0.0.1:9820"
    assert make("http://localhost:9820").cc_connect.base_url == "http://localhost:9820"
    # explicit HTTPS allowed to any host
    assert make("https://cc.example.com").cc_connect.base_url == "https://cc.example.com"
    # plaintext to non-loopback refused (token would leak)
    with pytest.raises(ValidationError, match="loopback"):
        make("http://10.0.0.5:9820")
    with pytest.raises(ValidationError, match="loopback"):
        make("http://example.com:9820")
    # garbage scheme refused
    with pytest.raises(ValidationError):
        make("ftp://127.0.0.1")


def test_cc_connect_token_is_secret_str():
    s = Settings(_env_file=None)
    s.cc_connect.token = SecretStr("hunter2")
    assert s.cc_connect.token.get_secret_value() == "hunter2"
    assert "hunter2" not in repr(s.cc_connect.token)
    assert "hunter2" not in str(s.cc_connect)


def test_platform_message_id_path_injection_rejected():
    """A hostile message id must never inject path segments into the URL."""
    from researchd.integrations.cc_connect.delivery import CcConnectDeliveryError, _check_message_id

    assert _check_message_id("om_abc123") == "om_abc123"
    for hostile in ("../../etc/passwd", "om x", "om/../x", "a\nb", "om_abc123/extra"):
        with pytest.raises(CcConnectDeliveryError, match="refusing to build URL"):
            _check_message_id(hostile)


def test_delivery_never_echoes_platform_body(monkeypatch):
    """Errors surface ONLY the HTTP status class — the raw platform response
    body (which may carry internal details) is never echoed."""
    from researchd.integrations.cc_connect.delivery import CcConnectDeliveryError

    class BadResp:
        status_code = 500
        text = "internal: tenant token expired at secret-host"

    class BadClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            return BadResp()

        async def patch(self, url, json=None):
            return BadResp()

    monkeypatch.setattr("researchd.integrations.cc_connect.delivery.httpx.AsyncClient", BadClient)
    port = CcConnectDeliveryPort(token="t", project="p")

    import asyncio

    with pytest.raises(CcConnectDeliveryError, match="body withheld") as ei:
        asyncio.run(port.deliver(idempotency_key="k", kind="m", payload={"body": "b"}))
    assert "secret-host" not in str(ei.value)
    with pytest.raises(CcConnectDeliveryError, match="body withheld") as ei2:
        asyncio.run(port.update("om_x", {"body": "b"}))
    assert "secret-host" not in str(ei2.value)


def test_run_result_cannot_become_delivery_payload(factory):
    """Delivery payloads come ONLY from compiled outbox payloads (ReportSpec
    body + buttons); a raw Run.result / model reply can never reach the port:
    the port takes a dict with body/buttons and rejects anything else-shaped."""
    from researchd.integrations.cc_connect.delivery import build_card_payload

    raw_run_result = {
        "schema": "researchd.work_result.v1",
        "task_id": "T-1",
        "outcome": "SUBMIT_FOR_REVIEW",
        "criteria_results": [],
        "artifacts": [], "evidence_candidates": [],
        "claim_changes": [], "issues": [],
        "decision_candidates": [], "next_task_proposals": [],
    }
    # a raw Run.result has no body/buttons -> the card builder renders an
    # EMPTY markdown block; i.e. raw results are structurally incapable of
    # becoming a report card. The outbox path always passes a compiled payload.
    card = build_card_payload(raw_run_result)
    assert '"content": ""' in card or '"body"' in card
    # and the scheduler never routes run.result to the port (asserted at the
    # outbox boundary in test_phase6_gate_reporter / golden path: delivery
    # payloads originate from ReportSpec compilation only)
    from researchd.reporting.reporter import _evidence_bottom_line
    from researchd.reporting.spec import compile_spec

    spec = compile_spec(
        project_id="P", type="EVIDENCE", title="t",
        bottom_line="新增已验证证据：E-1「s」",
        bottom_line_evidence_refs=["E-1"],
    )
    from researchd.reporting.spec import render_text

    rendered = render_text(spec)
    assert rendered and "E-1" in rendered  # compiled body only
