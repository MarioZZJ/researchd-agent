"""Model-call invocation (IMPLEMENTATION.md §13): every planner/worker/auditor
executor turn is recorded so model usage is traceable — profile, resolved
model, reasoning effort, skills, budget, start/finish, status and usage
(explicitly "unavailable" when the executor does not report it — never
fabricated). Worker/auditor turns also carry runs rows; planner turns exist
ONLY here (they have no task/run)."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .base import DomainModel, new_id, utcnow


class Invocation(DomainModel):
    invocation_id: str = Field(default_factory=lambda: new_id("invocation"))
    role: str = "planner"  # planner | worker | auditor
    project_id: str
    task_id: str | None = None
    run_id: str | None = None
    context_id: str | None = None
    profile_name: str | None = None
    resolved_model: str | None = None
    reasoning_effort: str | None = None
    skills: list[Any] = Field(default_factory=list)
    budget: dict = Field(default_factory=dict)
    started_at: Any | None = None  # datetime or ISO string (row stores datetime)
    finished_at: Any | None = None
    status: str = "RUNNING"  # RUNNING|SUCCEEDED|FAILED|INTERRUPTED
    error_message: str | None = None
    usage: dict | None = None  # {"available": False, ...} when unknown

    def __init__(self, **data):
        data.setdefault("id", data.get("invocation_id") or new_id("invocation"))
        data.setdefault("started_at", utcnow())
        super().__init__(**data)
