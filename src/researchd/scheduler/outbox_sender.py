"""Outbox sender: claim -> deliver -> mark_sent/release/dead (IMPLEMENTATION.md
§10, §25.2). Delivery is idempotent on the outbox idempotency_key: a crash
after commit-and-send but before mark_sent replays the same key, and the
delivery port deduplicates on it.
"""

from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.base import Actor, AggregateRef, new_id, utcnow
from ..domain.events import make_event
from ..persistence.models import EventRow, ProjectionStateRow
from ..persistence.outbox import OutboxRepo
from ..projections.feishu_client import FeishuDocRevisionConflict
from ..projections.feishu_doc import DOC_BLOCK_KIND, DocPlatform, section_hash

logger = logging.getLogger("researchd.outbox")


class DeliveryPort(Protocol):
    """Outbound delivery surface (cc-connect Delivery API / fake)."""

    async def deliver(
        self,
        *,
        idempotency_key: str,
        kind: str,
        payload: dict,
        attachments: list | None = None,
        project_id: str | None = None,
    ) -> str: ...

    async def update(self, platform_message_id: str, payload: dict) -> None: ...


class HumanPatchSkip(Exception):
    """A human edited the remote block after it was queued; the write is
    skipped (never overwrite human content). The outbox row is completed
    without state write-back."""


class OutboxSender:
    def __init__(self, session_factory, port: DeliveryPort, *, max_per_tick: int = 20,
                 doc_platform: DocPlatform | None = None):
        self.session_factory = session_factory
        self.port = port
        self.doc_platform = doc_platform
        self.max_per_tick = max_per_tick

    async def send_pending(self) -> dict:
        """Deliver due outbox rows. Returns delivery counters for diagnostics."""
        stats = {"claimed": 0, "sent": 0, "failed": 0, "dead": 0, "released": 0, "skipped": 0}
        with self.session_factory() as session:
            repo = OutboxRepo(session)
            rows = repo.pending(limit=self.max_per_tick)
            for row in rows:
                if not repo.claim(row.id, attempts=row.attempts):
                    continue
                # claim() bumps attempts to row.attempts+1 in the DB; every
                # subsequent transition (sent/failed/dead/skip) MUST use this
                # exact generation so a reclaimed row can never be clobbered
                # by a stale claimant (attempts is the fencing token).
                claimed_attempts = row.attempts + 1
                session.commit()
                stats["claimed"] += 1
                try:
                    delivery_id = await self._deliver(row)
                except HumanPatchSkip:
                    with self.session_factory() as s4:
                        r4 = OutboxRepo(s4)
                        row4 = r4.get_by_idempotency_key(row.idempotency_key)
                        if row4 is None:
                            continue
                        r4.record_attempt(row4.id, success=True)
                        if r4.mark_sent(row4.id, attempts=claimed_attempts, delivery_id=""):
                            # persist the skip so the next sync does NOT
                            # re-enqueue the same generation (it would be
                            # skipped again forever); the state hash stays
                            # unchanged until the content itself changes
                            payload = dict(row4.payload_json or {})
                            payload["skipped"] = True
                            row4.payload_json = payload
                        s4.commit()
                        stats["skipped"] += 1
                    continue
                except Exception as exc:  # noqa: BLE001
                    with self.session_factory() as s2:
                        r2 = OutboxRepo(s2)
                        row2 = r2.get_by_idempotency_key(row.idempotency_key)
                        if row2 is None:
                            continue
                        r2.record_attempt(row2.id, success=False, error=str(exc)[:1000])
                        if row2.attempts != claimed_attempts:
                            continue  # row reclaimed — never touch it
                        if row2.attempts >= row2.max_attempts:
                            r2.mark_dead(row2.id, attempts=claimed_attempts, error=str(exc)[:2000])
                            stats["dead"] += 1
                        else:
                            # backoff atomically returns the row to PENDING
                            # with the retry time (no separate release)
                            r2.backoff(row2.id, attempts=claimed_attempts)
                            stats["released"] += 1
                        s2.commit()  # receipt must be durable
                        stats["failed"] += 1
                    continue
                with self.session_factory() as s3:
                    r3 = OutboxRepo(s3)
                    row3 = r3.get_by_idempotency_key(row.idempotency_key)
                    if row3 is None:
                        continue
                    r3.record_attempt(row3.id, success=True)
                    # write-back ONLY when the row is still ours (mark_sent
                    # fences on the claimant's generation): a reclaimed row
                    # must never let a stale claimant clobber newer state
                    if r3.mark_sent(row3.id, attempts=claimed_attempts, delivery_id=delivery_id):
                        _write_back_delivery(s3, row3, delivery_id)
                        _write_back_projection(s3, row3)
                    s3.commit()  # receipt must be durable
                    stats["sent"] += 1
        return stats

    async def _deliver(self, row) -> str:  # noqa: ANN001
        payload = row.payload_json or {}
        if (payload.get("kind") or "message") == DOC_BLOCK_KIND:
            if self.doc_platform is None:
                raise RuntimeError("doc_block outbox row but no doc platform configured")
            document_id = payload["document_id"]
            section_key = payload["section_key"]
            text = payload["text"]
            # revision-aware read when the platform exposes it: the current
            # document revision is passed to create/update as the optimistic
            # concurrency token, and a revision conflict is resolved by
            # re-reading (adopt-if-equal, else human-patch skip)
            remote = await self.doc_platform.list_blocks(document_id)
            remote_text = remote.get(section_key)
            revision = None
            reader = getattr(self.doc_platform, "list_blocks_with_revision", None)
            if reader is not None:
                try:
                    remote, revision = await reader(document_id)
                    remote_text = remote.get(section_key)
                except Exception:  # noqa: BLE001  revision read is best-effort
                    pass
            if remote_text == text:
                # already converged (adopt path): no remote write at all
                return payload.get("content_hash", "")
            # TOCTOU guard: the write is only valid if the remote is still
            # exactly what it was at enqueue time. Any change in between
            # (human edit / block deleted / block created) means the queued
            # write no longer applies — skip it (never overwrite humans).
            if remote_text != payload.get("expected_remote"):
                raise HumanPatchSkip(row.id)
            try:
                if section_key in remote:
                    await self.doc_platform.update_block(
                        document_id, section_key, text, document_revision_id=revision
                    )
                else:
                    await self.doc_platform.create_block(
                        document_id, section_key, text, document_revision_id=revision
                    )
            except FeishuDocRevisionConflict:
                # the document moved under us between the read and the write:
                # re-read and only write if the remote is still ours; any
                # human content wins (never overwrite)
                remote2 = await self.doc_platform.list_blocks(document_id)
                if remote2.get(section_key) != payload.get("expected_remote"):
                    raise HumanPatchSkip(row.id)
                if section_key in remote2:
                    await self.doc_platform.update_block(document_id, section_key, text)
                else:
                    await self.doc_platform.create_block(document_id, section_key, text)
            return payload.get("content_hash", "")
        return await self.port.deliver(
            idempotency_key=row.idempotency_key,
            kind=payload.get("kind", "message"),
            payload=payload,
            project_id=row.project_id,
        )


def _write_back_delivery(session, row, delivery_id: str) -> None:  # noqa: ANN001
    """Persist the platform message id on the outbox payload AND on the Report
    row so in-place updates (PATCH) have a handle (IMPLEMENTATION.md §21)."""
    if not delivery_id:
        return
    payload = dict(row.payload_json or {})
    payload["platform_message_id"] = delivery_id
    row.payload_json = payload
    report_id = payload.get("report_id")
    if report_id:
        from sqlalchemy import update

        from ..persistence.models import ReportRow

        session.execute(
            update(ReportRow)
            .where(ReportRow.report_id == report_id)
            .values(platform_message_id=delivery_id, status="SENT")
            .execution_options(synchronize_session=False)
        )


def _write_back_projection(session, row) -> None:  # noqa: ANN001
    """On successful doc_block delivery: persist the projection state and the
    projection.updated event in the SAME transaction as mark_sent, so the
    state can never claim a block the remote does not have. The event
    idempotency key embeds the outbox row id: A->B->A content cycles produce
    distinct rows/keys, while a retry of the same row replays the same key."""
    payload = row.payload_json or {}
    if payload.get("kind") != DOC_BLOCK_KIND:
        return
    project_id = row.project_id
    document_id = payload["document_id"]
    section_key = payload["section_key"]
    content_hash = payload.get("content_hash") or section_hash(payload.get("text", ""))

    existing = session.execute(
        select(ProjectionStateRow).where(
            ProjectionStateRow.project_id == project_id,
            ProjectionStateRow.document_id == document_id,
            ProjectionStateRow.section_key == section_key,
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            ProjectionStateRow(
                id=new_id("other"),
                project_id=project_id,
                document_id=document_id,
                section_key=section_key,
                content_hash=content_hash,
            )
        )
    else:
        existing.content_hash = content_hash

    key = f"projection-sent:{project_id}:{document_id}:{section_key}:{content_hash}:{row.id}"
    already = session.execute(
        select(EventRow.id).where(EventRow.idempotency_key == key)
    ).first()
    if already is None:
        event = make_event(
            event_type="projection.updated",
            aggregate=AggregateRef(type="projection", id=f"{document_id}:{section_key}", version=1),
            idempotency_key=key,
            project_id=project_id,
            actor=Actor(type="system"),
            payload={"section_key": section_key, "content_hash": content_hash, "outbox_row_id": row.id},
        )
        session.add(
            EventRow(
                id=f"EVTROW-{event.event_id}",
                schema=event.schema,
                event_id=event.event_id,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                project_id=event.project_id,
                aggregate_type=event.aggregate.type,
                aggregate_id=event.aggregate.id,
                aggregate_version=event.aggregate.version,
                actor_json=event.actor.model_dump() if event.actor else None,
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
                idempotency_key=event.idempotency_key,
                payload_json=event.payload,
            )
        )
