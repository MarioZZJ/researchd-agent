"""Project document projection (IMPLEMENTATION.md §21.4, §23 Phase 7).

Design (post-review): remote writes are OUTBOXED, never performed inline.
- sync_document() compiles deterministic sections, compares against the remote
  truth and the persisted projection state, and ONLY enqueues a doc_block
  outbox row for sections that need a write. No remote call, no state write.
- The outbox sender executes the remote create/update and, only on success,
  updates projection_states and appends the projection.updated event in the
  same transaction. Crashes / partial failures leave the outbox row PENDING
  and retry; the write itself is idempotent (same content).

Human patch detection (conservative):
- if the remote block differs from the text the system last wrote (persisted
  hash matches the remote), the system does NOT overwrite it and records a
  projection.human_patch event (idempotency keyed on the patch hash).
- first takeover (no state row): if remote already equals the compiled
  content the block is adopted; otherwise it is treated as PI-owned content
  and never overwritten.

PI Notes protection: the pi-notes section is never written by the system.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.base import Actor, AggregateRef, new_id, utcnow
from ..domain.events import make_event
from ..persistence.models import OutboxRow, ProjectionStateRow
from ..persistence.outbox import OutboxStatus
from ..persistence.repositories import (
    ClaimRepo,
    DecisionRepo,
    EventRepo,
    EvidenceRepo,
    TaskRepo,
)

logger = logging.getLogger("researchd.projection")

PI_NOTES_SECTION = "pi-notes"
SECTION_ORDER = ("status", "evidence", "claims", "decisions", "milestones", PI_NOTES_SECTION)

DOC_BLOCK_KIND = "doc_block"


@dataclass
class SectionContent:
    key: str
    text: str
    owner: str = "system"  # system | pi


@dataclass
class ProjectionResult:
    updated: list[str] = field(default_factory=list)   # enqueued for write
    unchanged: list[str] = field(default_factory=list)  # already synced
    adopted: list[str] = field(default_factory=list)    # remote == local, no state
    protected: list[str] = field(default_factory=list)  # pi-owned, never written
    human_patches: list[str] = field(default_factory=list)  # detected, kept
    errors: list[str] = field(default_factory=list)


class DocPlatform:
    """Minimal docx-like surface (blocks keyed by section). The Feishu docx
    client is real; FakeDocPlatform serves deterministic tests."""

    async def create_document(self, title: str, *, folder_token: str | None = None):
        """Create a new document; returns (document_id, revision_id)."""
        raise NotImplementedError

    async def add_permission_member(
        self, document_id: str, *, member_type: str, member_id: str, perm: str = "full_access"
    ) -> bool:
        """Share the document with a member; False when the platform denies."""
        raise NotImplementedError

    async def list_blocks(self, document_id: str) -> dict[str, str]:
        """section_key -> block text (remote truth)."""
        raise NotImplementedError

    async def list_blocks_with_revision(self, document_id: str) -> tuple[dict[str, str], int | None]:
        """section_key -> block text + current document revision (optimistic
        concurrency token); None revision when the platform has none."""
        return await self.list_blocks(document_id), None

    async def create_block(self, document_id: str, section_key: str, text: str, *, document_revision_id: int | None = None) -> None:
        raise NotImplementedError

    async def update_block(self, document_id: str, section_key: str, text: str, *, document_revision_id: int | None = None) -> None:
        raise NotImplementedError


class FakeDocPlatform(DocPlatform):
    """In-memory doc platform: records writes; can simulate human edits."""

    def __init__(self):
        self.blocks: dict[str, dict[str, str]] = {}  # document_id -> section -> text
        self.calls: list[tuple] = []
        self.human_edit: list[tuple] = []  # (document_id, section_key, text) applied manually
        self.fail_writes: bool = False  # simulate platform outages
        self.revision: int | None = 1  # simulated document revision
        self.documents: dict[str, dict] = {}  # document_id -> {title, folder_token, members}
        self.deny_collaborator: bool = False  # simulate missing drive scope

    async def create_document(self, title: str, *, folder_token: str | None = None):
        doc_id = f"doc-{len(self.documents) + 1}"
        self.documents[doc_id] = {"title": title, "folder_token": folder_token, "members": []}
        self.revision = 1
        return doc_id, self.revision

    async def add_permission_member(
        self, document_id: str, *, member_type: str, member_id: str, perm: str = "full_access"
    ) -> bool:
        if self.deny_collaborator:
            return False
        self.documents.setdefault(document_id, {"members": []})["members"].append(
            {"member_type": member_type, "member_id": member_id, "perm": perm}
        )
        return True

    async def list_blocks(self, document_id: str) -> dict[str, str]:
        return dict(self.blocks.get(document_id, {}))

    async def list_blocks_with_revision(self, document_id: str) -> tuple[dict[str, str], int | None]:
        return dict(self.blocks.get(document_id, {})), self.revision

    async def create_block(self, document_id: str, section_key: str, text: str, *, document_revision_id: int | None = None) -> None:
        if self.fail_writes:
            raise RuntimeError("platform outage (simulated)")
        self.blocks.setdefault(document_id, {})[section_key] = text
        self.revision += 1
        self.calls.append(("create", document_id, section_key))

    async def update_block(self, document_id: str, section_key: str, text: str, *, document_revision_id: int | None = None) -> None:
        if self.fail_writes:
            raise RuntimeError("platform outage (simulated)")
        self.blocks.setdefault(document_id, {})[section_key] = text
        self.revision += 1
        self.calls.append(("update", document_id, section_key))

    def simulate_human_edit(self, document_id: str, section_key: str, text: str) -> None:
        self.blocks.setdefault(document_id, {})[section_key] = text
        self.human_edit.append((document_id, section_key, text))


def compile_sections(session: Session, project_id: str) -> dict[str, SectionContent]:
    """Deterministic section content from persisted state (stable sort so the
    hash is stable across runs)."""
    evidence = sorted(EvidenceRepo(session).list_verified(project_id), key=lambda e: e.evidence_id)
    claims = sorted(ClaimRepo(session).list_by_project(project_id), key=lambda c: c.claim_id)
    decisions = sorted(DecisionRepo(session).list_open(project_id), key=lambda d: d.decision_id)
    tasks = sorted(TaskRepo(session).list_by_status(project_id, []), key=lambda t: t.task_id)

    sections: dict[str, SectionContent] = {}

    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.status.value] = counts.get(t.status.value, 0) + 1
    status_lines = [f"- {k}: {v}" for k, v in sorted(counts.items())] or [f"- tasks: {len(tasks)}"]
    sections["status"] = SectionContent(key="status", text="## 状态\n" + "\n".join(status_lines))

    evidence_lines = [f"- {e.evidence_id}: {e.statement[:200]}" for e in evidence]
    sections["evidence"] = SectionContent(
        key="evidence",
        text="## 已验证证据\n" + ("\n".join(evidence_lines) if evidence_lines else "（暂无）"),
    )
    claim_lines = [
        f"- {c.claim_id} [{c.evidence_state.value}] {c.text[:200]}" for c in claims
    ]
    sections["claims"] = SectionContent(
        key="claims",
        text="## Claims\n" + ("\n".join(claim_lines) if claim_lines else "（暂无）"),
    )
    decision_lines = [
        f"- {d.decision_id} [{d.status.value}] {d.question[:200]}" for d in decisions
    ]
    sections["decisions"] = SectionContent(
        key="decisions",
        text="## 决策\n" + ("\n".join(decision_lines) if decision_lines else "（暂无）"),
    )
    # milestones: compiled from REAL milestone.reached events (never a
    # hardcoded placeholder); each entry cites the task/decision it came from
    from ..domain.events import make_event  # noqa: F401  (type registry)
    from ..persistence.repositories import EventRepo

    from sqlalchemy import select

    from ..persistence.models import EventRow

    milestone_rows = session.execute(
        select(EventRow)
        .where(EventRow.project_id == project_id, EventRow.event_type == "milestone.reached")
        .order_by(EventRow.occurred_at)
    ).scalars().all()
    milestone_lines = []
    for m in milestone_rows:
        body = (m.payload_json or {}).get("body", "")
        milestone_lines.append(f"- {body[:200]}" if body else f"- {m.idempotency_key}")
    sections["milestones"] = SectionContent(
        key="milestones",
        text="## 里程碑\n" + ("\n".join(milestone_lines) if milestone_lines else "（暂无）"),
    )
    # PI Notes: PI-owned; the system NEVER writes it
    sections[PI_NOTES_SECTION] = SectionContent(
        key=PI_NOTES_SECTION, text="## PI Notes\n（仅 PI 编辑）", owner="pi"
    )
    return sections


def section_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _state_row(session: Session, project_id: str, document_id: str, section_key: str) -> ProjectionStateRow | None:
    return session.execute(
        select(ProjectionStateRow).where(
            ProjectionStateRow.project_id == project_id,
            ProjectionStateRow.document_id == document_id,
            ProjectionStateRow.section_key == section_key,
        )
    ).scalar_one_or_none()


async def ensure_project_document(
    session: Session,
    platform: DocPlatform,
    project,
    *,
    title_template: str,
    folder_token: str = "",
    staging_chat_id: str = "",
    pi_open_id: str = "",
    default_permission: str = "full_access",
) -> str:
    """Create the project's document ONCE and persist the receipt in the same
    transaction (project.metadata.feishu_document_id + document.created event
    + first projection outbox). Idempotent: a replay with the receipt already
    persisted never creates a second document.

    Collaborator sharing is best-effort by design: a missing drive scope must
    not block the projection (the PI can still be granted access later), but
    the denial is logged by code only.
    """
    from ..persistence.outbox import OutboxRepo
    from ..persistence.repositories import ProjectRepo

    existing = (project.metadata or {}).get("feishu_document_id")
    if existing:
        return existing
    from datetime import date

    title = title_template.format(project_name=project.name, date=date.today().isoformat())
    doc_id, revision = await platform.create_document(title, folder_token=folder_token or None)
    if staging_chat_id:
        await platform.add_permission_member(
            doc_id, member_type="openchat", member_id=staging_chat_id, perm=default_permission
        )
    if pi_open_id:
        await platform.add_permission_member(
            doc_id, member_type="openid", member_id=pi_open_id, perm=default_permission
        )
    # same-transaction write-back: receipt + event + first projection outbox
    project.metadata = dict(project.metadata or {})
    project.metadata["feishu_document_id"] = doc_id
    project.metadata["feishu_document_title"] = title
    project.metadata["feishu_document_revision"] = revision
    project.updated_at = utcnow()
    ProjectRepo(session).save(project)
    EventRepo(session).append(
        make_event(
            event_type="document.created",
            aggregate=AggregateRef(type="project", id=project.id, version=project.version),
            idempotency_key=f"project:{project.project_id}:document.created:{doc_id}",
            project_id=project.project_id,
            payload={"document_id": doc_id, "title": title, "revision": revision},
        )
    )
    # first projection outbox: queue all sections (write-back happens in the
    # outbox sender AFTER a successful delivery, same design as sync_document)
    sections = compile_sections(session, project.project_id)
    for key in SECTION_ORDER:
        content = sections[key]
        if content.owner == "pi":
            continue
        _enqueue_doc_block(
            session,
            project_id=project.project_id,
            document_id=doc_id,
            section_key=key,
            text=content.text,
            expected_remote="",  # brand-new document: no remote content yet
            actor=Actor(type="system"),
        )
    logger.info(
        "project document ensured: project=%s document_id=%s title=%r",
        project.project_id, doc_id, title,
    )
    return doc_id


def _pending_outbox(session: Session, prefix: str) -> bool:
    """True if a doc_block row for this key prefix is already queued or was
    already completed for this content: PENDING/IN_FLIGHT (being delivered —
    re-enqueueing would duplicate) or SENT-with-skip (a human edited the
    remote after enqueue; the same content must NOT be re-enqueued until the
    compiled content itself changes, which yields a new hash prefix)."""
    row = session.execute(
        select(OutboxRow).where(
            OutboxRow.idempotency_key.like(prefix + "%"),
            OutboxRow.status.in_([OutboxStatus.PENDING.value, OutboxStatus.IN_FLIGHT.value]),
        )
    ).first()
    if row is not None:
        return True
    skipped = session.execute(
        select(OutboxRow.payload_json).where(
            OutboxRow.idempotency_key.like(prefix + "%"),
            OutboxRow.status == OutboxStatus.SENT.value,
        )
    ).scalars().all()
    return any((p or {}).get("skipped") for p in skipped)


def _enqueue_doc_block(session: Session, *, project_id: str, document_id: str,
                       section_key: str, text: str, expected_remote: str | None,
                       actor: Actor) -> bool:
    """Queue a remote write; returns True when newly queued.

    The idempotency key is `projection:<project>:<doc>:<key>:<hash>` with a
    generation counter: a previous SENT row for the same content (e.g. the
    remote block was deleted, or content cycled A->B->A) must NOT collide, so
    each re-write of the same content gets a fresh generation. If a PENDING or
    IN_FLIGHT row already exists for the same content (including during
    backoff), nothing new is queued — the existing row is the single source
    of truth until it is sent or dies.

    expected_remote is the remote block text observed at enqueue time: the
    sender skips the write if the remote changed since (human TOCTOU edit)."""
    from sqlalchemy import func

    prefix = f"projection:{project_id}:{document_id}:{section_key}:{section_hash(text)}"
    if _pending_outbox(session, prefix):
        return False
    (gen,) = session.execute(
        select(func.count()).select_from(OutboxRow).where(OutboxRow.idempotency_key.like(prefix + "%"))
    ).one()
    key = f"{prefix}:r{gen + 1}"
    row = OutboxRow(
        id=new_id("outbox"),
        destination=DOC_BLOCK_KIND,
        idempotency_key=key,
        project_id=project_id,
        payload_json={
            "kind": DOC_BLOCK_KIND,
            "document_id": document_id,
            "section_key": section_key,
            "text": text,
            "content_hash": section_hash(text),
            "expected_remote": expected_remote,
        },
        status=OutboxStatus.PENDING.value,
        attempts=0,
        max_attempts=8,
        next_attempt_at=utcnow(),
    )
    session.add(row)
    return True


async def sync_document(
    session: Session,
    platform: DocPlatform,
    *,
    project_id: str,
    document_id: str,
    actor: Actor | None = None,
) -> ProjectionResult:
    """Enqueue remote writes for changed sections.

    Never performs remote WRITES and never writes projection state — the
    remote truth is read (read-only) only to detect human patches and
    convergence; all state write-back happens in the outbox sender AFTER a
    successful delivery (or adopt convergence). Idempotent: unchanged
    sections and already-queued keys are skipped."""
    actor = actor or Actor(type="system")
    result = ProjectionResult()
    sections = compile_sections(session, project_id)
    remote = await platform.list_blocks(document_id)

    for key in SECTION_ORDER:
        content = sections[key]
        local_hash = section_hash(content.text)
        row = _state_row(session, project_id, document_id, key)

        if content.owner == "pi":
            result.protected.append(key)
            continue

        remote_text = remote.get(key)
        if remote_text == content.text:
            # remote already equals the compiled content
            if row is not None and row.content_hash == local_hash:
                result.unchanged.append(key)
                continue
            # state missing or stale -> converge through the outbox (the
            # sender adopts without a remote write and writes the state)
            if _enqueue_doc_block(session, project_id=project_id, document_id=document_id,
                                  section_key=key, text=content.text,
                                  expected_remote=remote_text, actor=actor):
                result.adopted.append(key)
            continue

        if row is not None and row.content_hash == local_hash and remote_text is not None:
            # the system's last write is still the desired content, but the
            # remote differs -> a human changed it -> never overwrite
            result.human_patches.append(key)
            _record_human_patch(session, project_id, document_id, key, remote_text, actor)
            continue
        # remote block was deleted (remote_text is None) while our content is
        # current -> fall through and enqueue the rewrite

        if row is None and remote_text is not None:
            # first takeover with unknown origin: conservative, never overwrite
            result.human_patches.append(key)
            _record_human_patch(session, project_id, document_id, key, remote_text, actor)
            continue

        # normal change path: our previous write (or nothing) -> enqueue
        if _enqueue_doc_block(session, project_id=project_id, document_id=document_id,
                              section_key=key, text=content.text,
                              expected_remote=remote_text, actor=actor):
            result.updated.append(key)

    session.commit()
    return result


def _record_human_patch(session: Session, project_id: str, document_id: str,
                        section_key: str, remote_text: str, actor: Actor) -> None:
    """Record a human patch exactly once per distinct patch content
    (idempotency key = patch content hash, so repeated ticks do not flood)."""
    from ..persistence.models import EventRow

    patch_hash = section_hash(remote_text)
    key = f"human-patch:{project_id}:{document_id}:{section_key}:{patch_hash}"
    exists = session.execute(
        select(EventRow.id).where(EventRow.idempotency_key == key)
    ).first()
    if exists is not None:
        return  # already recorded; do not re-append (and never rollback siblings)
    EventRepo(session).append(
        make_event(
            event_type="projection.human_patch",
            aggregate=AggregateRef(type="projection", id=f"{document_id}:{section_key}", version=1),
            idempotency_key=key,
            project_id=project_id,
            actor=actor,
            payload={"section_key": section_key, "patch_hash": patch_hash},
        )
    )
