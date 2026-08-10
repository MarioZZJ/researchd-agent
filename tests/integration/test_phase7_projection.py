"""Phase 7 tests: document projection (incremental sync, PI Notes protection,
human patch detection, outbox delivery)."""

from __future__ import annotations

import asyncio

from researchd.domain.base import Actor
from researchd.domain.evidence import Evidence
from researchd.executors.fake import FakeDeliveryPort
from researchd.persistence.repositories import EvidenceRepo
from researchd.persistence.transaction import UnitOfWork
from researchd.projections.feishu_doc import (
    FakeDocPlatform,
    PI_NOTES_SECTION,
    SECTION_ORDER,
    compile_sections,
    section_hash,
    sync_document,
)
from researchd.scheduler.outbox_sender import OutboxSender


def add_evidence(factory, evidence_id="E-1", project_id="P-1"):
    with UnitOfWork(factory) as uow:
        EvidenceRepo(uow.session).save(
            Evidence(
                evidence_id=evidence_id, project_id=project_id, type="literature",
                status="VERIFIED", statement="s", literature={"source_id": "doi:1"},
            )
        )
        uow.commit()


def sync_and_flush(factory, platform, *, project_id="P-1", document_id="doc-1"):
    """sync_document (enqueue) + sender flush (remote writes) + state write-back."""
    with UnitOfWork(factory) as uow:
        result = asyncio.run(
            sync_document(uow.session, platform, project_id=project_id,
                          document_id=document_id, actor=Actor(type="system"))
        )
    stats = asyncio.run(OutboxSender(factory, FakeDeliveryPort(), doc_platform=platform).send_pending())
    return result, stats


def test_compile_sections_deterministic(factory):
    add_evidence(factory)
    with UnitOfWork(factory) as uow:
        s1 = compile_sections(uow.session, "P-1")
        s2 = compile_sections(uow.session, "P-1")
    assert s1 == s2  # deterministic
    assert set(s1.keys()) == set(SECTION_ORDER)
    assert s1[PI_NOTES_SECTION].owner == "pi"


def test_sync_creates_all_blocks_once(factory):
    add_evidence(factory)
    platform = FakeDocPlatform()
    result, stats = sync_and_flush(factory, platform)
    # system-owned sections written; PI Notes protected (never written)
    assert set(result.updated) == set(SECTION_ORDER) - {PI_NOTES_SECTION}
    assert PI_NOTES_SECTION in result.protected
    assert PI_NOTES_SECTION not in platform.blocks.get("doc-1", {})
    creates = [c for c in platform.calls if c[0] == "create"]
    assert len(creates) == len(SECTION_ORDER) - 1
    assert stats["sent"] == len(SECTION_ORDER) - 1


def test_sync_is_incremental(factory):
    add_evidence(factory)
    platform = FakeDocPlatform()
    sync_and_flush(factory, platform)
    # second sync with unchanged state: nothing enqueued, nothing written
    result, stats = sync_and_flush(factory, platform)
    assert result.updated == []
    assert set(result.unchanged) == set(SECTION_ORDER) - {PI_NOTES_SECTION}
    assert stats["sent"] == 0
    n_writes = len(platform.calls)
    # third sync after state change: ONLY the changed block is rewritten
    add_evidence(factory, evidence_id="E-2")
    result, stats = sync_and_flush(factory, platform)
    assert "evidence" in result.updated
    assert len(result.updated) == 1
    assert result.unchanged == ["status", "claims", "decisions", "milestones"]
    # exactly one update call for the evidence block
    updates = [c for c in platform.calls if c[0] == "update"]
    assert len(updates) == 1
    assert len(platform.calls) == n_writes + 1


def test_pi_notes_never_overwritten(factory):
    add_evidence(factory)
    platform = FakeDocPlatform()
    sync_and_flush(factory, platform)
    # PI edits their notes manually
    platform.simulate_human_edit("doc-1", PI_NOTES_SECTION, "## PI Notes\n我的批注")
    result, _ = sync_and_flush(factory, platform)
    assert PI_NOTES_SECTION in result.protected
    assert platform.blocks["doc-1"][PI_NOTES_SECTION] == "## PI Notes\n我的批注"  # untouched


def test_human_patch_detected_and_kept(factory):
    add_evidence(factory)
    platform = FakeDocPlatform()
    sync_and_flush(factory, platform)
    # a human edits a system-owned section while local state is unchanged
    platform.simulate_human_edit("doc-1", "evidence", "## 已验证证据\n（人工改写）")
    result, stats = sync_and_flush(factory, platform)
    assert "evidence" in result.human_patches
    assert platform.blocks["doc-1"]["evidence"] == "## 已验证证据\n（人工改写）"  # NOT overwritten
    assert stats["sent"] == 0  # nothing written
    # the human patch is recorded as an event (exactly once)
    from sqlalchemy import select

    from researchd.persistence.models import EventRow

    with UnitOfWork(factory) as uow:
        events = uow.session.execute(select(EventRow)).scalars().all()
        patches = [e for e in events if e.event_type == "projection.human_patch"]
        assert len(patches) == 1
    # repeated ticks with the SAME patch do not flood events
    for _ in range(3):
        sync_and_flush(factory, platform)
    with UnitOfWork(factory) as uow:
        events = uow.session.execute(select(EventRow)).scalars().all()
        patches = [e for e in events if e.event_type == "projection.human_patch"]
        assert len(patches) == 1


def test_human_patch_then_state_change_overwrites(factory):
    """A later REAL state change in a patched section overwrites the patch
    (the system's content is authoritative once it actually changes)."""
    add_evidence(factory)
    platform = FakeDocPlatform()
    sync_and_flush(factory, platform)
    platform.simulate_human_edit("doc-1", "evidence", "## 已验证证据\n（人工改写）")
    sync_and_flush(factory, platform)
    add_evidence(factory, evidence_id="E-2")
    result, stats = sync_and_flush(factory, platform)
    assert "evidence" in result.updated  # state changed -> system rewrites
    assert stats["sent"] == 1
    assert "E-2" in platform.blocks["doc-1"]["evidence"]


def test_content_cycle_a_b_a_enqueues_twice(factory):
    """A->B->A content cycles produce distinct outbox rows/keys and two real
    updates, without idempotency collisions."""
    add_evidence(factory)
    platform = FakeDocPlatform()
    sync_and_flush(factory, platform)  # A
    add_evidence(factory, evidence_id="E-2")
    sync_and_flush(factory, platform)  # B
    # remove E-2 -> content back to A
    from sqlalchemy import delete

    from researchd.persistence.models import EvidenceRow

    with UnitOfWork(factory) as uow:
        uow.session.execute(delete(EvidenceRow).where(EvidenceRow.evidence_id == "E-2"))
        uow.commit()
    result, stats = sync_and_flush(factory, platform)  # A again
    assert "evidence" in result.updated
    assert stats["sent"] == 1
    assert platform.blocks["doc-1"]["evidence"] == "## 已验证证据\n- E-1: s"
    # no duplicate events / integrity errors
    from sqlalchemy import select

    from researchd.persistence.models import EventRow

    with UnitOfWork(factory) as uow:
        updated = [
            e for e in uow.session.execute(select(EventRow)).scalars().all()
            if e.event_type == "projection.updated" and e.payload_json["section_key"] == "evidence"
        ]
    assert len(updated) == 3  # A, B, A — three real updates, three events


def test_first_takeover_adopts_matching_block(factory):
    """Remote already equals compiled content -> adopted, no write."""
    add_evidence(factory)
    platform = FakeDocPlatform()
    platform.blocks["doc-1"] = {"evidence": "## 已验证证据\n- E-1: s"}
    result, stats = sync_and_flush(factory, platform)
    assert "evidence" in result.adopted
    # adopt converges through the outbox without a remote write; other
    # sections are created
    assert stats["sent"] == len(SECTION_ORDER) - 1  # adopt + 4 creates; PI Notes untouched
    assert [c[0] for c in platform.calls].count("create") == len(SECTION_ORDER) - 2
    # adopt is stable: next sync is unchanged
    result2, _ = sync_and_flush(factory, platform)
    assert "evidence" in result2.unchanged


def test_first_takeover_unknown_origin_never_overwritten(factory):
    """Remote block with unknown origin (no state row) is treated as
    human-owned and never overwritten."""
    add_evidence(factory)
    platform = FakeDocPlatform()
    platform.blocks["doc-1"] = {"evidence": "## 已验证证据\n（某人手写的）"}
    result, stats = sync_and_flush(factory, platform)
    assert "evidence" in result.human_patches
    assert platform.blocks["doc-1"]["evidence"] == "## 已验证证据\n（某人手写的）"
    assert stats["sent"] == len(SECTION_ORDER) - 2


def test_remote_block_deleted_is_rewritten(factory):
    add_evidence(factory)
    platform = FakeDocPlatform()
    sync_and_flush(factory, platform)
    del platform.blocks["doc-1"]["evidence"]  # someone deleted the block
    result, stats = sync_and_flush(factory, platform)
    assert "evidence" in result.updated  # system content is current -> rewrite
    assert stats["sent"] == 1
    assert "E-1" in platform.blocks["doc-1"]["evidence"]


def test_platform_outage_backoffs_and_recovers(factory):
    """Remote write failure -> outbox backoff, state NOT claimed; recovery
    after the outage converges."""
    add_evidence(factory)
    platform = FakeDocPlatform()
    platform.fail_writes = True
    with UnitOfWork(factory) as uow:
        result = asyncio.run(
            sync_document(uow.session, platform, project_id="P-1", document_id="doc-1")
        )
    stats = asyncio.run(OutboxSender(factory, FakeDeliveryPort(), doc_platform=platform).send_pending())
    assert stats["failed"] >= 1 and stats["sent"] == 0
    # state must NOT be claimed (outbox row stays pending; nothing in platform)
    assert "evidence" not in platform.blocks.get("doc-1", {})
    # outage clears -> retry converges (backoff expires first)
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from researchd.persistence.models import OutboxRow

    platform.fail_writes = False
    with UnitOfWork(factory) as uow:
        uow.session.execute(
            update(OutboxRow).values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        uow.commit()
    stats2 = asyncio.run(OutboxSender(factory, FakeDeliveryPort(), doc_platform=platform).send_pending())
    assert stats2["sent"] >= 1
    assert "E-1" in platform.blocks["doc-1"]["evidence"]
    # and the next sync sees everything unchanged
    result2, _ = sync_and_flush(factory, platform)
    assert result2.updated == []


def test_scheduler_tick_runs_projection(factory):
    """A project with feishu_document_id configured gets its document synced
    on every tick; unchanged state produces zero projection writes."""
    from researchd.config import Settings
    from researchd.domain.project import Project
    from researchd.executors.fake import FakeExecutor
    from researchd.persistence.repositories import ProjectRepo
    from researchd.scheduler.loop import SchedulerLoop

    with UnitOfWork(factory) as uow:
        project = ProjectRepo(uow.session).get_by_project_id("P-1")
        if project is None:
            project = Project(project_id="P-1", name="proj")
        project.metadata = {"feishu_document_id": "doc-9"}
        ProjectRepo(uow.session).save(project)
        uow.commit()

    loop = SchedulerLoop(Settings(), factory, FakeExecutor(), FakeDeliveryPort(), max_parallel=2)
    platform = FakeDocPlatform()
    loop._doc_platform_instance = platform  # test-only injection (prod: never)

    async def run():
        s1 = await loop.tick()            # enqueue doc_block rows
        await loop.sender.send_pending()  # flush to the (fake) platform
        blocks = await platform.list_blocks("doc-9")
        s2 = await loop.tick()            # unchanged -> nothing enqueued
        await loop.sender.send_pending()
        return s1, s2, blocks

    s1, s2, blocks = asyncio.run(run())
    assert s1["projection"] >= 1  # first tick enqueued the sections
    assert len(blocks) == len(SECTION_ORDER) - 1  # PI Notes never written
    assert s2["projection"] == 0  # unchanged state -> no writes


def test_toctou_human_edit_after_enqueue_skipped(factory):
    """A human edits the remote block AFTER it was queued but BEFORE the
    sender delivers: the write is skipped (HumanPatchSkip), the row completes
    without state write-back, and the block keeps the human content."""
    add_evidence(factory)
    platform = FakeDocPlatform()
    sync_and_flush(factory, platform)  # synced: evidence == system content
    # state changes (new evidence) -> a doc_block row is enqueued
    add_evidence(factory, evidence_id="E-2")
    with UnitOfWork(factory) as uow:
        result = asyncio.run(
            sync_document(uow.session, platform, project_id="P-1", document_id="doc-1")
        )
    assert "evidence" in result.updated
    # BEFORE delivery, a human edits the block
    platform.simulate_human_edit("doc-1", "evidence", "## 已验证证据\n（人工改写）")
    stats = asyncio.run(OutboxSender(factory, FakeDeliveryPort(), doc_platform=platform).send_pending())
    assert stats["skipped"] == 1
    assert stats["sent"] == 0
    # human content preserved; state NOT claimed (row completed without it)
    assert platform.blocks["doc-1"]["evidence"] == "## 已验证证据\n（人工改写）"
    from sqlalchemy import select

    from researchd.persistence.models import ProjectionStateRow

    with UnitOfWork(factory) as uow:
        st = uow.session.execute(
            select(ProjectionStateRow).where(
                ProjectionStateRow.project_id == "P-1",
                ProjectionStateRow.section_key == "evidence",
            )
        ).scalar_one_or_none()
    assert st is not None and st.content_hash != section_hash("## 已验证证据\n（人工改写）")


def test_skip_prevents_reenqueue_until_content_changes(factory):
    """After a HumanPatchSkip, subsequent syncs with unchanged content do NOT
    re-enqueue (no infinite skip loop); a content change yields a new
    generation and a legitimate overwrite."""
    add_evidence(factory)
    platform = FakeDocPlatform()
    sync_and_flush(factory, platform)
    # content changes -> row enqueued -> human edits before delivery -> skip
    add_evidence(factory, evidence_id="E-2")
    with UnitOfWork(factory) as uow:
        asyncio.run(sync_document(uow.session, platform, project_id="P-1", document_id="doc-1"))
    platform.simulate_human_edit("doc-1", "evidence", "## 已验证证据\n（人工改写）")
    stats = asyncio.run(OutboxSender(factory, FakeDeliveryPort(), doc_platform=platform).send_pending())
    assert stats["skipped"] == 1
    n_calls = len(platform.calls)
    # unchanged content: repeated sync+flush must NOT re-enqueue or rewrite
    for _ in range(3):
        result, stats = sync_and_flush(factory, platform)
        assert "evidence" not in result.updated
        assert stats["sent"] == 0
    assert len(platform.calls) == n_calls
    assert platform.blocks["doc-1"]["evidence"] == "## 已验证证据\n（人工改写）"
    # a REAL content change unblocks the section (new generation, overwrite)
    add_evidence(factory, evidence_id="E-3")
    result, stats = sync_and_flush(factory, platform)
    assert "evidence" in result.updated
    assert stats["sent"] == 1
    assert "E-3" in platform.blocks["doc-1"]["evidence"]


def test_adopt_never_writes_remote(factory):
    """First-takeover adopt converges through the outbox WITHOUT any remote
    platform write."""
    add_evidence(factory)
    platform = FakeDocPlatform()
    platform.blocks["doc-1"] = {"evidence": "## 已验证证据\n- E-1: s"}
    result, stats = sync_and_flush(factory, platform)
    assert "evidence" in result.adopted
    # the adopted section produced no create/update call at all
    assert [c for c in platform.calls if c[1] == "doc-1" and c[2] == "evidence"] == []
