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


def task_dispatch_decision(task: Task, open_decision_ids: list[str], blocked_task_ids: set[str] | None = None) -> DispatchDecision:
    """Deterministic readiness: dependencies satisfied, no blocking decisions.

    `blocked_task_ids` is the union of blocking_scope of OPEN decisions — only
    those tasks are paused (IMPLEMENTATION.md §8: only blocking_scope blocks).
    """
    if task.status is not TaskStatus.READY:
        return DispatchDecision("skip", f"status {task.status.value}")
    if blocked_task_ids and task.task_id in blocked_task_ids:
        return DispatchDecision("blocked", "in blocking_scope of an OPEN decision")
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
            reasoning_effort=(profile or {}).get("reasoning_effort"),
            configuration_source=(profile or {}).get("source") or "scheduler",
            process_instance_id=(profile or {}).get("process_instance_id"),
            lease_token=None,
        )
        run.transition(RunStatus.STARTING)
        # resolved config + mounted skills are frozen on the run for
        # traceability (IMPLEMENTATION.md §15.2: record what actually ran)
        run.metadata = dict(run.metadata or {})
        run.metadata["skills"] = list(getattr(self.executor, "installed_skills", []) or [])
        run.metadata["context_id"] = None  # set by the scheduler before the turn
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
        # usage: exactly what the executor reports; explicit unavailable when
        # it reports nothing — never fabricated
        run.usage = getattr(session_info, "usage", None) or {
            "available": False,
            "reason": "executor does not report usage",
        }
        run.metadata = dict(run.metadata or {})
        if getattr(session_info, "transcript_path", None):
            run.metadata["transcript_path"] = session_info.transcript_path  # path only
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
        # persist the worker's structured findings in the SAME transaction
        from ..application.apply_result import apply_work_result

        apply_work_result(self.session, run, result)
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

    # ------------------------------------------------------------ auditor
    def dispatch_audit_run(self, task: Task, *, profile: dict | None = None, worker_run_id: str | None = None) -> Run | None:
        """Create an auditor Run for a REVIEW task. The task STAYS REVIEW
        (REVIEW -> RUNNING is illegal); the lease serializes audits so a
        crashed/replayed audit can never double-run. Returns None when the
        lease is contended (an audit for this task is already in flight)."""
        run = Run(
            run_id=new_id("run"),
            task_id=task.task_id,
            project_id=task.project_id,
            executor=self.executor.name,
            executor_profile=(profile or {}).get("name"),
            resolved_model=(profile or {}).get("model"),
            reasoning_effort=(profile or {}).get("reasoning_effort"),
            configuration_source=(profile or {}).get("source") or "scheduler",
            process_instance_id=(profile or {}).get("process_instance_id"),
            lease_token=None,
            metadata={"role": "auditor", "worker_run_id": worker_run_id or task.current_run_id},
        )
        run.transition(RunStatus.STARTING)
        run.metadata = dict(run.metadata or {})
        run.metadata["skills"] = list(getattr(self.executor, "installed_skills", []) or [])
        RunRepo(self.session).save(run)
        self.session.flush()
        token = LeaseRepo(self.session).acquire(
            project_id=task.project_id,
            task_id=task.task_id,
            run_id=run.run_id,
            owner=f"auditor:{self.executor.name}",
        )
        if token is None:
            return None
        run.lease_token = token
        run.transition(RunStatus.RUNNING)
        RunRepo(self.session).save(run)
        self._emit("run.running", run, {"task_id": task.task_id, "role": "auditor"})
        return run

    def collect_audit(self, run: Run, result: Any, session_info: ExecutorSessionInfo) -> None:
        """Apply an auditor verdict in the SAME transaction as run success."""
        run.result = result.model_dump()
        run.session_id = session_info.session_id
        run.turn_id = session_info.turn_id
        run.usage = getattr(session_info, "usage", None) or {
            "available": False,
            "reason": "executor does not report usage",
        }
        run.metadata = dict(run.metadata or {})
        if getattr(session_info, "transcript_path", None):
            run.metadata["transcript_path"] = session_info.transcript_path  # path only
        outcome = result.verdict.value if hasattr(result.verdict, "value") else str(result.verdict)
        run.outcome = outcome
        run.transition(RunStatus.SUCCEEDED)
        RunRepo(self.session).save(run)
        LeaseRepo(self.session).release(run.lease_token)
        task = TaskRepo(self.session).get_by_task_id(run.task_id)
        if task is None:
            raise RuntimeError(f"task {run.task_id!r} missing for audit run {run.run_id}")
        from ..application.audit_gate import apply_audit_result

        counts = apply_audit_result(self.session, run, task, result)
        self._emit("run.succeeded", run, {"task_id": task.task_id, "verdict": outcome})
        if counts.get("completed"):
            self._emit("task.completed", task, {"audit_run_id": run.run_id})
        elif counts.get("revised") or counts.get("blocked"):
            self._emit("task.ready", task, {"audit_run_id": run.run_id, "verdict": outcome})
        elif counts.get("rejected"):
            self._emit("task.failed", task, {"audit_run_id": run.run_id})

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


def reconcile_orphans(session: Session, *, max_age_seconds: int = ORPHAN_HEARTBEAT_SECONDS, data_dir: str | Path | None = None) -> list[str]:
    """Reconcile stale runs after restart/crash.

    Receipt recovery: when the model ALREADY produced the full structured
    result (runtime receipt written by the executor adapter before collect),
    the run is finished from the receipt WITHOUT calling the model again.
    Without a receipt the run is ORPHANED and the task requeued for retry
    (the retry reason is recorded). Returns the list of handled run ids."""
    orphaned = []
    for run in orphan_candidates(session, max_age_seconds=max_age_seconds):
        dispatcher = RunDispatcher(session, None, actor=Actor(type="system"))  # type: ignore[arg-type]
        if data_dir and _recover_from_receipt(session, dispatcher, run, data_dir):
            orphaned.append(run.run_id)
            continue
        run.termination_reason = "orphaned: heartbeat expired; no completion receipt; retry"
        run.transition(RunStatus.ORPHANED)
        RunRepo(session).save(run)
        task = TaskRepo(session).get_by_task_id(run.task_id)
        if task is not None and task.status is TaskStatus.RUNNING:
            task.requeue(reason="run orphaned; retry")
            TaskRepo(session).save(task)
        orphaned.append(run.run_id)
    return orphaned


def _recover_from_receipt(session: Session, dispatcher: Any, run: Run, data_dir: str | Path) -> bool:
    """Finish an orphaned run from its runtime receipt (model already
    produced the result). Returns True when recovered. A corrupt receipt
    is treated as absent (retry) — never silently trusted."""
    import json as _json
    from pathlib import Path as _Path

    from ..executors.base import ExecutorSessionInfo, validate_audit_result, validate_work_result

    path = _Path(data_dir) / "receipts" / f"{run.run_id}.json"
    if not path.exists():
        return False
    try:
        receipt = _json.loads(path.read_text())
        raw = receipt["raw"]
        role = receipt.get("role", "worker")
        validator = validate_audit_result if role == "auditor" else validate_work_result
        result = validator(raw)
        if validator is validate_audit_result:
            result = result  # AuditResult
        info = ExecutorSessionInfo(
            executor=run.executor or "reasonix",
            session_id=receipt.get("session_id"),
            transcript_path=receipt.get("transcript_path"),
        )
        if role == "auditor":
            dispatcher.collect_audit(run, result, info)
        else:
            dispatcher.collect_success(run, result, info)
        run.termination_reason = "recovered from runtime receipt (no re-invocation)"
        RunRepo(session).save(run)
        path.unlink()  # receipt is single-use
        return True
    except Exception:  # noqa: BLE001  corrupt receipt -> fall through to retry
        logger.warning("receipt for run %s unusable; falling back to retry", run.run_id)
        return False
