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
from typing import NamedTuple

logger = logging.getLogger("researchd.feishu_doc")

SECTION_MARKER_PREFIX = "<!--rd:"

# docx block_type values (lark docx): 1=page, 2=text, 3..9=heading1..7
_TEXT_BLOCK_TYPES = {2, 3, 4, 5, 6, 7, 8, 9}

_MAX_RETRIES = 3
_BACKOFF_BASE_S = 0.5


class FeishuDocError(RuntimeError):
    """docx API failure. `code` carries the lark business code when known
    (e.g. 1061002 = document revision conflict)."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class FeishuDocRevisionConflict(FeishuDocError):
    """The document moved under us (revision conflict). The caller must
    re-read and decide: adopt-if-equal or treat as a human edit."""


class DocumentCreated(NamedTuple):
    """Result of FeishuDocClient.create_document."""

    document_id: str
    revision_id: int | None


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

    # ------------------------------------------------------------ create
    async def create_document(self, title: str, *, folder_token: str | None = None) -> DocumentCreated:
        """Create a new feishu docx document. Errors only carry the business
        code — the raw platform response is never surfaced (no secret/body
        leakage into logs)."""
        import lark_oapi as lark

        def _create():
            req = (
                lark.docx.v1.CreateDocumentRequest.builder()
                .request_body(
                    lark.docx.v1.CreateDocumentRequestBody.builder()
                    .title(title)
                    .folder_token(folder_token or "")
                    .build()
                )
                .build()
            )
            return self._client().docx.v1.document.create(req)

        resp = await self._call(_create, "create_document")
        data = resp.data  # type: ignore[union-attr]
        # lark-oapi wraps the payload as data.document (CreateDocumentResponseBody)
        document = getattr(data, "document", None)
        document_id = getattr(document, "document_id", None) if document is not None else None
        if not document_id:
            raise FeishuDocError("create_document returned no document_id", code=resp.code)
        revision_id = getattr(document, "revision_id", None) if document is not None else None
        logger.info("feishu document created: document_id=%s (revision=%s)", document_id, revision_id)
        return DocumentCreated(document_id=document_id, revision_id=revision_id)

    async def add_permission_member(
        self,
        document_id: str,
        *,
        member_type: str,  # "openchat" | "openid" | "userid" | "email"
        member_id: str,
        perm: str = "full_access",  # view | edit | full_access
    ) -> bool:
        """Share the document with a member (group / user). full_access grants
        edit. Returns False when the platform denies it (missing scope) so the
        caller can fall back to a user collaborator — the denial reason is
        logged by code only, never the raw body."""
        import lark_oapi as lark

        def _add():
            from lark_oapi.api.drive.v1.model.base_member import BaseMember

            req = (
                lark.drive.v1.CreatePermissionMemberRequest.builder()
                .token(document_id)
                .type("docx")
                .request_body(
                    BaseMember.builder()
                    .member_type(member_type)
                    .member_id(member_id)
                    .perm(perm)
                    .build()
                )
                .build()
            )
            return self._client().drive.v1.permission_member.create(req)

        resp = await self._call(_add, "add_permission_member")
        if resp.success():
            logger.info(
                "feishu doc collaborator added: document_id=%s member_type=%s perm=%s",
                document_id, member_type, perm,
            )
            return True
        logger.warning(
            "feishu doc collaborator denied: document_id=%s member_type=%s code=%s (raw body withheld)",
            document_id, member_type, resp.code,
        )
        return False

    async def remove_permission_member(
        self,
        document_id: str,
        *,
        member_type: str,
        member_id: str,
    ) -> bool:
        """Revoke a collaborator. Returns False when the platform denies it
        (missing scope); the denial is logged by code only."""
        import lark_oapi as lark

        def _remove():
            from lark_oapi.api.drive.v1.model.delete_permission_member_request import (
                DeletePermissionMemberRequest,
            )

            req = (
                lark.drive.v1.DeletePermissionMemberRequest.builder()
                .token(document_id)
                .type("docx")
                .member_type(member_type)
                .member_id(member_id)
                .build()
            )
            return self._client().drive.v1.permission_member.delete(req)

        resp = await self._call(_remove, "remove_permission_member")
        if resp.success():
            logger.info(
                "feishu doc collaborator removed: document_id=%s member_type=%s",
                document_id, member_type,
            )
            return True
        logger.warning(
            "feishu doc collaborator revoke denied: document_id=%s member_type=%s code=%s (raw body withheld)",
            document_id, member_type, resp.code,
        )
        return False

    # real platform rate-limit / transient business codes (lark docx + auth):
    # 99991663 tenant_access_token rate limit; 99991400 concurrent limit;
    # 1061001/1061002 document not ready/version conflicts handled separately
    _RETRY_BUSINESS_CODES = frozenset(
        {429, 500, 502, 503, 504, 99991663, 99991400, 99991661, 99991662}
    )

    async def _call(self, fn, op: str):
        """Retry TRANSIENT failures only, honoring Retry-After when present.
        lark-oapi calls are synchronous; run them in a worker thread.
        Business errors outside the retry set (incl. real conflicts) surface
        immediately — they are NOT disguised as retryable failures."""
        import time as _time

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
            retryable = resp.code in self._RETRY_BUSINESS_CODES
            try:
                status = resp.status_code if hasattr(resp, "status_code") else None
                retryable = retryable or (status is not None and status in (429, 500, 502, 503, 504))
            except Exception:  # noqa: BLE001
                pass
            if resp.code == 1061002:
                raise FeishuDocRevisionConflict(f"{op}: document revision conflict (1061002)")
            last = FeishuDocError(f"{op} failed code={resp.code} msg withheld", code=resp.code)
            if not retryable or attempt >= self.max_retries - 1:
                break
            # honor Retry-After when the platform provides it
            wait = _BACKOFF_BASE_S * (2**attempt)
            try:
                ra = resp.header("Retry-After")
                if ra:
                    wait = max(wait, float(ra))
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(wait)
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
        sections, _revision = await self.list_blocks_with_revision(document_id)
        return sections

    async def list_blocks_with_revision(self, document_id: str) -> tuple[dict[str, str], int | None]:
        """section_key -> block text PLUS the current document revision id
        (lark returns data.revision_id on the list response). The revision is
        passed back to create/update for optimistic concurrency."""
        from lark_oapi.api.docx.v1 import ListDocumentBlockRequest, ListDocumentBlockRequestBuilder

        client = self._client()
        sections: dict[str, str] = {}
        page_token: str | None = None
        revision: int | None = None
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
            if revision is None and getattr(resp.data, "revision_id", None) is not None:
                revision = int(resp.data.revision_id)
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
        return sections, revision

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

    async def create_block(self, document_id: str, section_key: str, text: str, *, document_revision_id: int | None = None) -> None:
        from lark_oapi.api.docx.v1 import (
            Block,
            CreateDocumentBlockChildrenRequestBodyBuilder,
            CreateDocumentBlockChildrenRequestBuilder,
        )

        client = self._client()
        # idempotent create: if the marked block already exists (crash after
        # a successful create + retry), treat it as done — never duplicate.
        # ABSENCE (None) and API FAILURE (raised) are strictly distinct.
        existing = await self._find_block_id(document_id, section_key)
        if existing is not None:
            await self.update_block(document_id, section_key, text, document_revision_id=document_revision_id)
            return
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
        builder = (
            CreateDocumentBlockChildrenRequestBuilder()
            .document_id(document_id)
            .block_id(root_id)
        )
        if document_revision_id is not None:
            builder.document_revision_id(document_revision_id)
        req = (
            builder.request_body(
                CreateDocumentBlockChildrenRequestBodyBuilder()
                .children([block])
                .build()
            )
            .build()
        )
        await self._call(
            lambda: client.docx.v1.document_block_children.create(req),
            f"docx create block {section_key}",
        )

    async def update_block(self, document_id: str, section_key: str, text: str, *, document_revision_id: int | None = None) -> None:
        from lark_oapi.api.docx.v1 import (
            PatchDocumentBlockRequestBuilder,
            UpdateBlockRequest,
        )

        client = self._client()
        block_id = await self._find_block_id(document_id, section_key)
        if block_id is None:
            raise FeishuDocError(f"document {document_id}: section block {section_key!r} not found (create it first)")
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
        builder = (
            PatchDocumentBlockRequestBuilder()
            .document_id(document_id)
            .block_id(block_id)
        )
        if document_revision_id is not None:
            builder.document_revision_id(document_revision_id)
        req = builder.request_body(body).build()
        await self._call(
            lambda: client.docx.v1.document_block.patch(req),
            f"docx update block {section_key}",
        )

    async def _find_block_id(self, document_id: str, section_key: str) -> str | None:
        """block_id of the marked section block, or None when the section is
        ABSENT. API failures raise — absence and failure are distinct (a
        failed list is never mistaken for 'not present', which would cause
        duplicate creates)."""
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
        return None  # section absent (NOT an error)

    async def delete_block(self, document_id: str, section_key: str) -> None:
        """Delete the section block. batch_delete_children removes children of
        a parent block BY INDEX, so we locate the page (root) block and the
        section block in document order and delete the section as the page's
        child at that index."""
        from lark_oapi.api.docx.v1 import (
            BatchDeleteDocumentBlockChildrenRequest,
            BatchDeleteDocumentBlockChildrenRequestBuilder,
            ListDocumentBlockRequest,
            ListDocumentBlockRequestBuilder,
        )

        client = self._client()
        page_id = None
        blocks = []
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
                "docx list for delete",
            )
            blocks.extend(resp.data.items or [])
            if not resp.data.has_more or not resp.data.page_token:
                break
            page_token = resp.data.page_token
        target_index = None
        for i, block in enumerate(blocks):
            if block.block_type == 1 and page_id is None:
                page_id = block.block_id
                continue
            if block.block_type not in _TEXT_BLOCK_TYPES:
                continue
            key, _ = self._parse_section(self._block_text(block))
            if key == section_key:
                # child index of the page block == document order offset
                target_index = i - 1
                break
        if page_id is None or target_index is None:
            raise FeishuDocError(f"document {document_id}: section {section_key!r} not found for delete")
        # (target_index derivation unchanged; find is absence-aware)
        from lark_oapi.api.docx.v1 import BatchDeleteDocumentBlockChildrenRequestBodyBuilder

        req = (
            BatchDeleteDocumentBlockChildrenRequestBuilder()
            .document_id(document_id)
            .block_id(page_id)
            .request_body(
                BatchDeleteDocumentBlockChildrenRequestBodyBuilder()
                .start_index(target_index)
                .end_index(target_index + 1)
                .build()
            )
            .build()
        )
        await self._call(
            lambda: client.docx.v1.document_block_children.batch_delete(req),
            f"docx delete block {section_key}",
        )
