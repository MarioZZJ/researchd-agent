"""Run lifecycle + dispatch + collection (IMPLEMENTATION.md §14).

The scheduler owns the RUNNING state: it dispatches READY tasks, creates Runs,
drives the executor, enforces budgets, collects results, and routes work to
REVIEW. `researchd service` is the only writer; the scheduler runs inside it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..domain.base import Actor, AggregateRef, new_id, utcnow
from ..domain.enums import RunStatus, TaskStatus, WorkOutcome
from ..domain.events import make_event
from ..domain.run import Run
from ..domain.task import Task
from ..executors.base import ExecutorAdapter, ExecutorSessionInfo, WorkResult
from ..persistence.outbox import OutboxRepo
from ..persistence.repositories import EventRepo, RunRepo, TaskRepo
from .leases import LeaseRepo

ORPHAN_HEARTBEAT_SECONDS = 300


@dataclass
class DispatchDecision:
    action: str  # dispatch | skip | blocked
    reason: str = ""


def task_dispatch_decision(task: Task, open_decision_ids: list[str]) -> DispatchDecision:
    """Deterministic readiness: dependencies satisfied, no blocking decisions."""
    if task.status is not TaskStatus.READY:
        return DispatchDecision("skip", f"status {task.status.value}")
    if task.blocked_by:
        blocking = [d for d in task.blocked_by if d in open_decision_ids]
        if blocking:
            return DispatchDecision("blocked", f"decisions {blocking}")
    return DispatchDecision("dispatch")


class RunDispatcher:
    """Dispatches and drives one Run to completion (worker path)."""

    def __init__(self, session: Session, executor: ExecutorAdapter, *, actor: Actor | None = None):
        self.session = session
        self.executor = executor
        self.actor = actor or Actor(type="agent", executor=executor.name)

    # ------------------------------------------------------------ dispatch
    def dispatch_task(self, task: Task, *, profile: dict | None = None) -> Run | None:
        """Create a Run for a READY task, claim a lease, move task to RUNNING.
        Returns None when the lease is contended."""
        run = Run(
            run_id=new_id("run"),
            task_id=task.task_id,
            project_id=task.project_id,
            executor=self.executor.name,
            executor_profile=(profile or {}).get("name"),
            resolved_model=(profile or {}).get("model"),
            configuration_source="scheduler",
            process_instance_id=(profile or {}).get("process_instance_id"),
            lease_token=None,
        )
        run.transition(RunStatus.STARTING)
        RunRepo(self.session).save(run)
        self.session.flush()
        token = LeaseRepo(self.session).acquire(
            project_id=task.project_id,
            task_id=task.task_id,
            run_id=run.run_id,
            owner=f"scheduler:{self.executor.name}",
        )
        if token is None:
            return None
        run.lease_token = token
        run.transition(RunStatus.RUNNING)
        RunRepo(self.session).save(run)
        task.start(run_id=run.run_id, lease_token=token)
        TaskRepo(self.session).save(task)
        self._emit("run.running", run, {"task_id": task.task_id})
        self._emit("task.started", task, {"run_id": run.run_id})
        return run

    # ------------------------------------------------------------ collect
    def collect_success(self, run: Run, result: WorkResult, session_info: ExecutorSessionInfo) -> None:
        run.result = result.model_dump()
        run.session_id = session_info.session_id
        run.turn_id = session_info.turn_id
        run.outcome = result.outcome.value
        run.transition(RunStatus.SUCCEEDED)
        RunRepo(self.session).save(run)
        LeaseRepo(self.session).release(run.lease_token)
        task = TaskRepo(self.session).get_by_task_id(run.task_id)
        if task is None:
            raise RuntimeError(f"task {run.task_id!r} missing for run {run.run_id}")
        if result.outcome is WorkOutcome.SUBMIT_FOR_REVIEW:
            task.submit_review()
        elif result.outcome is WorkOutcome.BLOCKED:
            task.block()
        else:  # FAILED
            task.fail("worker reported FAILED")
        TaskRepo(self.session).save(task)
        self._emit("run.succeeded", run, {"task_id": task.task_id, "outcome": result.outcome.value})
        self._emit(
            "task.review_submitted" if result.outcome is WorkOutcome.SUBMIT_FOR_REVIEW else "task.failed",
            task,
            {"run_id": run.run_id},
        )

    def collect_failure(self, run: Run, error: str) -> None:
        run.error_message = error[:2000]
        run.transition(RunStatus.FAILED)
        RunRepo(self.session).save(run)
        LeaseRepo(self.session).release(run.lease_token)
        task = TaskRepo(self.session).get_by_task_id(run.task_id)
        if task is not None and task.status is TaskStatus.RUNNING:
            task.fail(error)
            TaskRepo(self.session).save(task)
            self._emit("task.failed", task, {"run_id": run.run_id})
        self._emit("run.failed", run, {"error": error[:500]})

    def collect_interrupt(self, run: Run, reason: str) -> None:
        """Budget/lease expiry: run INTERRUPTED, task back to READY for retry."""
        run.termination_reason = reason
        run.transition(RunStatus.INTERRUPTED)
        RunRepo(self.session).save(run)
        LeaseRepo(self.session).release(run.lease_token)
        task = TaskRepo(self.session).get_by_task_id(run.task_id)
        if task is not None and task.status is TaskStatus.RUNNING:
            task.requeue(reason=f"interrupted: {reason}")
            TaskRepo(self.session).save(task)
            self._emit("task.ready", task, {"run_id": run.run_id})
        self._emit("run.interrupted", run, {"reason": reason})

    def heartbeat(self, run: Run) -> None:
        run.heartbeat()
        RunRepo(self.session).save(run)
        if run.lease_token:
            LeaseRepo(self.session).heartbeat(run.lease_token)

    # ------------------------------------------------------------ events
    def _emit(self, event_type: str, obj: Any, payload: dict | None = None) -> None:
        EventRepo(self.session).append(
            make_event(
                event_type=event_type,
                aggregate=AggregateRef(type=obj.__class__.__name__.lower(), id=obj.id, version=obj.version),
                idempotency_key=f"{obj.id}:{event_type}:{utcnow().isoformat()}",
                project_id=obj.project_id,
                actor=self.actor,
                payload=payload,
            )
        )


def orphan_candidates(session: Session, *, max_age_seconds: int = ORPHAN_HEARTBEAT_SECONDS) -> list[Run]:
    """Runs stuck in STARTING/RUNNING with a stale heartbeat (the heartbeat is
    the authoritative liveness signal: it also renews the lease)."""
    now = utcnow()
    runs = RunRepo(session).list_active()
    out = []
    for run in runs:
        if run.status in (RunStatus.STARTING, RunStatus.RUNNING):
            heartbeat_ok = run.heartbeat_at is not None and (now - run.heartbeat_at).total_seconds() <= max_age_seconds
            if not heartbeat_ok:
                out.append(run)
    return out


def reconcile_orphans(session: Session, *, max_age_seconds: int = ORPHAN_HEARTBEAT_SECONDS) -> list[str]:
    """Reconcile stale runs after restart/crash: ORPHANED + task back to READY.
    Returns the list of orphaned run ids."""
    orphaned = []
    for run in orphan_candidates(session, max_age_seconds=max_age_seconds):
        dispatcher = RunDispatcher(session, None, actor=Actor(type="system"))  # type: ignore[arg-type]
        run.termination_reason = "orphaned: heartbeat expired"
        run.transition(RunStatus.ORPHANED)
        RunRepo(session).save(run)
        task = TaskRepo(session).get_by_task_id(run.task_id)
        if task is not None and task.status is TaskStatus.RUNNING:
            task.requeue(reason="run orphaned; retry")
            TaskRepo(session).save(task)
        orphaned.append(run.run_id)
    return orphaned
