"""Application handlers: the ONLY execution layer for commands and inbound
messages. Runs inside `researchd service` (the sole database writer).

Every handler is idempotent: it records the processed message/command by
idempotency key before executing, and replays are no-ops.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from ..domain.base import Actor, AggregateRef, new_id, utcnow
from ..domain.decision import DecisionVersionMismatch, UnknownOptionError
from ..domain.enums import InboundPlatform, ProjectStatus, TaskStatus
from ..domain.events import make_event
from ..domain.project import Project
from ..persistence.models import InboundMessageRow, ProjectMemberRow, ProjectRow
from ..persistence.outbox import OutboxRepo
from ..persistence.repositories import (
    ClaimRepo,
    DecisionRepo,
    EventRepo,
    ProjectRepo,
    TaskRepo,
)
from .commands import ParsedCommand, UnknownCommand, parse_command


@dataclass
class CommandReply:
    text: str
    data: dict[str, Any] | None = None


class HandlerError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _parse_version_flag(value) -> int | None:  # noqa: ANN001
    """--version must be an explicit positive integer when present (a bare
    `--version` with no value is rejected, not silently ignored)."""
    if value is None:
        return None
    if value is True:
        raise HandlerError("--version requires a positive integer value")
    try:
        v = int(str(value))
    except (TypeError, ValueError) as exc:
        raise HandlerError("--version must be a positive integer") from exc
    if v <= 0:
        raise HandlerError("--version must be a positive integer")
    return v


def normalize_inbound(
    *,
    message_id: str,
    platform: str = InboundPlatform.FEISHU.value,
    cc_project: str | None = None,
    cc_session_key: str | None = None,
    actor: Actor | None = None,
    text: str = "",
    attachments: list | None = None,
    received_at: datetime | None = None,
) -> dict:
    """Normalize an inbound platform message (researchd.inbound_message.v1)."""
    actor_dict = None
    if actor is not None:
        actor_dict = actor.model_dump() if hasattr(actor, "model_dump") else dict(actor)
    return {
        "schema": "researchd.inbound_message.v1",
        "message_id": message_id,
        "idempotency_key": f"{platform}:{message_id}",
        "platform": platform,
        "cc_project": cc_project,
        "cc_session_key": cc_session_key,
        "actor": actor_dict or {"type": "human", "platform_user_id": "unknown"},
        "text": text,
        "attachments": attachments or [],
        "received_at": (received_at or utcnow()).isoformat(),
    }


def _event_repo(session: Session) -> EventRepo:
    return EventRepo(session)


def _record_inbound(session: Session, msg: dict) -> bool:
    """Atomically record the message; returns True if NEW (to be processed).

    INSERT OR IGNORE on the unique idempotency_key: concurrent duplicate
    delivery resolves to a no-op instead of a 500 (IMPLEMENTATION.md §25.3).
    """
    result = session.execute(
        insert(InboundMessageRow)
        .values(
            id=new_id("other"),
            message_id=msg["message_id"],
            idempotency_key=msg["idempotency_key"],
            platform=msg["platform"],
            cc_project=msg.get("cc_project"),
            cc_session_key=msg.get("cc_session_key"),
            actor_json=msg.get("actor"),
            text=msg.get("text", ""),
            attachments_json=msg.get("attachments"),
            received_at=datetime.fromisoformat(msg["received_at"]),
        )
        .on_conflict_do_nothing(index_elements=[InboundMessageRow.idempotency_key])
    )
    return result.rowcount == 1


def _require_actor_authorized(
    session: Session, project_id: str, actor: Actor, *, require_approval: bool = False
) -> None:
    """Gate project-mutating actions on project membership (IMPLEMENTATION.md
    §22, §25.8). Degradation rule: when the project has NO member rows yet
    (bootstrap), any actor is allowed; once members exist, the actor must be a
    member (and have can_approve_decisions for decision answers)."""
    rows = session.execute(
        select(ProjectMemberRow).where(ProjectMemberRow.project_id == project_id)
    ).scalars().all()
    if not rows:
        return  # bootstrap phase
    user_id = actor.platform_user_id
    for row in rows:
        if row.platform_user_id == user_id:
            if require_approval and not row.can_approve_decisions:
                raise HandlerError(f"actor {user_id!r} cannot approve decisions for project {project_id}", 403)
            return
    raise HandlerError(f"actor {user_id!r} is not a member of project {project_id}", 403)


class CommandHandler:
    """Executes parsed commands against repositories. One instance per request
    (session-scoped)."""

    def __init__(self, session: Session, *, project_id: str | None = None, actor: Actor | None = None):
        self.session = session
        self.project_id = project_id
        self.actor = actor or Actor(type="human")

    # ------------------------------------------------------------ helpers
    def _require_project(self) -> Project:
        if not self.project_id:
            raise HandlerError("no project bound; use /research bind project <id>", 400)
        project = ProjectRepo(self.session).get_by_project_id(self.project_id)
        if project is None:
            raise HandlerError(f"project {self.project_id!r} not found", 404)
        return project

    def _emit(self, event_type: str, aggregate: AggregateRef, *, payload: dict | None = None) -> None:
        _event_repo(self.session).append(
            make_event(
                event_type=event_type,
                aggregate=aggregate,
                idempotency_key=f"{aggregate.type}:{aggregate.id}:{event_type}:v{aggregate.version}",
                project_id=self.project_id,
                actor=self.actor,
                payload=payload,
            )
        )

    # ------------------------------------------------------------ commands
    def dispatch(self, cmd: ParsedCommand) -> CommandReply:
        method = getattr(self, f"cmd_{cmd.name}", None)
        if method is None:
            raise UnknownCommand(cmd.raw)
        return method(cmd)

    def cmd_status(self, cmd: ParsedCommand) -> CommandReply:
        project = self._require_project()
        task_repo = TaskRepo(self.session)
        counts: dict[str, int] = {}
        for status in TaskStatus:
            counts[status.value] = len(task_repo.list_by_status(project.project_id, [status.value]))
        decisions = DecisionRepo(self.session).list_open(project.project_id)
        return CommandReply(
            text=(
                f"project {project.project_id} [{project.status.value}] tasks={counts} "
                f"open_decisions={len(decisions)}"
            ),
            data={"project_id": project.project_id, "status": project.status.value, "tasks": counts},
        )

    def cmd_bind(self, cmd: ParsedCommand) -> CommandReply:
        kind = cmd.args[0]
        if kind == "project":
            if len(cmd.args) < 2:
                raise HandlerError("usage: /research bind project <project-id>")
            self.project_id = cmd.args[1]
            project = self._require_project()
            return CommandReply(f"bound to project {project.project_id}", {"project_id": project.project_id})
        if kind == "inbox":
            return CommandReply("PI inbox binding recorded (requires cc-connect delivery config)")
        raise HandlerError(f"unknown bind kind {kind!r}")

    def cmd_pause(self, cmd: ParsedCommand) -> CommandReply:
        project = self._require_project()
        _require_actor_authorized(self.session, project.project_id, self.actor)
        project.set_status(ProjectStatus.PAUSED, reason="user command")
        ProjectRepo(self.session).save(project)
        self._emit(
            "project.paused",
            AggregateRef(type="project", id=project.id, version=project.version),
        )
        return CommandReply(f"project {project.project_id} paused")

    def cmd_resume(self, cmd: ParsedCommand) -> CommandReply:
        project = self._require_project()
        _require_actor_authorized(self.session, project.project_id, self.actor)
        project.set_status(ProjectStatus.ACTIVE)
        ProjectRepo(self.session).save(project)
        self._emit(
            "project.resumed",
            AggregateRef(type="project", id=project.id, version=project.version),
        )
        return CommandReply(f"project {project.project_id} resumed")

    def cmd_digest(self, cmd: ParsedCommand) -> CommandReply:
        project = self._require_project()
        # Digest content is compiled by the reporter (Phase 6); here we only
        # schedule the digest outbox notification.
        OutboxRepo(self.session).enqueue(
            destination="delivery",
            idempotency_key=f"digest:{project.project_id}:{utcnow().isoformat()}",
            payload={"kind": "digest", "project_id": project.project_id},
            project_id=project.project_id,
        )
        return CommandReply("digest scheduled")

    def cmd_sync(self, cmd: ParsedCommand) -> CommandReply:
        project = self._require_project()
        OutboxRepo(self.session).enqueue(
            destination="delivery",
            idempotency_key=f"sync:{project.project_id}:{utcnow().isoformat()}",
            payload={"kind": "projection_sync", "project_id": project.project_id},
            project_id=project.project_id,
        )
        return CommandReply("projection sync scheduled")

    def cmd_model(self, cmd: ParsedCommand) -> CommandReply:
        """Interaction profile is session-level and is managed by the ACP shim
        (InteractionSession), never persisted into the project policy."""
        project = self._require_project()
        if not cmd.args:
            return CommandReply(
                "interaction profile is a session-level setting (fast|deep|deterministic); "
                "it never changes the project execution policy. "
                "Use /research model interaction <profile>."
            )
        if cmd.args[0] != "interaction" or len(cmd.args) != 2 or cmd.args[1] not in ("fast", "deep", "deterministic"):
            raise HandlerError("usage: /research model interaction fast|deep|deterministic")
        return CommandReply(
            f"interaction profile request: {cmd.args[1]} (applied to this session by the client)"
        )

    def cmd_config(self, cmd: ParsedCommand) -> CommandReply:
        project = self._require_project()
        if cmd.args[0] == "show":
            return CommandReply(
                f"role_overrides={json.dumps(project.policy.role_overrides)} "
                f"(execution policy; interaction profile is session-level)"
            )
        if cmd.args[0] == "set":
            if len(cmd.args) != 3 or not cmd.args[1].startswith("role."):
                raise HandlerError("usage: /research config set role.<role> <profile>")
            role = cmd.args[1][len("role."):]
            profile = cmd.args[2]
            project.policy.role_overrides[role] = profile
            ProjectRepo(self.session).save(project)
            self._emit(
                "project.executor_policy_changed",
                AggregateRef(type="project", id=project.id, version=project.version),
                payload={"role": role, "profile": profile},
            )
            return CommandReply(f"role {role} -> profile {profile} (affects future runs only)")
        raise HandlerError(f"unknown config subcommand {cmd.args[0]!r}")

    def cmd_decision(self, cmd: ParsedCommand) -> CommandReply:
        decision_id, option_id = cmd.args
        version = _parse_version_flag(cmd.flags.get("--version"))
        repo = DecisionRepo(self.session)
        decision = repo.get_by_decision_id(decision_id)
        if decision is None:
            raise HandlerError(f"decision {decision_id!r} not found", 404)
        if decision.project_id:
            _require_actor_authorized(
                self.session, decision.project_id, self.actor, require_approval=True
            )
        if decision.status.value not in ("OPEN",):
            return CommandReply(
                f"decision {decision_id} already {decision.status.value} (answer={decision.answer}); "
                "duplicate click is a no-op",
                {"decision_id": decision_id, "status": decision.status.value, "applied": False},
            )
        try:
            decision.apply_answer(option_id, actor=str(self.actor.platform_user_id or self.actor.type), version=version)
        except DecisionVersionMismatch as exc:
            raise HandlerError(str(exc), 409) from exc
        except UnknownOptionError as exc:
            raise HandlerError(str(exc), 400) from exc
        repo.save(decision)
        self._emit(
            "decision.answered",
            AggregateRef(type="decision", id=decision.id, version=decision.version),
            payload={"option_id": option_id, "version": version},
        )
        return CommandReply(
            f"decision {decision_id} answered {option_id}",
            {"decision_id": decision_id, "answer": option_id, "applied": True},
        )

    def cmd_explain(self, cmd: ParsedCommand) -> CommandReply:
        obj_id = cmd.args[0]
        return CommandReply(_explain_object(self.session, obj_id))

    def cmd_task(self, cmd: ParsedCommand) -> CommandReply:
        task = TaskRepo(self.session).get_by_task_id(cmd.args[0])
        if task is None:
            raise HandlerError(f"task {cmd.args[0]!r} not found", 404)
        return CommandReply(
            f"task {task.task_id} [{task.status.value}] objective={task.contract.objective[:120]}",
            {"task_id": task.task_id, "status": task.status.value},
        )

    def cmd_claim(self, cmd: ParsedCommand) -> CommandReply:
        claim = ClaimRepo(self.session).get_by_claim_id(cmd.args[0])
        if claim is None:
            raise HandlerError(f"claim {cmd.args[0]!r} not found", 404)
        return CommandReply(
            f"claim {claim.claim_id} evidence={claim.evidence_state.value} review={claim.review_level.value} "
            f"use={claim.use_state.value}",
            {"claim_id": claim.claim_id},
        )


def _explain_object(session: Session, obj_id: str) -> str:
    """Explain an object id from its prefix: D-* decision, T-* task, E-* evidence, C-* claim, I-* issue."""
    prefix = obj_id.split("-", 1)[0].upper()
    if prefix == "D":
        d = DecisionRepo(session).get_by_decision_id(obj_id)
        if d is None:
            return f"decision {obj_id}: not found"
        return (
            f"decision {obj_id} [{d.status.value}] Q: {d.question[:200]} options="
            f"{[o.option_id for o in d.options]}"
        )
    if prefix == "T":
        t = TaskRepo(session).get_by_task_id(obj_id)
        if t is None:
            return f"task {obj_id}: not found"
        return f"task {obj_id} [{t.status.value}] objective={t.contract.objective[:200]}"
    if prefix == "E":
        from ..persistence.repositories import EvidenceRepo

        e = EvidenceRepo(session).get_by_evidence_id(obj_id)
        if e is None:
            return f"evidence {obj_id}: not found"
        return f"evidence {obj_id} [{e.status.value}] {e.statement[:200]}"
    if prefix == "C":
        c = ClaimRepo(session).get_by_claim_id(obj_id)
        if c is None:
            return f"claim {obj_id}: not found"
        return f"claim {obj_id} [{c.evidence_state.value}] {c.text[:200]}"
    if prefix == "I":
        from ..persistence.repositories import IssueRepo

        i = IssueRepo(session).get_by_issue_id(obj_id)
        if i is None:
            return f"issue {obj_id}: not found"
        return f"issue {obj_id} [{i.status.value}] {i.title[:200]}"
    return f"{obj_id}: unknown object type"


def handle_inbound(session: Session, msg: dict, *, fallback_project: str | None = None) -> CommandReply:
    """Process one normalized inbound message. Idempotent by message id."""
    if not _record_inbound(session, msg):
        # duplicate: return current state without re-executing
        return CommandReply("duplicate message ignored (already processed)")
    text = msg.get("text", "")
    project_id = msg.get("cc_project") or fallback_project
    actor = Actor(**(msg.get("actor") or {})) if msg.get("actor") else Actor(type="human")
    try:
        cmd = parse_command(text)
    except UnknownCommand as exc:
        raise HandlerError(f"unrecognized command: {exc}", 400) from exc
    handler = CommandHandler(session, project_id=project_id, actor=actor)
    return handler.dispatch(cmd)
