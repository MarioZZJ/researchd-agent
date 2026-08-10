"""Project domain object and binding/policy configuration."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .base import DomainModel, new_id, utcnow
from .enums import ProjectStatus


class ProjectBinding(BaseModel):
    """cc-connect binding: which project session keys receive reports."""

    model_config = {"extra": "forbid"}

    binding_id: str = Field(default_factory=lambda: new_id("other"))
    project_id: str
    kind: str = "project_group"  # project_group | pi_inbox
    cc_project: str | None = None
    session_key: str | None = None
    chat_id: str | None = None
    enabled: bool = True


class ProjectMember(BaseModel):
    model_config = {"extra": "forbid"}

    member_id: str = Field(default_factory=lambda: new_id("other"))
    project_id: str
    platform_user_id: str
    display_name: str | None = None
    role: str = "member"  # pi | member
    can_approve_decisions: bool = False


class ExecutorPolicy(BaseModel):
    """Per-project role -> profile overrides; only affects FUTURE runs.
    Interaction profile (fast|deep|deterministic) is SESSION-level and never
    stored here (IMPLEMENTATION.md §15.3)."""

    model_config = {"extra": "forbid"}

    role_overrides: dict[str, str] = Field(default_factory=dict)
    default_budget: dict = Field(default_factory=dict)


class Project(DomainModel):
    project_id: str = Field(default_factory=lambda: new_id("project"))
    name: str
    description: str = ""
    status: ProjectStatus = ProjectStatus.ACTIVE
    workspace_root: str | None = None  # filesystem path, resolved at runtime
    policy: ExecutorPolicy = Field(default_factory=ExecutorPolicy)
    paused_reason: str | None = None
    initial_brief_hash: str | None = None

    def __init__(self, **data):
        data.setdefault("id", data.get("project_id") or new_id("project"))
        super().__init__(**data)

    @field_validator("status", mode="before")
    @classmethod
    def _coerce(cls, v):
        return ProjectStatus(v) if v and not isinstance(v, ProjectStatus) else v

    def set_status(self, status: ProjectStatus, reason: str = "") -> ProjectStatus:
        self.status = status
        self.paused_reason = reason or None
        self.updated_at = utcnow()
        return self.status


class Question(DomainModel):
    question_id: str = Field(default_factory=lambda: new_id("question"))
    text: str
    is_core: bool = False
    status: str = "OPEN"

    def __init__(self, **data):
        data.setdefault("id", data.get("question_id") or new_id("question"))
        super().__init__(**data)
