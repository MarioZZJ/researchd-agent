"""Run domain object (IMPLEMENTATION.md §7.2, §14).

A Run is one executor invocation of a Task. SUCCEEDED only means the executor
returned normally and passed basic schema validation — never scientific acceptance.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from .base import DomainModel, new_id, utcnow
from .enums import RunStatus
from .state_machine import RunStateMachine


class Run(DomainModel):
    run_id: str = Field(default_factory=lambda: new_id("run"))
    task_id: str
    status: RunStatus = RunStatus.QUEUED

    # resolved configuration (frozen at dispatch, IMPLEMENTATION.md §15.1)
    executor: str | None = None
    executor_profile: str | None = None
    resolved_model: str | None = None
    reasoning_effort: str | None = None
    configuration_source: str | None = None

    # process/session provenance (§14)
    process_instance_id: str | None = None
    session_id: str | None = None  # reasonix session id / codex thread id
    turn_id: str | None = None
    last_event_sequence: int | None = None
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    termination_reason: str | None = None
    lease_token: str | None = None

    # outcome
    outcome: str | None = None  # WorkOutcome
    result: dict[str, Any] | None = None  # validated WorkResult/PlannerResult
    error_message: str | None = None
    usage: dict | None = None  # token usage when the executor reports it;
    #  {"available": False, "reason": "..."} when it does not — never fabricated
    repair_attempts: int = 0

    def __init__(self, **data):
        data.setdefault("id", data.get("run_id") or new_id("run"))
        super().__init__(**data)

    @field_validator("status", mode="before")
    @classmethod
    def _coerce(cls, v):
        return RunStatus(v) if v and not isinstance(v, RunStatus) else v

    def transition(self, target: RunStatus | str) -> RunStatus:
        target = RunStatus(target) if isinstance(target, str) else target
        self.status = RunStateMachine("run", self.status).transition(target)
        self.updated_at = utcnow()
        return self.status

    def heartbeat(self) -> None:
        self.heartbeat_at = utcnow()
        self.updated_at = utcnow()
