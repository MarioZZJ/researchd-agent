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
from pathlib import Path

from ..domain.base import Actor, AggregateRef, new_id
from ..domain.events import make_event
from ..domain.task import Task, TaskContract, TaskStatus

logger = logging.getLogger("researchd.scheduler.ext")

CHEAP_DIAGNOSTIC_MARKER = "cheap-diagnostic:"


async def plan_projects(session_factory, executor, *, planner_profile: dict | None = None, data_dir: str | Path | None = None) -> int:  # noqa: ANN001
    """Materialize the first task batch for task-less ACTIVE projects.

    Every planner model call is recorded in the invocations ledger (planner
    turns have no task/run row): role/profile/resolved model/effort/skills/
    budget + start/finish + status + usage ("unavailable" when the executor
    does not report it — never fabricated).
    """
    from sqlalchemy import select

    from ..domain.base import utcnow
    from ..domain.invocation import Invocation
    from ..persistence.models import EventRow
    from ..persistence.repositories import EventRepo, InvocationRepo, ProjectRepo, TaskRepo
    from ..executors.base import ExecutorSessionInfo

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
        # execute OUTSIDE the transaction (long-running): build a bounded,
        # persisted planner context first (IMPLEMENTATION.md §13)
        invocation = None
        try:
            from ..application.context_package import ContextPackageBuilder

            with session_factory() as session:
                project_row = ProjectRepo(session).get_by_project_id(project.project_id)
                if project_row is None:
                    continue
                builder = ContextPackageBuilder(session, data_dir=data_dir)
                pkg = builder.persist(builder.planner(project_row))
                context = builder.to_context_dict(
                    pkg, objective=f"为项目 {project.project_id} 规划首批研究任务"
                )
                invocation = Invocation(
                    role="planner",
                    project_id=project.project_id,
                    context_id=pkg.context_id,
                    profile_name=(planner_profile or {}).get("name"),
                    resolved_model=(planner_profile or {}).get("model"),
                    reasoning_effort=(planner_profile or {}).get("reasoning_effort"),
                    skills=list(getattr(executor, "installed_skills", []) or []),
                    budget=dict(getattr(project_row.policy, "default_budget", {}) or {}),
                    status="RUNNING",
                    created_by="scheduler",
                )
                InvocationRepo(session).save(invocation)
                session.commit()
            result, session_info = await executor.run_planner(
                context,
                profile=planner_profile or {},
            )
            finished_ok = True
        except Exception:  # noqa: BLE001
            logger.exception("planner failed for project %s", project.project_id)
            finished_ok = False
            session_info = None
        # invocation ledger close-out (same shape for ok and failed paths)
        with session_factory() as session:
            if invocation is None:
                continue
            inv = InvocationRepo(session).get_by_invocation_id(invocation.invocation_id)
            if inv is not None:
                inv.status = "SUCCEEDED" if finished_ok else "FAILED"
                inv.finished_at = utcnow()
                if not finished_ok:
                    inv.error_message = "planner turn failed"[:500]
                elif isinstance(session_info, ExecutorSessionInfo):
                    if session_info.session_id:
                        inv.metadata = dict(getattr(inv, "metadata", {}) or {})
                        inv.metadata["session_id"] = session_info.session_id
                        inv.metadata["turn_id"] = session_info.turn_id or ""
                # usage: record exactly what the executor reports; explicit
                # unavailable when it reports nothing
                inv.usage = getattr(session_info, "usage", None) if isinstance(session_info, ExecutorSessionInfo) else None
                if inv.usage is None:
                    inv.usage = {"available": False, "reason": "executor does not report usage"}
                InvocationRepo(session).save(inv)
                session.commit()
        if not finished_ok:
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
    """Emit ONE milestone report per real milestone event (idempotent via the
    milestone.reached event key). No evidence-count thresholds: a milestone is
    a deterministic PROJECT milestone — the first completed task, or the
    first applied decision. Reports cite the real task/decision ids."""
    from sqlalchemy import select

    from ..domain.enums import ReportType, TaskStatus
    from ..persistence.models import EventRow, OutboxRow, ReportRow
    from ..persistence.outbox import OutboxStatus
    from ..persistence.repositories import DecisionRepo, ProjectRepo, TaskRepo

    reached = 0
    with session_factory() as session:
        projects = ProjectRepo(session).list_all()
    for project in projects:
        if project.status.value != "ACTIVE":
            continue
        with session_factory() as session:
            milestones = []
            completed = [
                t for t in TaskRepo(session).list_by_status(project.project_id, [])
                if t.status.value == TaskStatus.COMPLETED.value
            ]
            if completed:
                first = min(completed, key=lambda t: t.task_id)
                key = f"milestone:{project.project_id}:first-completed"
                if not session.execute(select(EventRow.id).where(EventRow.idempotency_key == key)).first():
                    milestones.append(
                        (
                            key,
                            "MS-first-completed",
                            f"【MILESTONE】{project.name}：首批研究任务完成\n"
                            f"任务 {first.task_id}「{first.contract.objective[:60]}」已完成并经过独立审查。",
                        )
                    )
            applied = [
                d for d in DecisionRepo(session).list_all_statuses(project.project_id)
                if d.status.value in ("ANSWERED", "APPLIED")
            ]
            if applied:
                first_dec = min(applied, key=lambda d: d.decision_id)
                key = f"milestone:{project.project_id}:first-decision-applied"
                if not session.execute(select(EventRow.id).where(EventRow.idempotency_key == key)).first():
                    milestones.append(
                        (
                            key,
                            "MS-first-decision-applied",
                            f"【MILESTONE】{project.name}：首个科研决策已应用\n"
                            f"决策 {first_dec.decision_id}「{first_dec.question[:60]}」→ {first_dec.answer}",
                        )
                    )
            for key, milestone_id, body in milestones:
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
                            "milestone_id": milestone_id,
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
                            "milestone_id": milestone_id,
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
                        payload={"milestone_id": milestone_id, "body": body},
                    )
                )
                session.commit()
                reached += 1
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
