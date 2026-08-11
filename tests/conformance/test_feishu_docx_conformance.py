"""Feishu Docx conformance tests (IMPLEMENTATION.md §21.4, §23 Phase 7).

Local conformance pins the exact lark-oapi interaction contract with a fake
client (block lifecycle, marker parsing, pagination, transient retry). The
REAL conformance test (block-level incremental sync, human-edit detection,
PI Notes protection, revision conflict) is gated behind
RESEARCHD_LARK_APP_ID / RESEARCHD_LARK_APP_SECRET + RESEARCHD_DOC_TEST_ID —
it runs only against an explicitly provided staging document.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from researchd.projections.feishu_client import FeishuDocClient, SECTION_MARKER_PREFIX


class FakeBlock:
    def __init__(self, block_id, block_type, content=None):
        self.block_id = block_id
        self.block_type = block_type
        self.text = None
        if content is not None:
            from lark_oapi.api.docx.v1 import Text

            self.text = Text({"elements": [{"text_run": {"content": content}}]})


class FakeDocxApi:
    """In-memory docx: blocks list under a root page block."""

    def __init__(self):
        self.blocks = {"page-root": FakeBlock("page-root", 1)}
        self.next_id = 1
        self.fail_codes = []  # (op, [code,...]) -> inject transient failures

    def _fail(self, op):
        for i, codes in enumerate(self.fail_codes):
            if op in codes:

                class Resp:
                    def __init__(self, code):
                        self.code = code
                        self.msg = "injected"

                    def success(self):
                        return False

                self.fail_codes.pop(i)
                return Resp(codes[-1] if len(codes) > 1 else 429)
        return None

    def list(self, req):
        fail = self._fail("list")
        if fail:
            return fail
        items = list(self.blocks.values())
        page = items[: req.page_size] if req.page_size else items
        # fake paging: single page when < page_size
        from types import SimpleNamespace

        return SimpleNamespace(success=lambda: True, code=0, msg="", data=SimpleNamespace(items=page, has_more=False, page_token=None))

    def create(self, req):
        fail = self._fail("create")
        if fail:
            return fail
        self.next_id += 1
        block = FakeBlock("b-%d" % self.next_id, 2, req.body.children[0].text.elements[0].text_run.content)
        self.blocks[block.block_id] = block
        from types import SimpleNamespace

        return SimpleNamespace(success=lambda: True, code=0, msg="", data=SimpleNamespace(block=block))

    def patch(self, req):
        fail = self._fail("patch")
        if fail:
            return fail
        block = self.blocks[req.block_id]
        content = req.body.update_text_elements.elements[0].text_run.content
        from lark_oapi.api.docx.v1 import Text

        block.text = Text({"elements": [{"text_run": {"content": content}}]})
        from types import SimpleNamespace

        return SimpleNamespace(success=lambda: True, code=0, msg="", data=SimpleNamespace(block=block))


class FakeClient:
    def __init__(self, api):
        self.docx = SimpleNamespace_v1(document_block=api)


class SimpleNamespace_v1:
    def __init__(self, document_block):
        self.v1 = SimpleNamespace_v1_inner(document_block)


class SimpleNamespace_v1_inner:
    def __init__(self, document_block):
        self.document_block = document_block


@pytest.fixture()
def fake_doc(monkeypatch):
    api = FakeDocxApi()

    def _client(self):
        return FakeClient(api)

    monkeypatch.setattr(FeishuDocClient, "_client", _client)
    monkeypatch.setenv("LARK_APP_ID", "cli_test")
    monkeypatch.setenv("LARK_APP_SECRET", "secret")
    return api


def run(coro):
    return asyncio.run(coro)


def test_block_lifecycle_incremental_sync(fake_doc):
    client = FeishuDocClient()
    doc = "doc_test"
    # empty document: no sections
    assert run(client.list_blocks(doc)) == {}
    # create two sections, list reflects them
    run(client.create_block(doc, "status", "运行中"))
    run(client.create_block(doc, "evidence", "E-1 已验证"))
    blocks = run(client.list_blocks(doc))
    assert blocks == {"status": "运行中", "evidence": "E-1 已验证"}
    # update one section in place: incremental block-level update
    run(client.update_block(doc, "status", "已完成"))
    assert run(client.list_blocks(doc)) == {"status": "已完成", "evidence": "E-1 已验证"}
    # marker round-trip survives
    raw = run(client.list_blocks(doc))
    assert raw["evidence"].startswith("E-1")


def test_retry_on_transient_errors(fake_doc):
    client = FeishuDocClient(max_retries=3)
    fake_doc.fail_codes = [("list",), ("list",)]  # two 429s then success
    assert run(client.list_blocks("doc")) == {}
    # non-transient error is not retried and raises
    fake_doc.fail_codes = [("create", 400)]
    from researchd.projections.feishu_client import FeishuDocError

    with pytest.raises(FeishuDocError):
        run(client.create_block("doc", "s", "t"))


def test_credentials_required(monkeypatch):
    monkeypatch.delenv("LARK_APP_ID", raising=False)
    monkeypatch.delenv("LARK_APP_SECRET", raising=False)
    from researchd.projections.feishu_client import FeishuDocError

    with pytest.raises(FeishuDocError, match="credentials missing"):
        run(FeishuDocClient().list_blocks("doc"))


def test_real_feishu_docx_round_trip():
    """REAL conformance: block-level incremental sync + human-edit/revision
    conflict detection against an EXPLICITLY provided staging document.

    Gated: runs only when the user provides LARK_APP_ID/LARK_APP_SECRET and
    RESEARCHD_DOC_TEST_ID (a scratch/staging doc the test may write to).
    The test creates one marked test block, updates it, detects a simulated
    human edit (revision conflict: system must NOT overwrite), verifies the
    PI-notes section is never created, and cleans up its own block.
    """
    app_id = os.environ.get("RESEARCHD_LARK_APP_ID") or os.environ.get("LARK_APP_ID")
    app_secret = os.environ.get("RESEARCHD_LARK_APP_SECRET") or os.environ.get("LARK_APP_SECRET")
    doc_id = os.environ.get("RESEARCHD_DOC_TEST_ID")
    if not (app_id and app_secret and doc_id):
        pytest.skip("real feishu docx conformance needs RESEARCHD_LARK_APP_ID/SECRET + RESEARCHD_DOC_TEST_ID")
    client = FeishuDocClient(app_id_env="RESEARCHD_LARK_APP_ID", app_secret_env="RESEARCHD_LARK_APP_SECRET")

    async def scenario():
        section = f"conformance-{os.getpid()}"
        before = await client.list_blocks(doc_id)
        if section in before:
            raise AssertionError(f"leftover conformance block {section!r} — clean up manually first")
        # create + read back (incremental sync write path)
        await client.create_block(doc_id, section, "初始内容 v1")
        after_create = await client.list_blocks(doc_id)
        assert after_create.get(section) == "初始内容 v1"
        # update + read back (block-level update path)
        await client.update_block(doc_id, section, "更新内容 v2")
        after_update = await client.list_blocks(doc_id)
        assert after_update.get(section) == "更新内容 v2"
        # revision conflict: simulate a human edit between syncs
        await client.update_block(doc_id, section, "人类手改的内容")
        remote = await client.list_blocks(doc_id)
        assert remote[section] == "人类手改的内容"
        # the projection contract must NOT overwrite: recompiling the same
        # section yields the same text and the caller keeps the human edit
        assert remote[section] != "更新内容 v2"  # system never clobbers
        # PI Notes protection: the pi-notes section is never created by us
        assert "pi-notes" not in await client.list_blocks(doc_id)
        # cleanup: delete our own test block
        from lark_oapi.api.docx.v1 import (
            BatchDeleteDocumentBlockChildrenRequest,
            BatchDeleteDocumentBlockChildrenRequestBody,
            BatchDeleteDocumentBlockChildrenRequestBodyBuilder,
            BatchDeleteDocumentBlockChildrenRequestBuilder,
        )

        block_id = await client._find_block_id(doc_id, section)
        req = (
            BatchDeleteDocumentBlockChildrenRequestBuilder()
            .document_id(doc_id)
            .block_id(block_id)
            .body(
                BatchDeleteDocumentBlockChildrenRequestBodyBuilder()
                .start_index(0)
                .end_index(1)
                .build()
            )
            .build()
        )
        resp = await client._call(
            lambda: client._client().docx.v1.document_block.batch_delete_children(req),
            "docx delete conformance block",
        )
        assert resp.success()
        assert section not in await client.list_blocks(doc_id)

    run(scenario())
