"""Decision domain object (IMPLEMENTATION.md §7.6, §8).

Workers may only propose a DecisionCandidate. Only the Decision Gate creates
OPEN PI-facing decisions. Button clicks are idempotent; the fingerprint
deduplicates the same question regardless of wording.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from .base import DomainModel, new_id, utcnow
from .enums import DecisionCategory, DecisionStatus
from .state_machine import DecisionStateMachine


class DecisionOption(BaseModel):
    model_config = {"extra": "forbid"}

    option_id: str
    label: str
    description: str = ""
    scientific_consequence: str = ""


class Decision(DomainModel):
    decision_id: str = Field(default_factory=lambda: new_id("decision"))
    status: DecisionStatus = DecisionStatus.CANDIDATE
    question: str
    trigger: str = ""
    why_material: str = ""
    options: list[DecisionOption] = Field(default_factory=list)
    recommendation: str | None = None
    recommendation_basis: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    unresolved_uncertainty: str | None = None
    reversibility: str | None = None
    blocking_scope: list[str] = Field(default_factory=list)  # task ids
    continue_scope: list[str] = Field(default_factory=list)  # task ids
    decision_version: int = 1
    category: DecisionCategory = DecisionCategory.OTHER
    affected_object: str | None = None
    fingerprint: str | None = None
    answer: str | None = None
    answered_by: str | None = None
    answered_at: datetime | None = None
    applied_revision: int | None = None

    def __init__(self, **data):
        data.setdefault("id", data.get("decision_id") or new_id("decision"))
        super().__init__(**data)

    @field_validator("status", "category", mode="before")
    @classmethod
    def _coerce(cls, v):
        if isinstance(v, str):
            for enum_cls in (DecisionStatus, DecisionCategory):
                try:
                    return enum_cls(v)
                except ValueError:
                    continue
        return v

    def transition(self, target: DecisionStatus | str) -> DecisionStatus:
        target = DecisionStatus(target) if isinstance(target, str) else target
        self.status = DecisionStateMachine("decision", self.status).transition(target)
        self.updated_at = utcnow()
        return self.status

    def answer(self, option_id: str, actor: str, *, version: int | None = None) -> DecisionStatus:
        if version is not None and version != self.decision_version:
            raise ValueError(
                f"decision {self.decision_id}: version mismatch (expected {self.decision_version}, got {version})"
            )
        if not any(o.option_id == option_id for o in self.options):
            raise ValueError(f"decision {self.decision_id}: unknown option {option_id!r}")
        if self.status not in (DecisionStatus.OPEN,):
            raise ValueError(f"decision {self.decision_id}: not answerable in status {self.status}")
        self.answer = option_id
        self.answered_by = actor
        self.answered_at = utcnow()
        return self.transition(DecisionStatus.ANSWERED)

    def apply(self, revision: int) -> DecisionStatus:
        self.applied_revision = revision
        return self.transition(DecisionStatus.APPLIED)

    def withdraw(self, reason: str = "") -> DecisionStatus:
        return self.transition(DecisionStatus.WITHDRAWN)
