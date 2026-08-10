"""Scheduler extensions for the golden path (IMPLEMENTATION.md §23 Phase 8,
§26): project planning, milestone reporting, cheap diagnostics.

- plan_projects: ACTIVE project with zero tasks and no planner.ran event gets
  its first task batch from the planner (crash-safe via the event key).
- check_milestones: once the project reaches its verified-evidence threshold,
  emit ONE milestone report + milestone.reached event (idempotent forever).
- ensure_cheap_diagnostics: OPEN conflict candidates flagged cheap_parallel
  get a READY diagnostic task instead of a PI decision; the gate is only
  consulted again after the diagnostic run completes.
"""

from __future__ import annotations

import hashlib
import logging

from ..domain.base import Actor, AggregateRef, new_id
from ..domain.events import make_event
from ..domain.task import Task, TaskContract, TaskStatus

logger = logging.getLogger("researchd.scheduler.ext")

CHEAP_DIAGNOSTIC_MARKER = "cheap-diagnostic:"


async def plan_projects(session_factory, executor) -> int:  # noqa: ANN001
    """Materialize the first task batch for task-less ACTIVE projects."""
    from sqlalchemy import select

    from ..persistence.models import EventRow
    from ..persistence.repositories import EventRepo, ProjectRepo, TaskRepo

    planned = 0
    with session_factory() as session:
        projects = ProjectRepo(session).list_all()
    for project in projects:
        if project.status.value != "ACTIVE":
            continue
        with session_factory() as session:
            if TaskRepo(session).list_by_status(project.project_id, []):
                continue
            key = f"planner:{project.project_id}:ran"
            if session.execute(select(EventRow.id).where(EventRow.idempotency_key == key)).first():
                continue  # planner already ran for this project
        # execute OUTSIDE the transaction (long-running)
        try:
            result, _session_info = await executor.run_planner(
                {
                    "project": {
                        "project_id": project.project_id,
                        "name": project.name,
                        "description": project.description,
                    }
                },
                profile={},
            )
        except Exception:  # noqa: BLE001
            logger.exception("planner failed for project %s", project.project_id)
            continue
        with session_factory() as session:
            if TaskRepo(session).list_by_status(project.project_id, []):
                continue  # raced with another tick
            if session.execute(select(EventRow.id).where(EventRow.idempotency_key == key)).first():
                continue
            created = 0
            for pt in result.proposed_tasks:
                task = Task(
                    task_id=pt.task_id,
                    project_id=project.project_id,
                    status=TaskStatus.READY,  # system-approved first batch
                    contract=TaskContract(
                        task_id=pt.task_id,
                        role=pt.role,
                        objective=pt.objective,
                        why_now=pt.why_now,
                        inputs=pt.inputs,
                        deliverables=pt.deliverables,
                        success_criteria=pt.success_criteria,
                        stop_conditions=pt.stop_conditions,
                        escalation_conditions=pt.escalation_conditions,
                        budget=pt.budget,
                        executor_profile=pt.executor_profile,
                    ),
                    created_by="planner",
                )
                TaskRepo(session).save(task)
                created += 1
            if created:
                EventRepo(session).append(
                    make_event(
                        event_type="planner.ran",
                        aggregate=AggregateRef(type="project", id=project.project_id, version=1),
                        idempotency_key=key,
                        project_id=project.project_id,
                        actor=Actor(type="system"),
                        payload={"task_count": created},
                    )
                )
                session.commit()
                planned += 1
                logger.info("planned %d tasks for %s", created, project.project_id)
    return planned


def check_milestones(session_factory) -> int:  # noqa: ANN001
    """Emit one milestone report per project once the verified-evidence
    threshold is reached (idempotent via the milestone.reached event key)."""
    from sqlalchemy import select

    from ..domain.enums import ReportType
    from ..persistence.models import EventRow, OutboxRow, ReportRow
    from ..persistence.outbox import OutboxStatus
    from ..persistence.repositories import EvidenceRepo, ProjectRepo

    reached = 0
    with session_factory() as session:
        projects = ProjectRepo(session).list_all()
    for project in projects:
        if project.status.value != "ACTIVE":
            continue
        threshold = int((project.metadata or {}).get("milestone_evidence_threshold", 2))
        with session_factory() as session:
            verified = len(EvidenceRepo(session).list_verified(project.project_id))
            if verified < threshold:
                continue
            key = f"milestone:{project.project_id}:{threshold}"
            if session.execute(select(EventRow.id).where(EventRow.idempotency_key == key)).first():
                continue  # already reached (idempotent across restarts)
            body = (
                f"【MILESTONE】{project.name}\n"
                f"已验证证据达到阈值（{verified} ≥ {threshold}）\n"
                f"里程碑：证据基础就绪，可进入综合阶段。"
            )
            report_id = new_id("report")
            session.add(
                ReportRow(
                    id=report_id,
                    report_id=report_id,
                    project_id=project.project_id,
                    spec_json={
                        "type": ReportType.DIGEST.value,
                        "body": body,
                        "project_id": project.project_id,
                        "milestone_id": f"MS-{project.project_id}",
                    },
                    status="COMPILED",
                    body_hash=hashlib.sha256(body.encode()).hexdigest(),
                )
            )
            session.add(
                OutboxRow(
                    id=new_id("outbox"),
                    destination="delivery",
                    idempotency_key=f"milestone:{report_id}",
                    project_id=project.project_id,
                    payload_json={
                        "kind": "message",
                        "project_id": project.project_id,
                        "report_id": report_id,
                        "body": body,
                        "source": "milestone",
                        "milestone_id": f"MS-{project.project_id}",
                    },
                    status=OutboxStatus.PENDING.value,
                    attempts=0,
                    max_attempts=8,
                    next_attempt_at=None,
                )
            )
            from ..persistence.repositories import EventRepo

            EventRepo(session).append(
                make_event(
                    event_type="milestone.reached",
                    aggregate=AggregateRef(type="project", id=project.project_id, version=1),
                    idempotency_key=key,
                    project_id=project.project_id,
                    actor=Actor(type="system"),
                    payload={"verified_evidence": verified, "threshold": threshold},
                )
            )
            session.commit()
            reached += 1
            logger.info("milestone reached for %s (%d evidence)", project.project_id, verified)
    return reached


def ensure_cheap_diagnostics(session_factory) -> int:  # noqa: ANN001
    """For OPEN conflict candidates flagged cheap_parallel: create a READY
    diagnostic task. Idempotent per (project, question hash); a diagnostic
    task's own result is evaluated normally (no new diagnostic is spawned
    from it).

    Terminal-state handling: if a diagnostic task for this question already
    reached a terminal state (REVIEW/FAILED) AND a second attempt would be
    needed, the source run is marked evaluated (the gate gives up and the
    conflict is treated as settled by the diagnostic outcome) instead of
    scanning forever."""
    from sqlalchemy import select

    from ..persistence.models import RunRow, TaskRow
    from ..persistence.repositories import TaskRepo

    created = 0
    with session_factory() as session:
        rows = session.execute(
            select(RunRow).where(
                RunRow.status.in_(["SUCCEEDED", "COMPLETED"]),
                RunRow.result_json.is_not(None),
            )
        ).scalars().all()
        tasks = {t.task_id: t for t in TaskRepo(session).list_by_status(None, [])}
        for row in rows:
            task = tasks.get(row.task_id)
            if task is None:
                continue
            why_now = task.contract.why_now or ""
            if why_now.startswith(CHEAP_DIAGNOSTIC_MARKER):
                continue  # a diagnostic run's own result is evaluated directly
            result = row.result_json or {}
            for cand in result.get("decision_candidates", []):
                if not cand.get("cheap_parallel"):
                    continue
                if not cand.get("has_option_conflict", True):
                    continue
                question = cand.get("question", "")
                qhash = hashlib.sha256(question.encode()).hexdigest()[:12]
                marker = f"{CHEAP_DIAGNOSTIC_MARKER}{qhash}"
                diag_tasks = session.execute(
                    select(TaskRow).where(
                        TaskRow.project_id == row.project_id,
                        TaskRow.contract_json["why_now"].as_string().like(marker + "%"),
                    )
                ).scalars().all()
                active = [
                    t for t in diag_tasks
                    if t.status in ("READY", "RUNNING", "BLOCKED")
                ]
                if active:
                    continue  # diagnostic already queued/running
                if len(diag_tasks) >= 2:
                    # the diagnostic ran twice without resolving; stop
                    # spawning and let the gate settle the conflict directly
                    from ..persistence.models import EventRow

                    key = f"diagnostic-giveup:{row.project_id}:{qhash}"
                    if session.execute(select(EventRow.id).where(EventRow.idempotency_key == key)).first() is None:
                        from ..domain.base import Actor, AggregateRef
                        from ..domain.events import make_event
                        from ..persistence.repositories import EventRepo

                        EventRepo(session).append(
                            make_event(
                                event_type="diagnostic.giveup",
                                aggregate=AggregateRef(type="run", id=row.run_id, version=1),
                                idempotency_key=key,
                                project_id=row.project_id,
                                actor=Actor(type="system"),
                                payload={"question": question[:200]},
                            )
                        )
                    continue
                # deterministic id scoped to the project (task ids are global)
                task_id = f"DGN-{row.project_id[-8:]}-{qhash}"
                task = Task(
                    task_id=task_id,
                    project_id=row.project_id,
                    status=TaskStatus.READY,
                    contract=TaskContract(
                        task_id=task_id,
                        role="worker",
                        objective=f"廉价诊断：{question}",
                        why_now=marker,
                        success_criteria=[
                            {"id": "dgn-1", "text": "判断冲突是否实质性并回报结论"}
                        ],
                    ),
                    created_by="scheduler",
                )
                try:
                    TaskRepo(session).save(task)
                    session.flush()  # visible to the same-tick duplicate check
                except Exception:  # noqa: BLE001  unique race with another tick
                    session.rollback()
                    continue
                created += 1
                logger.info("cheap diagnostic queued for %s", question[:80])
        session.commit()
    return created
