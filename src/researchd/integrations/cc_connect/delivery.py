"""cc-connect Delivery API client (IMPLEMENTATION.md §19.2).

Sends REAL Feishu interactive cards (schema 2.0): the decision card keeps its
buttons (never degraded to text), the card carries the cc-connect callback
protocol (`value.action = "cmd:<command>"`, `session_key`, `after_click`
in-place feedback), and `platform_message_id` receipts enable in-place PATCH
updates. Raw executor output is NEVER sent through this port — only compiled
report payloads from the outbox.

The token comes from the 0600 settings (env file), is sent only in the
Authorization header / never logged, and the port fails closed when the
cc-connect target is not configured.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ...scheduler.outbox_sender import DeliveryPort

DEFAULT_BASE_URL = "http://127.0.0.1:9820"

# platform message ids are cc-connect/Feishu opaque ids: URL-safe only
_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class CcConnectDeliveryError(RuntimeError):
    pass


def _check_message_id(platform_message_id: str) -> str:
    """Path-safe validation: only URL-safe characters, so a hostile id can
    never inject path segments into the management API URL."""
    if not _MESSAGE_ID_RE.fullmatch(platform_message_id):
        raise CcConnectDeliveryError(
            f"invalid platform_message_id {platform_message_id[:20]!r} (refusing to build URL)"
        )
    return platform_message_id


def build_card_payload(payload: dict, *, session_key: str | None = None) -> str:
    """Compile an interactive card JSON from an outbox payload.

    body: markdown body; buttons: decision options with a command value that
    cc-connect dispatches back as a user message (cmd: protocol), so a button
    click keeps the real platform user id and the decision version.

    NO session_key is injected into the button value: cc-connect derives the
    session from the CLICKER's own identity (chat+user), so in a shared chat
    each member's click is attributed to THAT member — a fixed session_key
    would attribute every click to whoever clicked first.
    """
    body = payload.get("body") or ""
    title = payload.get("title") or "researchd"
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": body}]
    buttons = payload.get("buttons") or []
    if buttons:
        actions: list[dict[str, Any]] = []
        for b in buttons:
            cmd = b.get("value", "")
            if not cmd:
                continue
            value: dict[str, Any] = {"action": f"cmd:{cmd}"}
            # neutral in-place feedback: NOT a success claim — the actual
            # outcome (accepted/expired/non-member/duplicate) is shown by
            # the decision card update after the service processed it
            value["after_click"] = {
                "title": "已收到",
                "color": "blue",
                "markdown": "已收到你的选择，处理结果将随后更新",
            }
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": (b.get("text") or "")[:40]},
                    "type": "primary",
                    "value": value,
                }
            )
        if actions:
            elements.append({"tag": "action", "actions": actions})
    card = {
        # Card 1.0 (NOT schema 2.0): cc-connect's createMessageHandle renders
        # card 1.0 via the Im.Message.Create interactive path, and card 2.0
        # rejects the 1.0 `tag: action` button element ("cards of schema V2
        # no longer support this capability"). Drop schema to stay 1.0.
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title[:100]}},
        "elements": elements,
    }
    return json.dumps(card, ensure_ascii=False)


class CcConnectDeliveryPort(DeliveryPort):
    """Delivery port speaking to cc-connect's Management API.

    transport: uds (api.sock) or tcp localhost with bearer token.
    """

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, token: str, project: str, session_key: str = "", uds: str | None = None):
        if not token:
            raise CcConnectDeliveryError("cc-connect token is required (fail-closed)")
        if not project:
            raise CcConnectDeliveryError("cc-connect project is required (fail-closed)")
        self.base_url = base_url
        self.token = token  # never logged; Authorization header only
        self.project = project
        self.session_key = session_key
        self.uds = uds

    def _client(self) -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(uds=self.uds) if self.uds else None
        headers = {"Authorization": f"Bearer {self.token}"}
        return httpx.AsyncClient(transport=transport, headers=headers, timeout=15.0)

    async def deliver(
        self,
        *,
        idempotency_key: str,
        kind: str,
        payload: dict,
        attachments: list | None = None,
        project_id: str | None = None,
    ) -> str:
        message = build_card_payload(payload, session_key=self.session_key)
        async with self._client() as client:
            resp = await client.post(
                f"{self.base_url}/api/v1/projects/{self.project}/deliveries",
                json={
                    "session_key": self.session_key,
                    "message": message,
                    "idempotency_key": idempotency_key,
                },
            )
        if resp.status_code >= 400:
            # NEVER echo the raw platform response body (it may carry internal
            # details); only the status class is surfaced
            raise CcConnectDeliveryError(
                f"cc-connect delivery failed: HTTP {resp.status_code} (body withheld)"
            )
        data = resp.json()
        # cc-connect wraps payloads as {"data": {...}, "ok": true}
        inner = data.get("data") if isinstance(data, dict) else None
        mid = (inner or {}).get("platform_message_id", "") if isinstance(inner, dict) else ""
        if mid:
            _check_message_id(mid)
        return mid

    async def update(self, platform_message_id: str, payload: dict) -> None:
        _check_message_id(platform_message_id)
        message = build_card_payload(payload, session_key=self.session_key)
        async with self._client() as client:
            resp = await client.patch(
                f"{self.base_url}/api/v1/projects/{self.project}/deliveries/{platform_message_id}",
                json={"session_key": self.session_key, "message": message},
            )
        if resp.status_code >= 400:
            # NEVER echo the raw platform response body (see deliver())
            raise CcConnectDeliveryError(
                f"cc-connect delivery update failed: HTTP {resp.status_code} (body withheld)"
            )
