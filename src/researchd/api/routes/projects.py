"""Project routes: create/list/status/pause/resume/cancel/tasks/decisions,
decision answer, commands, sync, reconcile."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from ..dependencies import require_token
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...application.handlers import CommandHandler, HandlerError, _require_actor_authorized, normalize_inbound
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
    workspace_root: str | None = None  # must resolve under <data_dir>/workspaces
    actor: str  # gateway-declared platform user id of the creator (owner)


class CommandRequest(BaseModel):
    text: str
    actor: dict  # gateway-declared identity; required (fail-closed)


class DecisionAnswerRequest(BaseModel):
    option_id: str
    version: int | None = None  # required; missing -> 400 (fingerprint guard)
    actor: str  # gateway-declared platform user id; NO default (fail-closed)


@router.get("/projects", dependencies=[Depends(require_token)])
def list_projects(uow: UnitOfWork = Depends(get_uow)) -> dict:
    projects = []
    for row in ProjectRepo(uow.session).list_all():
        projects.append({"project_id": row.project_id, "name": row.name, "status": row.status})
    return {"projects": projects}


@router.post("/projects", dependencies=[Depends(require_token)])
def create_project(req: ProjectCreateRequest, request: Request, uow: UnitOfWork = Depends(get_uow)) -> dict:
    repo = ProjectRepo(uow.session)
    if req.project_id and repo.get_by_project_id(req.project_id) is not None:
        raise HTTPException(status_code=409, detail=f"project {req.project_id!r} already exists")
    # workspace_root is service-derived: must resolve under <data_dir>/workspaces
    allowed_root = Path(request.app.state.settings.data_dir) / "workspaces"
    if req.workspace_root:
        resolved = Path(req.workspace_root).resolve()
        if not str(resolved).startswith(str(allowed_root.resolve()) + "/"):
            raise HTTPException(status_code=400, detail="workspace_root must be under <data_dir>/workspaces")
        workspace_root = str(resolved)
    else:
        workspace_root = str((allowed_root / (req.project_id or new_id("project"))).resolve())
    project = Project(
        project_id=req.project_id or new_id("project"),
        name=req.name,
        description=req.description,
        workspace_root=workspace_root,
        policy=ExecutorPolicy(),
    )
    repo.save(project)
    # creator becomes the owner member (fail-closed membership gate §22)
    from ...persistence.models import ProjectMemberRow

    uow.session.add(
        ProjectMemberRow(
            id=f"PM-{project.project_id}-{req.actor}",
            member_id=f"PM-{project.project_id}-{req.actor}",
            project_id=project.project_id,
            platform_user_id=req.actor,
            role="owner",
            can_approve_decisions=True,
        )
    )
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


@router.get("/projects/{project_id}/status", dependencies=[Depends(require_token)])
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


@router.post("/projects/{project_id}/pause", dependencies=[Depends(require_token)])
def pause_project(project_id: str, actor: str, uow: UnitOfWork = Depends(get_uow)) -> dict:
    project = _get_project(uow, project_id)
    _require_actor_authorized(uow.session, project_id, Actor(type="human", platform_user_id=actor))
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


@router.post("/projects/{project_id}/resume", dependencies=[Depends(require_token)])
def resume_project(project_id: str, actor: str, uow: UnitOfWork = Depends(get_uow)) -> dict:
    project = _get_project(uow, project_id)
    _require_actor_authorized(uow.session, project_id, Actor(type="human", platform_user_id=actor))
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


@router.post("/projects/{project_id}/cancel", dependencies=[Depends(require_token)])
def cancel_project(project_id: str, actor: str, uow: UnitOfWork = Depends(get_uow)) -> dict:
    project = _get_project(uow, project_id)
    _require_actor_authorized(uow.session, project_id, Actor(type="human", platform_user_id=actor))
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


@router.get("/projects/{project_id}/tasks", dependencies=[Depends(require_token)])
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


@router.get("/projects/{project_id}/decisions", dependencies=[Depends(require_token)])
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


@router.post("/decisions/{decision_id}/answer", dependencies=[Depends(require_token)])
def answer_decision(decision_id: str, req: DecisionAnswerRequest, uow: UnitOfWork = Depends(get_uow)) -> dict:
    if req.version is None:
        raise HTTPException(status_code=400, detail="version is required (decision fingerprint)")
    if req.version <= 0:
        raise HTTPException(status_code=400, detail="version must be a positive integer")
    repo = DecisionRepo(uow.session)
    decision = repo.get_by_decision_id(decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail=f"decision {decision_id!r} not found")
    # membership + approval gate (IMPLEMENTATION.md §22): the answering actor
    # must be a project member with can_approve_decisions once members exist
    try:
        _require_actor_authorized(
            uow.session, decision.project_id, Actor(type="human", platform_user_id=req.actor),
            require_approval=True,
        )
    except HandlerError as exc:
        uow.rollback()
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
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


@router.post("/projects/{project_id}/commands", dependencies=[Depends(require_token)])
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


@router.post("/projects/{project_id}/sync", dependencies=[Depends(require_token)])
def sync_project(project_id: str, actor: str, uow: UnitOfWork = Depends(get_uow)) -> dict:
    project = _get_project(uow, project_id)
    _require_actor_authorized(uow.session, project_id, Actor(type="human", platform_user_id=actor))
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


@router.post("/reconcile", dependencies=[Depends(require_token)])
def reconcile(uow: UnitOfWork = Depends(get_uow)) -> dict:
    """Manual reconciliation trigger (orphan recovery runs in the scheduler loop)."""
    from ...domain.base import utcnow
    from ...persistence.repositories import RunRepo

    runs = RunRepo(uow.session).list_active()
    orphaned = [r.run_id for r in runs if r.heartbeat_at is None or (utcnow() - r.heartbeat_at).total_seconds() > 300]
    uow.commit()
    return {"active_runs": len(runs), "suspected_orphans": orphaned}
