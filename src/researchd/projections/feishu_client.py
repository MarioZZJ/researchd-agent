"""Feishu docx client for project document projection (IMPLEMENTATION.md §21.4).

Real lark-oapi adapter: list/create/update document blocks by section.

Section mapping: each section is ONE text block whose first line carries the
marker `<!--rd:{section_key}-->`; the block text after the marker is the
section body. list_blocks() scans the document (paginated), parses markers,
and returns section_key -> body. create_block() appends a new marked block
under the document root (page) block; update_block() PATCHes the block in
place (update_text_elements).

Guarantees:
- writes are idempotent at the content level (same text -> same block);
- transient API failures (429 / 5xx) are retried with capped exponential
  backoff; credentials errors and 4xx are surfaced immediately;
- human edits are never overwritten by this client — callers (projection
  layer) compare the persisted hash against the remote truth first, and
  list_blocks() returns the CURRENT remote text so the diff is authoritative.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger("researchd.feishu_doc")

SECTION_MARKER_PREFIX = "<!--rd:"

# docx block_type values (lark docx): 1=page, 2=text, 3..9=heading1..7
_TEXT_BLOCK_TYPES = {2, 3, 4, 5, 6, 7, 8, 9}

_MAX_RETRIES = 3
_BACKOFF_BASE_S = 0.5


class FeishuDocError(RuntimeError):
    pass


class FeishuDocClient:
    """lark-oapi docx adapter: list/create/update blocks by section."""

    def __init__(self, *, app_id_env: str = "LARK_APP_ID", app_secret_env: str = "LARK_APP_SECRET", max_retries: int = _MAX_RETRIES):
        self.app_id_env = app_id_env
        self.app_secret_env = app_secret_env
        self.max_retries = max_retries

    # ------------------------------------------------------------ client
    def _client(self):
        try:
            import lark_oapi as lark
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("lark-oapi not installed (uv sync --all-groups)") from exc
        app_id = os.environ.get(self.app_id_env)
        app_secret = os.environ.get(self.app_secret_env)
        if not app_id or not app_secret:
            raise FeishuDocError(
                f"feishu credentials missing: {self.app_id_env}/{self.app_secret_env} "
                "(set them to use the real doc client)"
            )
        return lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    async def _call(self, fn, op: str):
        """Retry transient failures (429/5xx) with capped exponential backoff.
        lark-oapi calls are synchronous; run them in a worker thread."""
        last = None
        for attempt in range(self.max_retries):
            try:
                resp = await asyncio.to_thread(fn)
            except Exception as exc:  # network/transport: retry
                last = exc
                if attempt >= self.max_retries - 1:
                    break
                await asyncio.sleep(_BACKOFF_BASE_S * (2**attempt))
                continue
            if resp.success():
                return resp
            last = FeishuDocError(f"{op} failed code={resp.code} msg={resp.msg}")
            if resp.code in (429, 500, 502, 503, 504):
                if attempt >= self.max_retries - 1:
                    break
                await asyncio.sleep(_BACKOFF_BASE_S * (2**attempt))
                continue
            raise last
        raise last  # type: ignore[misc]

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _block_text(block) -> str:  # noqa: ANN001
        """Concatenated text of a docx block (text/heading only)."""
        if block is None or block.text is None or not block.text.elements:
            return ""
        parts = []
        for el in block.text.elements:
            if el.text_run is not None and el.text_run.content:
                parts.append(el.text_run.content)
        return "".join(parts)

    @staticmethod
    def _parse_section(text: str) -> tuple[str | None, str]:
        """(section_key, body) from a marked block text, or (None, text)."""
        if text.startswith(SECTION_MARKER_PREFIX):
            end = text.find("-->")
            if end > 0:
                key = text[len(SECTION_MARKER_PREFIX):end].strip()
                body = text[end + 3:].lstrip("\n")
                return key, body
        return None, text

    # ------------------------------------------------------------ platform
    async def list_blocks(self, document_id: str) -> dict[str, str]:
        """section_key -> block text (current remote truth, paginated)."""
        from lark_oapi.api.docx.v1 import ListDocumentBlockRequest, ListDocumentBlockRequestBuilder

        client = self._client()
        sections: dict[str, str] = {}
        page_token: str | None = None
        while True:
            builder = (
                ListDocumentBlockRequestBuilder()
                .document_id(document_id)
                .page_size(500)
            )
            if page_token:
                builder.page_token(page_token)
            req: ListDocumentBlockRequest = builder.build()
            resp = await self._call(
                lambda: client.docx.v1.document_block.list(req),
                "docx list blocks",
            )
            items = resp.data.items or []
            for block in items:
                if block.block_type not in _TEXT_BLOCK_TYPES:
                    continue
                text = self._block_text(block)
                key, body = self._parse_section(text)
                if key is not None:
                    sections[key] = body
            if not resp.data.has_more or not resp.data.page_token:
                break
            page_token = resp.data.page_token
        return sections

    async def _root_block_id(self, document_id: str) -> str:
        from lark_oapi.api.docx.v1 import ListDocumentBlockRequest, ListDocumentBlockRequestBuilder

        client = self._client()
        req = (
            ListDocumentBlockRequestBuilder()
            .document_id(document_id)
            .page_size(50)
            .build()
        )
        resp = await self._call(
            lambda: client.docx.v1.document_block.list(req),
            "docx list root block",
        )
        for block in resp.data.items or []:
            if block.block_type == 1:  # page block
                return block.block_id
        raise FeishuDocError(f"document {document_id}: no page block found")

    async def create_block(self, document_id: str, section_key: str, text: str) -> None:
        from lark_oapi.api.docx.v1 import (
            Block,
            CreateDocumentBlockChildrenRequestBodyBuilder,
            CreateDocumentBlockChildrenRequestBuilder,
            Text,
            TextElement,
            TextRun,
        )

        client = self._client()
        root_id = await self._root_block_id(document_id)
        content = f"{SECTION_MARKER_PREFIX}{section_key}-->\n{text}"
        block = Block(
            {
                "block_type": 2,
                "text": {
                    "elements": [
                        {"text_run": {"content": content}},
                    ]
                },
            }
        )
        req = (
            CreateDocumentBlockChildrenRequestBuilder()
            .document_id(document_id)
            .block_id(root_id)
            .request_body(
                CreateDocumentBlockChildrenRequestBodyBuilder()
                .children([block])
                .build()
            )
            .build()
        )
        await self._call(
            lambda: client.docx.v1.document_block.create(req),
            f"docx create block {section_key}",
        )

    async def update_block(self, document_id: str, section_key: str, text: str) -> None:
        from lark_oapi.api.docx.v1 import (
            PatchDocumentBlockRequestBuilder,
            UpdateBlockRequest,
        )

        client = self._client()
        block_id = await self._find_block_id(document_id, section_key)
        content = f"{SECTION_MARKER_PREFIX}{section_key}-->\n{text}"
        body = UpdateBlockRequest(
            {
                "update_text_elements": {
                    "elements": [
                        {"text_run": {"content": content}},
                    ]
                },
            }
        )
        req = (
            PatchDocumentBlockRequestBuilder()
            .document_id(document_id)
            .block_id(block_id)
            .request_body(body)
            .build()
        )
        await self._call(
            lambda: client.docx.v1.document_block.patch(req),
            f"docx update block {section_key}",
        )

    async def _find_block_id(self, document_id: str, section_key: str) -> str:
        """block_id of the marked section block (raise if missing)."""
        from lark_oapi.api.docx.v1 import ListDocumentBlockRequest, ListDocumentBlockRequestBuilder

        client = self._client()
        page_token: str | None = None
        while True:
            builder = (
                ListDocumentBlockRequestBuilder()
                .document_id(document_id)
                .page_size(500)
            )
            if page_token:
                builder.page_token(page_token)
            req: ListDocumentBlockRequest = builder.build()
            resp = await self._call(
                lambda: client.docx.v1.document_block.list(req),
                "docx find section block",
            )
            for block in resp.data.items or []:
                if block.block_type not in _TEXT_BLOCK_TYPES:
                    continue
                key, _body = self._parse_section(self._block_text(block))
                if key == section_key:
                    return block.block_id
            if not resp.data.has_more or not resp.data.page_token:
                break
            page_token = resp.data.page_token
        raise FeishuDocError(f"document {document_id}: section block {section_key!r} not found (create it first)")
