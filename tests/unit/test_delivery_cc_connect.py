"""Delivery wiring tests (IMPLEMENTATION.md §19.2): real interactive card
payloads (buttons never degraded), cc-connect request shapes, fail-closed
factory, token hygiene."""

from __future__ import annotations

import json

import pytest

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
    settings.cc_connect.token = ""
    with pytest.raises(ValueError, match="TOKEN"):
        _build_delivery_port(settings)
    # fully configured -> real port, token not echoed anywhere
    settings.cc_connect.token = "supersecret"
    port = _build_delivery_port(settings)
    assert isinstance(port, CcConnectDeliveryPort)
    assert port.token == "supersecret"
    assert "supersecret" not in repr(port)
    # unknown delivery fails
    settings.scheduler.delivery = "nope"
    with pytest.raises(ValueError, match="unknown delivery"):
        _build_delivery_port(settings)
