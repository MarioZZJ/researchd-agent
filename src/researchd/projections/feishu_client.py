"""Feishu docx client for project document projection (IMPLEMENTATION.md §21.4).

STATUS: PENDING / B-01 GATED — real API calls are blocked on authorization
(credentials + a target document id). Until then:
- the interface contract (list/create/update by section) is pinned by
  FakeDocPlatform and the deterministic projection tests;
- the exact lark-oapi docx calls (create-block/patch-block shapes, block
  types, revision semantics) MUST be verified against the live API in a gated
  conformance test before this client is enabled (settings.doc_platform).
"""

from __future__ import annotations

import os

from .feishu_doc import DocPlatform


class FeishuDocClient(DocPlatform):
    """lark-oapi docx adapter: list/create/update blocks by section.

    A section maps to one document block (heading + text) created via the
    docx create-block API; updates use the patch-block API. The exact block
    types are pinned by the gated real conformance test once B-01 is lifted.
    """

    def __init__(self, *, app_id_env: str = "LARK_APP_ID", app_secret_env: str = "LARK_APP_SECRET"):
        self.app_id_env = app_id_env
        self.app_secret_env = app_secret_env

    def _client(self):
        try:
            import lark_oapi as lark
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("lark-oapi not installed (uv sync --all-groups)") from exc
        app_id = os.environ.get(self.app_id_env)
        app_secret = os.environ.get(self.app_secret_env)
        if not app_id or not app_secret:
            raise RuntimeError(
                f"feishu credentials missing: {self.app_id_env}/{self.app_secret_env} "
                "(set them or use FakeDocPlatform until B-01 is lifted)"
            )
        return lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    async def list_blocks(self, document_id: str) -> dict[str, str]:
        raise RuntimeError("PENDING (B-01): real feishu docx calls require authorization")

    async def create_block(self, document_id: str, section_key: str, text: str) -> None:
        raise RuntimeError("PENDING (B-01): real feishu docx calls require authorization")

    async def update_block(self, document_id: str, section_key: str, text: str) -> None:
        raise RuntimeError("PENDING (B-01): real feishu docx calls require authorization")
