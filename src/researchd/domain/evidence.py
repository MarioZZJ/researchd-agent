"""Artifact, Evidence, Claim, Issue, Decision domain objects (IMPLEMENTATION.md §7.3-7.6)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .base import DomainModel, new_id, utcnow
from .enums import (
    ClaimEvidenceRelation,
    ClaimEvidenceState,
    ClaimReviewLevel,
    ClaimUseState,
    EvidenceStatus,
    EvidenceType,
    IssueStatus,
)
from .state_machine import (
    ClaimEvidenceStateMachine,
    ClaimReviewStateMachine,
    ClaimUseStateMachine,
    EvidenceStateMachine,
    IssueStateMachine,
)


# ---------------------------------------------------------------- Artifact
class Artifact(DomainModel):
    artifact_id: str = Field(default_factory=lambda: new_id("artifact"))
    task_id: str | None = None
    run_id: str | None = None
    kind: str = "file"  # table | figure | document | dataset | other
    path: str  # relative to project workspace root
    sha256: str | None = None
    size_bytes: int | None = None
    mime_type: str | None = None
    description: str = ""
    code_commit: str | None = None
    data_version: str | None = None
    status: str = "REGISTERED"

    def __init__(self, **data):
        data.setdefault("id", data.get("artifact_id") or new_id("artifact"))
        super().__init__(**data)


# ---------------------------------------------------------------- Evidence
class LiteratureProvenance(BaseModel):
    model_config = {"extra": "forbid"}

    source_id: str
    locator: str | None = None
    snapshot_artifact_id: str | None = None
    fetched_at: datetime | None = None
    limitations: list[str] = Field(default_factory=list)


class ComputationalProvenance(BaseModel):
    model_config = {"extra": "forbid"}

    run_id: str
    artifact_id: str
    code_commit: str | None = None
    data_snapshot: str | None = None
    statistics: dict[str, Any] = Field(default_factory=dict)
    uncertainty: dict[str, Any] = Field(default_factory=dict)
    interpretation_limits: list[str] = Field(default_factory=list)


class ModelAnnotationProvenance(BaseModel):
    model_config = {"extra": "forbid"}

    rubric_version: str
    model: str
    sampling: str
    validation_artifact_id: str | None = None
    limitations: list[str] = Field(default_factory=list)


class Evidence(DomainModel):
    """An Evidence must have real provenance; agent free-text judgment is NOT evidence."""

    evidence_id: str = Field(default_factory=lambda: new_id("evidence"))
    type: EvidenceType = EvidenceType.OTHER
    status: EvidenceStatus = EvidenceStatus.CANDIDATE
    statement: str
    task_id: str | None = None
    run_id: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    literature: LiteratureProvenance | None = None
    computational: ComputationalProvenance | None = None
    model_annotation: ModelAnnotationProvenance | None = None
    limitations: list[str] = Field(default_factory=list)

    def __init__(self, **data):
        data.setdefault("id", data.get("evidence_id") or new_id("evidence"))
        super().__init__(**data)

    @field_validator("status", mode="before")
    @classmethod
    def _coerce(cls, v):
        return EvidenceStatus(v) if v and not isinstance(v, EvidenceStatus) else v

    def provenance_ok(self) -> bool:
        """VERIFIED requires real provenance matching the type (IMPLEMENTATION.md §7.3, 25.4)."""
        if self.type == EvidenceType.LITERATURE:
            return bool(self.literature and self.literature.source_id and self.statement.strip())
        if self.type == EvidenceType.COMPUTATIONAL:
            return bool(
                self.computational
                and self.computational.run_id
                and self.computational.artifact_id
                and self.run_id
                and self.artifact_refs
                and self.computational.run_id == self.run_id  # references must be consistent
                and self.computational.artifact_id in self.artifact_refs
            )
        if self.type == EvidenceType.MODEL_ANNOTATION:
            return bool(self.model_annotation and self.model_annotation.rubric_version and self.model_annotation.model)
        if self.type == EvidenceType.HUMAN:
            return bool(self.created_by and self.created_by != "system")
        return bool(self.statement.strip() and (self.task_id or self.run_id))

    def verify(self) -> "Evidence":
        if not self.provenance_ok():
            raise ValueError(
                f"evidence {self.evidence_id} cannot be VERIFIED: missing provenance "
                "(real artifact/run/code/data required)"
            )
        if not EvidenceStateMachine("evidence", self.status).can(EvidenceStatus.VERIFIED):
            from .state_machine import InvalidTransition

            raise InvalidTransition("evidence", self.status, EvidenceStatus.VERIFIED)
        self.status = EvidenceStatus.VERIFIED
        self.updated_at = utcnow()
        return self

    def transition(self, target: EvidenceStatus | str) -> EvidenceStatus:
        target = EvidenceStatus(target) if isinstance(target, str) else target
        # VERIFIED is only reachable through verify() (provenance-gated)
        if target is EvidenceStatus.VERIFIED:
            raise ValueError(f"evidence {self.evidence_id}: VERIFIED only via verify() with real provenance")
        self.status = EvidenceStateMachine("evidence", self.status).transition(target)
        self.updated_at = utcnow()
        return self.status


# ---------------------------------------------------------------- Claim
class ClaimEvidenceLink(BaseModel):
    model_config = {"extra": "forbid"}

    evidence_id: str
    relation: ClaimEvidenceRelation = ClaimEvidenceRelation.SUPPORTS
    note: str | None = None


class Claim(DomainModel):
    claim_id: str = Field(default_factory=lambda: new_id("claim"))
    text: str
    is_core: bool = False
    evidence_state: ClaimEvidenceState = ClaimEvidenceState.UNTESTED
    review_level: ClaimReviewLevel = ClaimReviewLevel.NONE
    use_state: ClaimUseState = ClaimUseState.DRAFT
    evidence_links: list[ClaimEvidenceLink] = Field(default_factory=list)

    def __init__(self, **data):
        data.setdefault("id", data.get("claim_id") or new_id("claim"))
        super().__init__(**data)

    @field_validator("evidence_state", "review_level", "use_state", mode="before")
    @classmethod
    def _coerce(cls, v):
        if isinstance(v, str):
            for enum_cls in (ClaimEvidenceState, ClaimReviewLevel, ClaimUseState):
                try:
                    return enum_cls(v)
                except ValueError:
                    continue
        return v

    def set_evidence_state(self, target: ClaimEvidenceState | str) -> ClaimEvidenceState:
        target = ClaimEvidenceState(target) if isinstance(target, str) else target
        self.evidence_state = ClaimEvidenceStateMachine("claim.evidence", self.evidence_state).transition(target)
        self.updated_at = utcnow()
        return self.evidence_state

    def set_review_level(self, target: ClaimReviewLevel | str) -> ClaimReviewLevel:
        target = ClaimReviewLevel(target) if isinstance(target, str) else target
        self.review_level = ClaimReviewStateMachine("claim.review", self.review_level).transition(target)
        self.updated_at = utcnow()
        return self.review_level

    def set_use_state(self, target: ClaimUseState | str) -> ClaimUseState:
        target = ClaimUseState(target) if isinstance(target, str) else target
        self.use_state = ClaimUseStateMachine("claim.use", self.use_state).transition(target)
        self.updated_at = utcnow()
        return self.use_state

    def can_enter_manuscript(self) -> bool:
        """Core claims need cross-model review + PI approval (IMPLEMENTATION.md §7.4)."""
        if self.is_core:
            return self.review_level in (ClaimReviewLevel.CROSS_MODEL, ClaimReviewLevel.PI) and self.evidence_state in (
                ClaimEvidenceState.SUPPORTED,
                ClaimEvidenceState.MIXED,
            )
        return self.evidence_state not in (ClaimEvidenceState.CONTRADICTED, ClaimEvidenceState.UNTESTED)


# ---------------------------------------------------------------- Issue
class Issue(DomainModel):
    issue_id: str = Field(default_factory=lambda: new_id("issue"))
    status: IssueStatus = IssueStatus.OPEN
    title: str
    description: str = ""
    severity: str = "info"  # info | warning | critical
    task_id: str | None = None
    investigation_plan: str | None = None

    def __init__(self, **data):
        data.setdefault("id", data.get("issue_id") or new_id("issue"))
        super().__init__(**data)

    @field_validator("status", mode="before")
    @classmethod
    def _coerce(cls, v):
        return IssueStatus(v) if v and not isinstance(v, IssueStatus) else v

    def transition(self, target: IssueStatus | str) -> IssueStatus:
        target = IssueStatus(target) if isinstance(target, str) else target
        self.status = IssueStateMachine("issue", self.status).transition(target)
        self.updated_at = utcnow()
        return self.status
