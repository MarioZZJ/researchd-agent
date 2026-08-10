"""Transactional outbox (IMPLEMENTATION.md §10, §25.2/25.3).

Outbox rows are inserted in the SAME transaction as the aggregate update and
event append. The sender retries with backoff, supports dead-letter, and
relies on the unique idempotency_key so that:
- "committed but not sent" -> replayed after restart;
- "sent but receipt not written" -> delivery is idempotent on the key.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..domain.base import new_id, utcnow
from ..domain.enums import OutboxStatus
from .models import OutboxAttemptRow, OutboxRow
from .transaction import OptimisticConcurrencyError

BACKOFF_BASE_SECONDS = 5
BACKOFF_MAX_SECONDS = 300


class OutboxRepo:
    def __init__(self, session: Session):
        self.session = session

    def enqueue(
        self,
        *,
        destination: str,
        idempotency_key: str,
        payload: dict,
        project_id: str | None = None,
        max_attempts: int = 5,
    ) -> OutboxRow:
        row = OutboxRow(
            id=new_id("other"),
            project_id=project_id,
            destination=destination,
            idempotency_key=idempotency_key,
            payload_json=payload,
            status=OutboxStatus.PENDING.value,
            max_attempts=max_attempts,
        )
        self.session.add(row)
        return row

    def pending(self, *, limit: int = 50, destination: str | None = None) -> list[OutboxRow]:
        """Rows eligible for sending: PENDING (due) or IN_FLIGHT with expired lease."""
        now = utcnow()
        stmt = (
            select(OutboxRow)
            .where(
                ((OutboxRow.status == OutboxStatus.PENDING.value) & (OutboxRow.next_attempt_at.is_(None) | (OutboxRow.next_attempt_at <= now)))
                | ((OutboxRow.status == OutboxStatus.IN_FLIGHT.value) & (OutboxRow.next_attempt_at <= now))
            )
            .order_by(OutboxRow.created_at)
            .limit(limit)
        )
        if destination:
            stmt = stmt.where(OutboxRow.destination == destination)
        return list(self.session.execute(stmt).scalars())

    def get_by_idempotency_key(self, key: str) -> OutboxRow | None:
        return self.session.execute(
            select(OutboxRow).where(OutboxRow.idempotency_key == key)
        ).scalar_one_or_none()

    def claim(self, row_id: str, *, attempts: int, lease_seconds: int = 60) -> bool:
        """Atomically claim a row: -> IN_FLIGHT with a fresh lease.

        Only PENDING rows and IN_FLIGHT rows whose lease has EXPIRED can be
        claimed; the attempts condition makes concurrent claims mutually
        exclusive (a lost worker's row is reclaimed only after its lease lapses).
        """
        now = utcnow()
        result = self.session.execute(
            update(OutboxRow)
            .where(
                OutboxRow.id == row_id,
                OutboxRow.attempts == attempts,
                (
                    (OutboxRow.status == OutboxStatus.PENDING.value)
                    | ((OutboxRow.status == OutboxStatus.IN_FLIGHT.value) & (OutboxRow.next_attempt_at <= now))
                ),
            )
            .values(
                attempts=attempts + 1,
                status=OutboxStatus.IN_FLIGHT.value,
                next_attempt_at=now + timedelta(seconds=lease_seconds),
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def release(self, row_id: str, *, attempts: int) -> bool:
        """Return a failed in-flight row to PENDING (retry path), clearing the lease.

        Only the current claimant (matching attempts) may release; a late
        release after the row was reclaimed/SENT is a no-op.
        """
        result = self.session.execute(
            update(OutboxRow)
            .where(
                OutboxRow.id == row_id,
                OutboxRow.status == OutboxStatus.IN_FLIGHT.value,
                OutboxRow.attempts == attempts,
            )
            .values(status=OutboxStatus.PENDING.value, next_attempt_at=None)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def record_attempt(self, row_id: str, *, success: bool, error: str | None = None) -> None:
        self.session.add(
            OutboxAttemptRow(
                id=new_id("other"),
                outbox_id=row_id,
                success=success,
                error=error,
            )
        )

    def mark_sent(self, row_id: str, *, attempts: int, delivery_id: str | None = None) -> bool:
        """Mark SENT; only valid for the current claimant's in-flight row.
        delivery_id is persisted by the sender (write-back), not here."""
        result = self.session.execute(
            update(OutboxRow)
            .where(
                OutboxRow.id == row_id,
                OutboxRow.status == OutboxStatus.IN_FLIGHT.value,
                OutboxRow.attempts == attempts,
            )
            .values(status=OutboxStatus.SENT.value, sent_at=utcnow(), last_error=None)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def mark_dead(self, row_id: str, *, attempts: int, error: str) -> bool:
        """Dead-letter; only valid for the current claimant's in-flight row."""
        result = self.session.execute(
            update(OutboxRow)
            .where(
                OutboxRow.id == row_id,
                OutboxRow.status == OutboxStatus.IN_FLIGHT.value,
                OutboxRow.attempts == attempts,
            )
            .values(status=OutboxStatus.DEAD.value, last_error=error[:2000])
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def backoff(self, row_id: str, *, attempts: int) -> bool:
        """Failed delivery: ATOMICALLY return the in-flight row to PENDING with
        an exponential retry time (single UPDATE: no window where a concurrent
        sender could re-claim, and no separate release that would wipe the
        retry time). Returns False if the row moved on meanwhile."""
        delay = next_backoff_delay(attempts)
        result = self.session.execute(
            update(OutboxRow)
            .where(
                OutboxRow.id == row_id,
                OutboxRow.status == OutboxStatus.IN_FLIGHT.value,
                OutboxRow.attempts == attempts,
            )
            .values(status=OutboxStatus.PENDING.value, next_attempt_at=utcnow() + delay)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1


def next_backoff_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(BACKOFF_BASE_SECONDS * (2 ** min(attempts, 6)), BACKOFF_MAX_SECONDS))


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
