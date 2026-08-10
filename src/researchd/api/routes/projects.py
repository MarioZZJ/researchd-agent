"""Project routes: create/list/status/pause/resume/cancel/tasks/decisions,
decision answer, commands, sync, reconcile."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...application.handlers import CommandHandler, HandlerError, normalize_inbound
from ...application.commands import UnknownCommand, parse_command
from ...domain.base import Actor, AggregateRef, new_id
from ...domain.enums import ProjectStatus
from ...domain.events import make_event
from ...domain.project import ExecutorPolicy, Project
from ...persistence.repositories import DecisionRepo, EventRepo, ProjectRepo, TaskRepo
from ...persistence.transaction import UnitOfWork
from ..dependencies import get_uow

router = APIRouter(prefix="/v1", tags=["projects"])


class ProjectCreateRequest(BaseModel):
    project_id: str | None = None
    name: str
    description: str = ""
    workspace_root: str | None = None


class CommandRequest(BaseModel):
    text: str
    actor: dict = Field(default_factory=dict)


class DecisionAnswerRequest(BaseModel):
    option_id: str
    version: int | None = None
    actor: str = "pi"


@router.get("/projects")
def list_projects(uow: UnitOfWork = Depends(get_uow)) -> dict:
    projects = []
    for row in ProjectRepo(uow.session).list_all():
        projects.append({"project_id": row.project_id, "name": row.name, "status": row.status})
    return {"projects": projects}


@router.post("/projects")
def create_project(req: ProjectCreateRequest, uow: UnitOfWork = Depends(get_uow)) -> dict:
    repo = ProjectRepo(uow.session)
    if req.project_id and repo.get_by_project_id(req.project_id) is not None:
        raise HTTPException(status_code=409, detail=f"project {req.project_id!r} already exists")
    project = Project(
        project_id=req.project_id or new_id("project"),
        name=req.name,
        description=req.description,
        workspace_root=req.workspace_root,
        policy=ExecutorPolicy(),
    )
    repo.save(project)
    EventRepo(uow.session).append(
        make_event(
            event_type="project.created",
            aggregate=AggregateRef(type="project", id=project.id, version=1),
            idempotency_key=f"project:{project.project_id}:created:v1",
            project_id=project.project_id,
            payload={"name": project.name},
        )
    )
    uow.commit()
    return {"project_id": project.project_id}


def _get_project(uow: UnitOfWork, project_id: str) -> Project:
    project = ProjectRepo(uow.session).get_by_project_id(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {project_id!r} not found")
    return project


@router.get("/projects/{project_id}/status")
def project_status(project_id: str, uow: UnitOfWork = Depends(get_uow)) -> dict:
    project = _get_project(uow, project_id)
    tasks = TaskRepo(uow.session).list_by_status(project_id, [])
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.status.value] = counts.get(t.status.value, 0) + 1
    decisions = DecisionRepo(uow.session).list_open(project_id)
    return {
        "project_id": project.project_id,
        "status": project.status.value,
        "task_counts": counts,
        "open_decisions": [d.decision_id for d in decisions],
    }


@router.post("/projects/{project_id}/pause")
def pause_project(project_id: str, uow: UnitOfWork = Depends(get_uow)) -> dict:
    project = _get_project(uow, project_id)
    project.set_status(ProjectStatus.PAUSED, reason="api")
    ProjectRepo(uow.session).save(project)
    EventRepo(uow.session).append(
        make_event(
            event_type="project.paused",
            aggregate=AggregateRef(type="project", id=project.id, version=project.version),
            idempotency_key=f"project:{project_id}:paused:api:v{project.version}",
            project_id=project_id,
        )
    )
    uow.commit()
    return {"project_id": project_id, "status": project.status.value}


@router.post("/projects/{project_id}/resume")
def resume_project(project_id: str, uow: UnitOfWork = Depends(get_uow)) -> dict:
    project = _get_project(uow, project_id)
    project.set_status(ProjectStatus.ACTIVE)
    ProjectRepo(uow.session).save(project)
    EventRepo(uow.session).append(
        make_event(
            event_type="project.resumed",
            aggregate=AggregateRef(type="project", id=project.id, version=project.version),
            idempotency_key=f"project:{project_id}:resumed:api:v{project.version}",
            project_id=project_id,
        )
    )
    uow.commit()
    return {"project_id": project_id, "status": project.status.value}


@router.post("/projects/{project_id}/cancel")
def cancel_project(project_id: str, uow: UnitOfWork = Depends(get_uow)) -> dict:
    project = _get_project(uow, project_id)
    project.set_status(ProjectStatus.CANCELLED, reason="api")
    ProjectRepo(uow.session).save(project)
    EventRepo(uow.session).append(
        make_event(
            event_type="project.cancelled",
            aggregate=AggregateRef(type="project", id=project.id, version=project.version),
            idempotency_key=f"project:{project_id}:cancelled:api:v{project.version}",
            project_id=project_id,
        )
    )
    uow.commit()
    return {"project_id": project_id, "status": project.status.value}


@router.get("/projects/{project_id}/tasks")
def list_tasks(project_id: str, uow: UnitOfWork = Depends(get_uow)) -> dict:
    _get_project(uow, project_id)
    tasks = TaskRepo(uow.session).list_by_status(project_id, [])
    return {
        "tasks": [
            {
                "task_id": t.task_id,
                "status": t.status.value,
                "objective": t.contract.objective,
                "role": t.contract.role.value if hasattr(t.contract.role, "value") else t.contract.role,
            }
            for t in tasks
        ]
    }


@router.get("/projects/{project_id}/decisions")
def list_decisions(project_id: str, uow: UnitOfWork = Depends(get_uow)) -> dict:
    _get_project(uow, project_id)
    decisions = DecisionRepo(uow.session).list_open(project_id)
    return {
        "decisions": [
            {
                "decision_id": d.decision_id,
                "status": d.status.value,
                "question": d.question,
                "options": [o.option_id for o in d.options],
                "version": d.decision_version,
            }
            for d in decisions
        ]
    }


@router.post("/decisions/{decision_id}/answer")
def answer_decision(decision_id: str, req: DecisionAnswerRequest, uow: UnitOfWork = Depends(get_uow)) -> dict:
    if req.version is not None and req.version <= 0:
        raise HTTPException(status_code=400, detail="version must be a positive integer")
    repo = DecisionRepo(uow.session)
    decision = repo.get_by_decision_id(decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail=f"decision {decision_id!r} not found")
    if decision.status.value != "OPEN":
        return {
            "decision_id": decision_id,
            "applied": False,
            "status": decision.status.value,
            "message": "not open (duplicate answer is a no-op)",
        }
    try:
        decision.apply_answer(req.option_id, actor=req.actor, version=req.version)
    except ValueError as exc:
        uow.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    repo.save(decision)
    EventRepo(uow.session).append(
        make_event(
            event_type="decision.answered",
            aggregate=AggregateRef(type="decision", id=decision.id, version=decision.version),
            idempotency_key=f"decision:{decision_id}:answered:v{decision.version}",
            project_id=decision.project_id,
            payload={"option_id": req.option_id},
        )
    )
    uow.commit()
    return {"decision_id": decision_id, "applied": True, "answer": req.option_id}


@router.post("/projects/{project_id}/commands")
def run_command(project_id: str, req: CommandRequest, uow: UnitOfWork = Depends(get_uow)) -> dict:
    _get_project(uow, project_id)
    try:
        cmd = parse_command(req.text)
    except UnknownCommand as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    handler = CommandHandler(uow.session, project_id=project_id, actor=Actor(**req.actor) if req.actor else Actor(type="human"))
    try:
        reply = handler.dispatch(cmd)
        uow.commit()
    except HandlerError as exc:
        uow.rollback()
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return {"command": cmd.name, "reply": reply.text, "data": reply.data}


@router.post("/projects/{project_id}/sync")
def sync_project(project_id: str, uow: UnitOfWork = Depends(get_uow)) -> dict:
    _get_project(uow, project_id)
    from ...persistence.outbox import OutboxRepo
    from ...domain.base import utcnow

    OutboxRepo(uow.session).enqueue(
        destination="delivery",
        idempotency_key=f"sync:{project_id}:{utcnow().isoformat()}",
        payload={"kind": "projection_sync", "project_id": project_id},
        project_id=project_id,
    )
    uow.commit()
    return {"project_id": project_id, "scheduled": True}


@router.post("/reconcile")
def reconcile(uow: UnitOfWork = Depends(get_uow)) -> dict:
    """Manual reconciliation trigger (orphan recovery runs in the scheduler loop)."""
    from ...domain.base import utcnow
    from ...persistence.repositories import RunRepo

    runs = RunRepo(uow.session).list_active()
    orphaned = [r.run_id for r in runs if r.heartbeat_at is None or (utcnow() - r.heartbeat_at).total_seconds() > 300]
    uow.commit()
    return {"active_runs": len(runs), "suspected_orphans": orphaned}
