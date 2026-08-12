"""Project routes: create/list/status/pause/resume/cancel/tasks/decisions,
decision answer, commands, sync, reconcile."""

from __future__ import annotations

import os
import re
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


_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@router.post("/projects", dependencies=[Depends(require_token)])
def create_project(req: ProjectCreateRequest, request: Request, uow: UnitOfWork = Depends(get_uow)) -> dict:
    repo = ProjectRepo(uow.session)
    actor = (req.actor or "").strip()
    if not actor or len(actor) > 128:
        raise HTTPException(status_code=400, detail="actor is required (non-empty platform user id)")
    project_id = req.project_id or new_id("project")
    if not _PROJECT_ID_RE.fullmatch(project_id) or len(project_id) > 64:
        raise HTTPException(status_code=400, detail="project_id must be a single safe path segment (<=64 chars)")
    if repo.get_by_project_id(project_id) is not None:
        raise HTTPException(status_code=409, detail=f"project {project_id!r} already exists")
    # workspace_root is service-derived: ALWAYS <data_dir>/workspaces/<project_id>.
    # The anchor itself is verified BEFORE resolving: mkdir, then O_NOFOLLOW
    # lstat-style check — a pre-planted symlink at <data_dir>/workspaces must
    # never become the trusted root.
    import logging

    logger = logging.getLogger("researchd.api")

    def _workspace_bad_request(context: str) -> None:
        logger.warning("workspace_root rejected (%s) for project %s by actor %s", context, project_id, actor)

    allowed_anchor = Path(request.app.state.settings.data_dir) / "workspaces"
    try:
        allowed_anchor.mkdir(parents=True, exist_ok=True)
    except OSError:
        _workspace_bad_request("anchor mkdir failed")
        raise HTTPException(status_code=400, detail="workspaces anchor unavailable") from None
    try:
        fd = os.open(allowed_anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        os.close(fd)
    except OSError:
        _workspace_bad_request("anchor is not a real directory")
        raise HTTPException(status_code=400, detail="workspaces anchor is not a real directory") from None
    try:
        allowed_root = allowed_anchor.resolve()
    except OSError:
        _workspace_bad_request("anchor resolve failed")
        raise HTTPException(status_code=400, detail="workspaces anchor unavailable") from None

    def _no_symlink_components(path: Path) -> bool:
        """Every component under allowed_root must be a real directory (lstat,
        BEFORE any resolve() that would follow a symlink)."""
        try:
            rel = path.relative_to(allowed_root)
        except ValueError:
            return False
        cur = allowed_root
        for part in rel.parts:
            cur = cur / part
            if cur.is_symlink():
                return False
        return True

    candidate = allowed_root / project_id  # un-resolved: symlink check is meaningful
    if req.workspace_root:
        requested = Path(req.workspace_root)
        # lexical checks FIRST: no '..' components, absolute, under candidate
        if not requested.is_absolute() or ".." in requested.parts:
            raise HTTPException(status_code=400, detail="workspace_root must be an absolute path without '..'")
        # symlink check on the un-resolved path (before resolve follows links)
        if not _no_symlink_components(requested):
            raise HTTPException(status_code=400, detail="workspace_root path must not traverse symlinks")
        normalized = requested.resolve()  # canonicalize (no symlinks remain)
        if not normalized.is_relative_to(candidate.resolve()):
            raise HTTPException(status_code=400, detail="workspace_root must be under <data_dir>/workspaces/<project_id>")
        workspace_root = str(normalized)
    else:
        if not _no_symlink_components(candidate):
            raise HTTPException(status_code=400, detail="workspace_root path must not traverse symlinks")
        workspace_root = str(candidate.resolve())
    # create the root with a no-follow guard: every component is created with
    # mkdir (never following an existing symlink) and then verified with
    # O_DIRECTORY|O_NOFOLLOW. The remaining check-to-use window is a same-uid
    # race that only OS-level isolation can close (blocker B-08).
    root = Path(workspace_root)
    cur = allowed_root
    for part in root.relative_to(allowed_root).parts:
        cur = cur / part
        try:
            os.mkdir(cur)
        except FileExistsError:
            pass
        try:
            fd = os.open(cur, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            os.close(fd)
        except OSError:
            logger.warning("workspace_root component not a real directory: %s", cur)
            raise HTTPException(status_code=400, detail="workspace_root component is not a real directory") from None
    resolved_root = root.resolve()
    if not resolved_root.is_relative_to(allowed_root):
        raise HTTPException(status_code=400, detail="workspace_root escaped the allowed root")
    workspace_root = str(resolved_root)
    project = Project(
        project_id=project_id,
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
            id=f"PM-{project.project_id}-{actor}",
            member_id=f"PM-{project.project_id}-{actor}",
            project_id=project.project_id,
            platform_user_id=actor,
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


@router.get("/projects/{project_id}/members/{user_id}", dependencies=[Depends(require_token)])
def check_membership(project_id: str, user_id: str, uow: UnitOfWork = Depends(get_uow)) -> dict:
    """Membership check for the ACP bind path (fail-closed: a project with no
    member rows refuses everything; a non-member gets 403)."""
    from sqlalchemy import select

    from ...persistence.models import ProjectMemberRow

    if not _PROJECT_ID_RE.fullmatch(project_id) or len(user_id) > 128:
        raise HTTPException(status_code=400, detail="bad project_id or user_id")
    rows = uow.session.execute(
        select(ProjectMemberRow).where(ProjectMemberRow.project_id == project_id)
    ).scalars().all()
    if not rows:
        raise HTTPException(status_code=403, detail="project has no members provisioned")
    if any(r.platform_user_id == user_id for r in rows):
        return {"project_id": project_id, "member": True}
    raise HTTPException(status_code=403, detail="not a member")


class ProjectMemberRequest(BaseModel):
    platform_user_id: str = Field(min_length=1, max_length=128)
    role: str = "member"  # member | pi
    can_approve_decisions: bool = False


@router.post("/projects/{project_id}/members", dependencies=[Depends(require_token)])
def add_member(project_id: str, req: ProjectMemberRequest, uow: UnitOfWork = Depends(get_uow)) -> dict:
    """Provision a project member (idempotent). The only writer path for
    membership besides project creation (fail-closed gate §22)."""
    from sqlalchemy import select

    from ...persistence.models import ProjectMemberRow

    if not _PROJECT_ID_RE.fullmatch(project_id):
        raise HTTPException(status_code=400, detail="bad project_id")
    project = ProjectRepo(uow.session).get_by_project_id(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {project_id!r} not found")
    existing = uow.session.execute(
        select(ProjectMemberRow).where(
            ProjectMemberRow.project_id == project_id,
            ProjectMemberRow.platform_user_id == req.platform_user_id,
        )
    ).scalars().first()
    if existing is not None:
        return {"project_id": project_id, "platform_user_id": req.platform_user_id, "added": False}
    uow.session.add(
        ProjectMemberRow(
            id=f"PM-{project_id}-{req.platform_user_id[:24]}",
            member_id=f"PM-{project_id}-{req.platform_user_id[:24]}",
            project_id=project_id,
            platform_user_id=req.platform_user_id,
            role=req.role,
            can_approve_decisions=req.can_approve_decisions,
        )
    )
    uow.commit()
    return {"project_id": project_id, "platform_user_id": req.platform_user_id, "added": True}


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
    # in-place card update: the already-sent decision card (if any) is
    # PATCHed to show the recorded answer — no meaningless new card (shared
    # application flow: the ACP/button path goes through the same helper)
    from ...application.handlers import _enqueue_decision_card_update

    _enqueue_decision_card_update(uow.session, decision, req.actor, req.option_id)
    uow.commit()
    return {"decision_id": decision_id, "applied": True, "answer": req.option_id}


class DecisionEvidenceLinkRequest(BaseModel):
    evidence_id: str


@router.post("/decisions/{decision_id}/evidence", dependencies=[Depends(require_token)])
def link_decision_evidence(decision_id: str, req: DecisionEvidenceLinkRequest, uow: UnitOfWork = Depends(get_uow)) -> dict:
    """Append a real evidence id to a decision's evidence_refs (idempotent).

    The reporter's linter requires a decision card's bottom line to cite REAL
    evidence; a bootstrap-OPEN decision (pilot D-002) needs the pilot's first
    VERIFIED evidence linked before its card can be sent. Token-gated; the
    evidence row itself must exist (fail-closed — the linter re-checks at
    report time anyway).
    """
    from ...persistence.repositories import EvidenceRepo

    repo = DecisionRepo(uow.session)
    decision = repo.get_by_decision_id(decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail=f"decision {decision_id!r} not found")
    evidence = EvidenceRepo(uow.session).get_by_evidence_id(req.evidence_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail=f"evidence {req.evidence_id!r} not found")
    if evidence.project_id != decision.project_id:
        raise HTTPException(
            status_code=400,
            detail=f"evidence {req.evidence_id!r} belongs to project {evidence.project_id}, "
            f"not {decision.project_id}",
        )
    refs = list(decision.evidence_refs or [])
    if req.evidence_id in refs:
        uow.commit()
        return {"decision_id": decision_id, "evidence_refs": refs, "applied": False}
    refs.append(req.evidence_id)
    decision.evidence_refs = refs
    repo.save(decision)
    EventRepo(uow.session).append(
        make_event(
            event_type="decision.evidence_linked",
            aggregate=AggregateRef(type="decision", id=decision.id, version=decision.version),
            idempotency_key=f"decision:{decision_id}:evidence:{req.evidence_id}",
            project_id=decision.project_id,
            payload={"evidence_id": req.evidence_id},
        )
    )
    uow.commit()
    return {"decision_id": decision_id, "evidence_refs": refs, "applied": True}


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
