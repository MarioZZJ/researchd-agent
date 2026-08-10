"""cc-connect Delivery API client (IMPLEMENTATION.md §19.2).

Backed by the narrow patch in integrations/cc-connect/patch/. Real sends are
GATED (B-01); the scheduler uses FakeDeliveryPort until authorized.
"""

from __future__ import annotations

import httpx

from ...scheduler.outbox_sender import DeliveryPort


class CcConnectDeliveryPort(DeliveryPort):
    """Delivery port speaking to cc-connect's Management API.

    transport: uds (api.sock) or tcp localhost with bearer token.
    """

    def __init__(self, *, base_url: str, token: str, project: str, session_key: str, uds: str | None = None):
        self.base_url = base_url
        self.token = token
        self.project = project
        self.session_key = session_key
        self.uds = uds

    def _client(self) -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(uds=self.uds) if self.uds else None
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
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
        body = payload.get("body", "")
        # card buttons are rendered as deterministic text lines until the
        # platform card API is wired (PARTIAL, gated by B-01)
        buttons = payload.get("buttons") or []
        if buttons:
            body += "\n\n选项：\n" + "\n".join(
                f"- {b.get('text', '')}：{b.get('scientific_consequence', '')} "
                f"（{b.get('value', '')}）"
                for b in buttons
            )
        async with self._client() as client:
            resp = await client.post(
                f"{self.base_url}/api/v1/projects/{self.project}/deliveries",
                json={
                    "session_key": self.session_key,
                    "message": body,
                    "idempotency_key": idempotency_key,
                },
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"cc-connect delivery failed: HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return data.get("platform_message_id", "")

    async def update(self, platform_message_id: str, payload: dict) -> None:
        body = payload.get("body", "")
        async with self._client() as client:
            resp = await client.patch(
                f"{self.base_url}/api/v1/projects/{self.project}/deliveries/{platform_message_id}",
                json={"session_key": self.session_key, "message": body},
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"cc-connect delivery update failed: HTTP {resp.status_code}: {resp.text[:300]}")
