"""Outbox sender: claim -> deliver -> mark_sent/release/dead (IMPLEMENTATION.md
§10, §25.2). Delivery is idempotent on the outbox idempotency_key: a crash
after commit-and-send but before mark_sent replays the same key, and the
delivery port deduplicates on it.
"""

from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy.orm import Session

from ..domain.base import new_id
from ..persistence.outbox import OutboxRepo

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


class OutboxSender:
    def __init__(self, session_factory, port: DeliveryPort, *, max_per_tick: int = 20):
        self.session_factory = session_factory
        self.port = port
        self.max_per_tick = max_per_tick

    async def send_pending(self) -> dict:
        """Deliver due outbox rows. Returns delivery counters for diagnostics."""
        stats = {"claimed": 0, "sent": 0, "failed": 0, "dead": 0, "released": 0}
        with self.session_factory() as session:
            repo = OutboxRepo(session)
            rows = repo.pending(limit=self.max_per_tick)
            for row in rows:
                if not repo.claim(row.id, attempts=row.attempts):
                    continue
                session.commit()
                stats["claimed"] += 1
                try:
                    delivery_id = await self.port.deliver(
                        idempotency_key=row.idempotency_key,
                        kind=(row.payload_json or {}).get("kind", "message"),
                        payload=row.payload_json or {},
                        project_id=row.project_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    with self.session_factory() as s2:
                        r2 = OutboxRepo(s2)
                        row2 = r2.get_by_idempotency_key(row.idempotency_key)
                        if row2 is None:
                            continue
                        r2.record_attempt(row2.id, success=False, error=str(exc)[:1000])
                        if row2.attempts >= row2.max_attempts:
                            r2.mark_dead(row2.id, attempts=row2.attempts, error=str(exc)[:2000])
                            stats["dead"] += 1
                        else:
                            # backoff atomically returns the row to PENDING
                            # with the retry time (no separate release)
                            r2.backoff(row2.id, attempts=row2.attempts)
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
                    if not r3.mark_sent(row3.id, attempts=row3.attempts, delivery_id=delivery_id):
                        # row moved on (reclaimed) — delivery itself was idempotent
                        pass
                    _write_back_delivery(s3, row3, delivery_id)
                    s3.commit()  # receipt must be durable
                    stats["sent"] += 1
        return stats


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
        from sqlalchemy import select, update

        from ..persistence.models import ReportRow

        session.execute(
            update(ReportRow)
            .where(ReportRow.report_id == report_id)
            .values(platform_message_id=delivery_id, status="SENT")
            .execution_options(synchronize_session=False)
        )
