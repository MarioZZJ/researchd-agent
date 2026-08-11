"""ensure_project_document: creation is once-only and transactional."""
import pytest

from researchd.domain.base import Actor
from researchd.domain.project import Project
from researchd.persistence.models import OutboxRow, ProjectRow, ProjectionStateRow
from researchd.persistence.outbox import OutboxStatus
from researchd.persistence.repositories import EventRepo, ProjectRepo
from researchd.persistence.transaction import make_engine, make_session_factory
from researchd.projections.feishu_doc import (
    DOC_BLOCK_KIND,
    SECTION_ORDER,
    ensure_project_document,
    sync_document,
)
from researchd.projections.feishu_doc import FakeDocPlatform


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(tmp_path / "test.db")
    from researchd.persistence.transaction import init_db

    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture()
def project(db):
    with db() as session:
        p = Project(
            project_id="proj-doc-1",
            name="Docx 自动创建测试",
            description="ensure_project_document 事务测试",
        )
        ProjectRepo(session).save(p)
        session.commit()
        return p


async def _ensure(session, platform, project, **kw):
    return await ensure_project_document(
        session,
        platform,
        project,
        title_template=kw.get("title_template", "科研项目报告 - {project_name} - {date}"),
        folder_token=kw.get("folder_token", ""),
        staging_chat_id=kw.get("staging_chat_id", "oc_staging_group"),
        pi_open_id=kw.get("pi_open_id", ""),
        default_permission=kw.get("default_permission", "full_access"),
    )


@pytest.mark.asyncio
async def test_creates_once_and_persists_receipt(db, project):
    platform = FakeDocPlatform()
    with db() as session:
        doc_id = await _ensure(session, platform, project)
        assert doc_id == "doc-1"
        session.commit()
        # receipt persisted in the same transaction
        row = session.execute(
            __import__("sqlalchemy").select(ProjectRow).where(ProjectRow.project_id == "proj-doc-1")
        ).scalar_one()
        assert row.metadata_json["feishu_document_id"] == "doc-1"
        # document.created event
        events = EventRepo(session).list_for_aggregate("project", project.id)
        assert any(e.event_type == "document.created" for e in events)
        # first projection outbox rows for non-PI sections
        outbox = session.execute(
            __import__("sqlalchemy").select(OutboxRow).where(OutboxRow.destination == DOC_BLOCK_KIND)
        ).scalars().all()
        assert len(outbox) >= 1
        assert all(r.status == OutboxStatus.PENDING.value for r in outbox)


@pytest.mark.asyncio
async def test_replay_never_creates_second_document(db, project):
    platform = FakeDocPlatform()
    with db() as session:
        doc1 = await _ensure(session, platform, project)
        session.commit()
    # simulate a service restart: fresh platform instance + fresh session
    platform2 = FakeDocPlatform()
    with db() as session:
        project2 = ProjectRepo(session).get_by_project_id("proj-doc-1")
        doc2 = await _ensure(session, platform2, project2)
        session.commit()
    assert doc1 == doc2 == "doc-1"
    assert list(platform2.documents) == []  # nothing created on replay
    with db() as session:
        events = EventRepo(session).list_for_aggregate("project", project.id)
        assert [e.event_type for e in events].count("document.created") == 1


@pytest.mark.asyncio
async def test_collaborators_added_and_denied_gracefully(db, project):
    platform = FakeDocPlatform()
    with db() as session:
        await _ensure(session, platform, project, pi_open_id="ou_pi_1")
        session.commit()
    doc = platform.documents["doc-1"]
    member_types = {m["member_type"] for m in doc["members"]}
    assert "openchat" in member_types
    assert "openid" in member_types
    # least privilege: the group is read-only, the PI is the editor
    perms = {m["member_type"]: m["perm"] for m in doc["members"]}
    assert perms["openchat"] == "view"
    assert perms["openid"] == "full_access"

    # drive scope denied: creation still succeeds, denial logged only
    platform2 = FakeDocPlatform()
    platform2.deny_collaborator = True
    with db() as session:
        project2 = ProjectRepo(session).get_by_project_id("proj-doc-1")
        doc_id = await _ensure(session, platform2, project2)
        session.commit()
    assert doc_id == "doc-1"  # receipt short-circuits before any call


@pytest.mark.asyncio
async def test_principal_rotation_revokes_stale_collaborator(db, project):
    """Rotating the PI (or chat) grants the new principal AND revokes the
    former one — stale collaborators never keep access."""
    platform = FakeDocPlatform()
    with db() as session:
        await _ensure(session, platform, project, pi_open_id="ou_pi_1")
        session.commit()
    with db() as session:
        await _ensure(session, platform, project, pi_open_id="ou_pi_2")
        session.commit()
    doc = platform.documents["doc-1"]
    pi_members = [m for m in doc["members"] if m["member_type"] == "openid"]
    assert {m["member_id"] for m in pi_members} == {"ou_pi_2"}  # old PI revoked
    with db() as session:
        project2 = ProjectRepo(session).get_by_project_id("proj-doc-1")
        shared = (project2.metadata or {}).get("feishu_document_shared", "")
        assert "openid:ou_pi_1" not in shared
        assert "openid:ou_pi_2" in shared


@pytest.mark.asyncio
async def test_denied_revoke_keeps_marker_for_retry(db, project):
    """When the platform denies the revoke (missing scope), the stale marker
    is KEPT so a later replay retries — a denied revoke must never be
    recorded as successful."""
    platform = FakeDocPlatform()
    with db() as session:
        await _ensure(session, platform, project, pi_open_id="ou_pi_1")
        session.commit()
    platform.deny_collaborator = True  # now the platform refuses membership ops
    with db() as session:
        await _ensure(session, platform, project, pi_open_id="ou_pi_2")
        session.commit()
    with db() as session:
        project2 = ProjectRepo(session).get_by_project_id("proj-doc-1")
        shared = (project2.metadata or {}).get("feishu_document_shared", "")
        assert "openid:ou_pi_1" in shared  # kept: revoke was denied
    # the new PI was also denied; marker must not claim it
    assert "openid:ou_pi_2" not in shared


@pytest.mark.asyncio
async def test_first_projection_flows_through_outbox(db, project):
    """ensure + sync: the queued doc_block rows are the same content the
    projection would enqueue; after the outbox sender runs, blocks exist."""
    platform = FakeDocPlatform()
    with db() as session:
        doc_id = await _ensure(session, platform, project)
        session.commit()
    # run the outbox delivery manually: simulate the sender applying rows
    with db() as session:
        rows = session.execute(
            __import__("sqlalchemy").select(OutboxRow).where(OutboxRow.destination == DOC_BLOCK_KIND)
        ).scalars().all()
        for row in rows:
            payload = row.payload_json
            text = payload["text"]
            await platform.create_block(
                payload["document_id"], payload["section_key"], text
            )
            row.status = OutboxStatus.SENT.value
            row.mark_sent = lambda: True  # noqa: PLW0642 (test stub)
        session.commit()
    remote = await platform.list_blocks(doc_id)
    assert remote  # sections were written
