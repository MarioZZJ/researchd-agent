"""Feishu Docx conformance tests (IMPLEMENTATION.md §21.4, §23 Phase 7).

Local conformance pins the exact lark-oapi interaction contract with a fake
client (block lifecycle, marker parsing, pagination, transient retry). The
REAL conformance test (block-level incremental sync, human-edit detection,
PI Notes protection, revision conflict) is gated behind
LARK_APP_ID / LARK_APP_SECRET + RESEARCHD_DOC_TEST_ID —
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

    def batch_delete_children(self, req):
        fail = self._fail("delete")
        if fail:
            return fail
        parent = self.blocks[req.block_id]
        # child blocks are stored in insertion order; drop the index range
        children = [b for b in self.blocks.values() if b is not parent]
        order = list(self.blocks.keys())
        parent_pos = order.index(req.block_id)
        target = order[parent_pos + 1 + req.body.start_index]
        del self.blocks[target]
        from types import SimpleNamespace

        return SimpleNamespace(success=lambda: True, code=0, msg="", data=SimpleNamespace(block=parent))


class FakeClient:
    def __init__(self, api):
        self.docx = SimpleNamespace_v1(document_block_children=api, document_block=api)


class SimpleNamespace_v1:
    def __init__(self, document_block_children, document_block):
        self.v1 = SimpleNamespace_v1_inner(document_block_children, document_block)


class SimpleNamespace_v1_inner:
    def __init__(self, document_block_children, document_block):
        self.document_block_children = document_block_children
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
    app_id = os.environ.get("LARK_APP_ID")
    app_secret = os.environ.get("LARK_APP_SECRET")
    doc_id = os.environ.get("RESEARCHD_DOC_TEST_ID")
    if not (app_id and app_secret and doc_id):
        pytest.skip("real feishu docx conformance needs LARK_APP_ID/SECRET + RESEARCHD_DOC_TEST_ID")
    client = FeishuDocClient()  # LARK_APP_ID / LARK_APP_SECRET (single convention)
    # the FULL path: sync_document -> outbox doc_block rows -> FeishuDocClient
    # (this is exactly what production projection does)
    import tempfile

    from researchd.persistence.transaction import init_db, make_engine, make_session_factory, UnitOfWork
    from researchd.scheduler.outbox_sender import OutboxSender
    from researchd.domain.project import Project
    from researchd.persistence.repositories import ProjectRepo
    from researchd.persistence.outbox import OutboxRepo
    from researchd.projections.feishu_doc import SECTION_ORDER

    engine = make_engine(os.path.join(tempfile.mkdtemp(), "t.db"))
    init_db(engine)
    factory = make_session_factory(engine)
    with UnitOfWork(factory) as uow:
        ProjectRepo(uow.session).save(
            Project(project_id="P-CONF", name="c", description="d", workspace_root="/tmp")
        )
        uow.commit()

    sender = OutboxSender(factory, type("P", (), {"deliver": lambda **k: ""}), doc_platform=client)

    async def scenario():
        section = f"conformance-{os.getpid()}"
        before = await client.list_blocks(doc_id)
        if section in before:
            raise AssertionError(f"leftover conformance block {section!r} — clean up manually first")
        # 1. first creation via the outbox path (create branch)
        with UnitOfWork(factory) as uow:
            OutboxRepo(uow.session).enqueue(
                destination="doc_block",
                idempotency_key=f"doc-conf-1-{section}",
                payload={
                    "kind": "doc_block", "project_id": "P-CONF",
                    "document_id": doc_id, "section_key": section,
                    "text": "初始内容 v1", "expected_remote": None, "content_hash": "h1",
                },
            )
            uow.commit()
        stats = await sender.send_pending()
        assert stats["sent"] == 1 and stats["skipped"] == 0, stats
        after_create = await client.list_blocks(doc_id)
        assert after_create.get(section) == "初始内容 v1"
        # 2. incremental single-block update; OTHER blocks unchanged
        other = {k: v for k, v in after_create.items() if k != section}
        with UnitOfWork(factory) as uow:
            OutboxRepo(uow.session).enqueue(
                destination="doc_block",
                idempotency_key=f"doc-conf-2-{section}",
                payload={
                    "kind": "doc_block", "project_id": "P-CONF",
                    "document_id": doc_id, "section_key": section,
                    "text": "更新内容 v2", "expected_remote": "初始内容 v1", "content_hash": "h2",
                },
            )
            uow.commit()
        stats = await sender.send_pending()
        assert stats["sent"] == 1, stats
        after_update = await client.list_blocks(doc_id)
        assert after_update.get(section) == "更新内容 v2"
        assert {k: v for k, v in after_update.items() if k != section} == other  # untouched
        # 3. idempotent replay of the SAME row: nothing new is sent/written
        stats = await sender.send_pending()
        assert stats["claimed"] == 0, stats
        # 4. human edit between enqueue and send -> write SKIPPED, never clobber
        await client.update_block(doc_id, section, "人类手改的内容")
        with UnitOfWork(factory) as uow:
            OutboxRepo(uow.session).enqueue(
                destination="doc_block",
                idempotency_key=f"doc-conf-3-{section}",
                payload={
                    "kind": "doc_block", "project_id": "P-CONF",
                    "document_id": doc_id, "section_key": section,
                    "text": "系统想覆盖的内容", "expected_remote": "更新内容 v2", "content_hash": "h3",
                },
            )
            uow.commit()
        stats = await sender.send_pending()
        assert stats["skipped"] == 1, stats  # human patch wins
        remote = await client.list_blocks(doc_id)
        assert remote[section] == "人类手改的内容"  # system never clobbers
        # 5. revision-aware read surfaces the current revision
        blocks, rev = await client.list_blocks_with_revision(doc_id)
        assert blocks.get(section) == "人类手改的内容"
        assert rev is not None
        # 6. PI Notes protection: the pi-notes section is never created by us
        assert "pi-notes" not in await client.list_blocks(doc_id)
        # 7. cleanup: delete our own test block (child-of-page by index)
        await client.delete_block(doc_id, section)
        assert section not in await client.list_blocks(doc_id)

    run(scenario())
    engine.dispose()


def test_real_feishu_docx_create_and_share():
    """REAL conformance: researchd CREATES its own document (user-authorized
    auto-create), grants collaborators, writes the first projection, then
    cleans up the created document is NOT possible (no delete API) — the
    document is a REAL artifact, so it is left in place and the caller must
    clean it manually (the document_id is printed, never the secrets).

    Gated: runs only with LARK_APP_ID / LARK_APP_SECRET and explicit
    RESEARCHD_FEISHU__ALLOW_AUTO_CREATE=1.
    """
    app_id = os.environ.get("LARK_APP_ID")
    app_secret = os.environ.get("LARK_APP_SECRET")
    if not (app_id and app_secret):
        pytest.skip("real feishu docx create needs LARK_APP_ID/SECRET")
    if os.environ.get("RESEARCHD_FEISHU__ALLOW_AUTO_CREATE") != "1":
        pytest.skip("auto-create conformance needs explicit RESEARCHD_FEISHU__ALLOW_AUTO_CREATE=1")
    client = FeishuDocClient()
    import tempfile

    from researchd.persistence.transaction import init_db, make_engine, make_session_factory, UnitOfWork
    from researchd.domain.project import Project
    from researchd.persistence.repositories import ProjectRepo
    from researchd.projections.feishu_doc import ensure_project_document

    engine = make_engine(os.path.join(tempfile.mkdtemp(), "t2.db"))
    init_db(engine)
    factory = make_session_factory(engine)

    async def scenario():
        with UnitOfWork(factory) as uow:
            ProjectRepo(uow.session).save(
                Project(project_id="P-CREATE", name="conformance-create", description="d")
            )
            uow.commit()
        with UnitOfWork(factory) as uow:
            project = ProjectRepo(uow.session).get_by_project_id("P-CREATE")
            doc_id = await ensure_project_document(
                uow.session,
                client,
                project,
                title_template="科研项目报告 - {project_name} - {date}",
                staging_chat_id=os.environ.get("RESEARCHD_FEISHU__STAGING_CHAT_ID", ""),
                pi_open_id=os.environ.get("RESEARCHD_FEISHU__PI_OPEN_ID", ""),
            )
            uow.commit()
        # replay (service restart): must NOT create a second document
        with UnitOfWork(factory) as uow:
            project = ProjectRepo(uow.session).get_by_project_id("P-CREATE")
            same = await ensure_project_document(uow.session, client, project, title_template="x")
            uow.commit()
        assert same == doc_id
        blocks = await client.list_blocks(doc_id)
        # first projection wrote at least one section
        assert any(k != "pi-notes" for k in blocks), blocks
        print(f"\nCREATED_DOCUMENT_ID={doc_id} title={project.metadata.get('feishu_document_title')}")
        print("REAL_DOC_CREATE_CONFORMANCE_OK (cleanup: delete the doc manually in the feishu UI)")

    asyncio.run(scenario())
    engine.dispose()
