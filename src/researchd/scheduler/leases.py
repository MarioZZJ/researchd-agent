"""Leases and workspace locks (IMPLEMENTATION.md §14, §25.5).

Leases give a dispatcher exclusive claim over a task/run; heartbeats renew
them; an expired lease means the previous holder died and another instance may
reclaim. Workspace locks serialize writes to the same project resource (file
or document section) so concurrent workers never interleave.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..domain.base import new_id, utcnow
from ..persistence.models import LeaseRow, WorkspaceLockRow

DEFAULT_LEASE_SECONDS = 300
DEFAULT_HEARTBEAT_SECONDS = 60
DEFAULT_LOCK_SECONDS = 3600


class LeaseNotHeld(Exception):
    pass


class WorkspaceLocked(Exception):
    def __init__(self, project_id: str, scope: str, owner: str):
        super().__init__(f"workspace {project_id}:{scope} locked by {owner}")
        self.project_id = project_id
        self.scope = scope
        self.owner = owner


class LeaseRepo:
    def __init__(self, session: Session):
        self.session = session

    def acquire(
        self,
        *,
        project_id: str,
        task_id: str | None,
        run_id: str | None,
        owner: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> str:
        """Acquire a lease on the run slot. Fails (returns None) if a live
        lease exists for the same run, or — for auditor/worker roles — a live
        lease exists for the same (task_id, owner-prefix) so a restart with a
        still-fresh heartbeat can never double-dispatch the same task."""
        existing = self.session.execute(
            select(LeaseRow).where(LeaseRow.run_id == run_id)
        ).scalar_one_or_none()
        if existing is not None and existing.expires_at > utcnow() and existing.released_at is None:
            return None  # live lease held by someone else
        if task_id and owner:
            prefix = owner.split(":")[0]
            dup = self.session.execute(
                select(LeaseRow).where(
                    LeaseRow.task_id == task_id,
                    LeaseRow.owner.like(prefix + ":%"),
                    LeaseRow.released_at.is_(None),
                    LeaseRow.expires_at > utcnow(),
                )
            ).scalar_one_or_none()
            if dup is not None and dup.run_id != run_id:
                return None  # task-level slot already live (fresh heartbeat)
        token = f"L-{new_id('other')[2:]}"
        row = LeaseRow(
            id=new_id("other"),
            project_id=project_id,
            task_id=task_id,
            run_id=run_id,
            owner=owner,
            token=token,
            expires_at=utcnow() + timedelta(seconds=lease_seconds),
            created_at=utcnow(),
        )
        self.session.add(row)
        self.session.flush()  # visible to same-session queries
        return token

    def heartbeat(self, token: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> bool:
        """Renew the lease; False when the lease was released/expired meanwhile."""
        result = self.session.execute(
            update(LeaseRow)
            .where(LeaseRow.token == token, LeaseRow.released_at.is_(None))
            .values(expires_at=utcnow() + timedelta(seconds=lease_seconds), heartbeat_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def release(self, token: str) -> bool:
        result = self.session.execute(
            update(LeaseRow)
            .where(LeaseRow.token == token, LeaseRow.released_at.is_(None))
            .values(released_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def live_for_run(self, run_id: str) -> LeaseRow | None:
        row = self.session.execute(
            select(LeaseRow).where(LeaseRow.run_id == run_id)
        ).scalar_one_or_none()
        if row is None or row.released_at is not None:
            return None
        if row.expires_at <= utcnow():
            return None
        return row

    def expired(self, *, limit: int = 50) -> list[LeaseRow]:
        rows = self.session.execute(
            select(LeaseRow)
            .where(LeaseRow.released_at.is_(None), LeaseRow.expires_at <= utcnow())
            .order_by(LeaseRow.expires_at)
            .limit(limit)
        ).scalars()
        return list(rows)


class WorkspaceLockRepo:
    def __init__(self, session: Session):
        self.session = session

    def acquire(
        self,
        *,
        project_id: str,
        scope: str,
        owner: str,
        lock_seconds: int = DEFAULT_LOCK_SECONDS,
        wait: bool = False,
    ) -> str | None:
        """Acquire an exclusive workspace lock. Returns token or None (locked)."""
        now = utcnow()
        existing = self.session.execute(
            select(WorkspaceLockRow).where(
                WorkspaceLockRow.project_id == project_id, WorkspaceLockRow.scope == scope
            )
        ).scalar_one_or_none()
        if existing is not None and existing.expires_at > now:
            return None
        token = f"WL-{new_id('other')[2:]}"
        if existing is not None:
            # steal expired lock
            self.session.execute(
                update(WorkspaceLockRow)
                .where(WorkspaceLockRow.id == existing.id)
                .values(owner=owner, token=token, acquired_at=now, expires_at=now + timedelta(seconds=lock_seconds))
                .execution_options(synchronize_session=False)
            )
        else:
            self.session.add(
                WorkspaceLockRow(
                    id=new_id("other"),
                    project_id=project_id,
                    scope=scope,
                    owner=owner,
                    token=token,
                    acquired_at=now,
                    expires_at=now + timedelta(seconds=lock_seconds),
                )
            )
        self.session.flush()  # make the lock visible to same-session queries
        return token

    def release(self, project_id: str, scope: str, token: str) -> bool:
        result = self.session.execute(
            update(WorkspaceLockRow)
            .where(
                WorkspaceLockRow.project_id == project_id,
                WorkspaceLockRow.scope == scope,
                WorkspaceLockRow.token == token,
            )
            .values(expires_at=utcnow())  # expiry doubles as release marker
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1
