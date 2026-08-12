"""Auditor gate: the ONLY path that turns a REVIEW task COMPLETED and a
CANDIDATE evidence VERIFIED (IMPLEMENTATION.md §7.3, §26).

- The auditor runs on an INDEPENDENT context package (never the worker's
  free-text self-assessment) and returns a verdict per evidence candidate.
- ACCEPT: the worker run's CANDIDATE evidence rows are re-validated against
  real persisted provenance and VERIFIED; the task COMPLETEs only when every
  success criterion PASSes. Evidence stays CANDIDATE when real provenance is
  missing (provenance is a hard gate, independent of the auditor's opinion).
- REVISE: the task returns to READY for another worker turn.
- BLOCK: the task returns to READY; the audit run's decision_candidates are
  picked up by the Decision Gate (blocking_scope pauses the task).
- REJECT: the task FAILs with the auditor's reason.

Idempotency: the `audit-applied:<audit_run_id>` event is written in the SAME
transaction as the verdict application; a replayed collect (crash between
apply and commit) is a no-op and can never double-verify or double-complete.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.base import Actor, AggregateRef, new_id
from ..domain.enums import AuditVerdict, TaskStatus
from ..domain.events import make_event
from ..executors.base import AuditResult
from ..persistence.models import EventRow
from ..persistence.repositories import (
    EvidenceRepo,
    EventRepo,
    RunRepo,
    TaskRepo,
)

logger = logging.getLogger("researchd.audit")

AUDIT_APPLIED_KEY = "audit-applied"


def _already_applied(session: Session, audit_run_id: str) -> bool:
    key = f"{AUDIT_APPLIED_KEY}:{audit_run_id}"
    return (
        session.execute(select(EventRow.id).where(EventRow.idempotency_key == key)).first()
        is not None
    )


def apply_audit_result(session: Session, audit_run, task, result: AuditResult) -> dict:
    """Apply an auditor verdict. Returns counters; raises on invalid state."""
    if _already_applied(session, audit_run.run_id):
        return {"replayed": True}

    worker_run_id = (audit_run.metadata or {}).get("worker_run_id") or task.current_run_id
    worker_run = RunRepo(session).get_by_run_id(worker_run_id) if worker_run_id else None
    if worker_run is None:
        raise RuntimeError(
            f"audit run {audit_run.run_id}: worker run {worker_run_id!r} not found; cannot apply"
        )
    if task.status is not TaskStatus.REVIEW:
        raise RuntimeError(
            f"audit run {audit_run.run_id}: task {task.task_id} is {task.status.value}, not REVIEW"
        )

    verdict = result.verdict.value if hasattr(result.verdict, "value") else str(result.verdict)
    counts = {"verified_evidence": 0, "revised": 0, "blocked": 0, "rejected": 0, "completed": 0}
    worker_result = worker_run.result or {}

    if verdict in ("ACCEPT",):
        # 0. auditor checks must all PASS — an ACCEPT with a FAIL check is
        #    NOT an accept (the worker's criteria self-report is never
        #    trusted over the auditor's checks)
        checks = result.checks or []
        if any(c.status == "FAIL" for c in checks):
            task.requeue(reason="auditor ACCEPT but check FAIL: " + str(next(c.summary for c in checks if c.status == "FAIL"))[:200])
            TaskRepo(session).save(task)
            counts["revised"] += 1
        else:
            # 1. evidence: per-candidate verdicts from evidence_status_changes
            #    (explicit list), else ACCEPT implies VERIFY-when-provenance
            wanted = {
                c.evidence_id: c.to_status
                for c in (result.evidence_status_changes or [])
            }
            explicit = bool(wanted)
            for ev in EvidenceRepo(session).list_by_project(worker_run.project_id):
                if ev.run_id != worker_run.run_id or ev.status.value != "CANDIDATE":
                    continue
                target = wanted.get(ev.evidence_id)
                if target == "VERIFIED" or (not explicit and target is None):
                    try:
                        from ..application.evidence_validation import verify_evidence

                        ev = verify_evidence(session, ev)
                        EvidenceRepo(session).save(ev)
                        counts["verified_evidence"] += 1
                    except Exception as exc:  # noqa: BLE001  provenance gate
                        logger.warning(
                            "evidence %s stays CANDIDATE after audit ACCEPT: %s", ev.evidence_id, exc
                        )
                elif target in ("CONTESTED", "INVALID", "SUPERSEDED"):
                    ev.transition(target)
                    EvidenceRepo(session).save(ev)
                # no explicit verdict -> stays CANDIDATE (not verified)
            # 2. task completion: ALL success criteria must PASS
            criteria = worker_result.get("criteria_results", [])
            results = {c.get("criterion_id"): c.get("status") for c in criteria}
            required = {sc.id for sc in task.contract.success_criteria}
            all_pass = bool(required) and all(results.get(cid) == "PASS" for cid in required)
            if not all_pass:
                # auditor accepted but the work itself did not meet the criteria:
                # FAIL (never COMPLETE, never an infinite requeue loop)
                task.fail("auditor ACCEPT but success criteria not all PASS")
                TaskRepo(session).save(task)
                counts["rejected"] += 1
            else:
                task.complete(criteria)
                TaskRepo(session).save(task)
                counts["completed"] += 1
    elif verdict in ("REVISE",):
        reason = "auditor REVISE"
        if result.revision_request:
            reason += ": " + str(result.revision_request)[:300]
        task.requeue(reason=reason)
        TaskRepo(session).save(task)
        counts["revised"] += 1
    elif verdict in ("BLOCK",):
        task.requeue(reason="auditor BLOCK: needs PI decision")
        TaskRepo(session).save(task)
        counts["blocked"] += 1
    elif verdict in ("REJECT",):
        reason = "auditor REJECT"
        if result.revision_request:
            reason += ": " + str(result.revision_request)[:300]
        task.fail(reason)
        TaskRepo(session).save(task)
        counts["rejected"] += 1
    else:
        raise RuntimeError(f"audit run {audit_run.run_id}: unknown verdict {verdict!r}")

    # idempotency gate in the SAME transaction as the state writes
    event_type = {
        "ACCEPT": "audit.accepted",
        "REVISE": "audit.revised",
        "BLOCK": "audit.blocked",
        "REJECT": "audit.rejected",
    }.get(verdict, f"audit.{verdict.lower()}")
    EventRepo(session).append(
        make_event(
            event_type=event_type,
            aggregate=AggregateRef(type="task", id=task.id, version=task.version),
            idempotency_key=f"{AUDIT_APPLIED_KEY}:{audit_run.run_id}",
            project_id=task.project_id,
            actor=Actor(type="agent", executor="auditor", model=audit_run.resolved_model, run_id=audit_run.run_id),
            payload={
                "audit_run_id": audit_run.run_id,
                "worker_run_id": worker_run.run_id,
                "verdict": verdict,
                "counts": counts,
            },
        )
    )
    return counts
