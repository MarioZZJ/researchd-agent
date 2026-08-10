"""ContextPackage, PlanRevision, TasteRuleProposal (IMPLEMENTATION.md §13, §21.4)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .base import DomainModel, new_id


class ContextObjectRef(BaseModel):
    model_config = {"extra": "forbid"}

    kind: str
    id: str
    summary: str | None = None


class ContextPackage(DomainModel):
    """Bounded context handed to a Planner/Worker/Auditor. Never the whole chat."""

    context_id: str = Field(default_factory=lambda: new_id("context_package"))
    task_id: str | None = None
    objects: list[ContextObjectRef] = Field(default_factory=list)
    token_estimate: int | None = None
    excluded_by_budget: list[str] = Field(default_factory=list)
    content_hash: str | None = None

    def __init__(self, **data):
        data.setdefault("id", data.get("context_id") or new_id("context_package"))
        super().__init__(**data)


class PlanRevision(DomainModel):
    revision_id: str = Field(default_factory=lambda: new_id("plan_revision"))
    project_id: str
    title: str
    body: str
    status: str = "PROPOSED"  # PROPOSED | APPLIED | REJECTED | SUPERSEDED
    supersedes: str | None = None
    applied_at: str | None = None

    def __init__(self, **data):
        data.setdefault("id", data.get("revision_id") or new_id("plan_revision"))
        super().__init__(**data)


class TasteRuleProposal(DomainModel):
    """User edits first become TasteRuleProposal; not effective until approved (§21.4)."""

    proposal_id: str = Field(default_factory=lambda: new_id("taste_rule"))
    original_text: str
    edited_text: str
    inferred_rule: str
    scope: str = "reporting"  # reporting | manuscript | narrative
    confidence: float | None = None
    source: str = "human_edit"
    status: str = "PENDING"  # PENDING | APPROVED | REJECTED

    def __init__(self, **data):
        data.setdefault("id", data.get("proposal_id") or new_id("taste_rule"))
        super().__init__(**data)
