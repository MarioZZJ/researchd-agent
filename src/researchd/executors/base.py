"""Executor abstraction and validated result models (IMPLEMENTATION.md §12).

Executors never return free-form "work reports". Everything is validated
against the JSON contracts in executors/schemas/. Raw executor output is only
written to the restricted run directory, never to Feishu.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..domain.enums import AuditVerdict, ClaimEvidenceRelation, EvidenceType, WorkOutcome

SCHEMA_DIR = Path(__file__).parent / "schemas"

_SCHEMA_CACHE: dict[str, dict] = {}


def load_schema(name: str) -> dict:
    if name not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[name] = json.loads((SCHEMA_DIR / name).read_text())
    return _SCHEMA_CACHE[name]


class ValidationFailure(Exception):
    """Raised when an executor result fails schema/contract validation."""


# ---------------------------------------------------------------- WorkResult
class CriterionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    status: Literal["PASS", "FAIL", "NOT_APPLICABLE"]
    refs: list[str] = Field(default_factory=list)


class WorkArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_ref: str
    kind: str
    path: str
    description: str = ""


class EvidenceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_ref: str
    type: EvidenceType
    statement: str
    artifact_refs: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    literature: dict | None = None


class ClaimChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str | None = None
    operation: Literal["create", "update", "retire"]
    text: str | None = None
    is_core: bool | None = None
    evidence_relations: list[dict] = Field(default_factory=list)


class WorkIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    description: str = ""
    severity: Literal["info", "warning", "critical"] = "info"


class DecisionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    trigger: str = ""
    why_material: str = ""
    category: str = "other"
    affected_object: str | None = None
    options: list[dict] = Field(default_factory=list)
    recommendation: str | None = None
    recommendation_basis: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    unresolved_uncertainty: str | None = None
    reversibility: str | None = None
    blocking_scope: list[str] = Field(default_factory=list)
    continue_scope: list[str] = Field(default_factory=list)
    has_option_conflict: bool = True
    cheap_parallel: bool = False
    numerical_only: bool = False
    hard_gate_override: bool = False  # external release / forced PI gate


class NextTaskProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    role: str
    objective: str
    why_now: str = ""
    depends_on: list[str] = Field(default_factory=list)


class WorkResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema: Literal["researchd.work_result.v1"] = "researchd.work_result.v1"
    task_id: str
    outcome: WorkOutcome
    criteria_results: list[CriterionResult] = Field(default_factory=list)
    artifacts: list[WorkArtifact] = Field(default_factory=list)
    evidence_candidates: list[EvidenceCandidate] = Field(default_factory=list)
    claim_changes: list[ClaimChange] = Field(default_factory=list)
    issues: list[WorkIssue] = Field(default_factory=list)
    decision_candidates: list[DecisionCandidate] = Field(default_factory=list)
    next_task_proposals: list[NextTaskProposal] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_contract(self) -> "WorkResult":
        # Every evidence candidate must reference a declared artifact by local_ref.
        declared = {a.local_ref for a in self.artifacts}
        for ev in self.evidence_candidates:
            for ref in ev.artifact_refs:
                if ref not in declared:
                    raise ValueError(
                        f"evidence candidate {ev.local_ref!r} references undeclared artifact {ref!r}"
                    )
        return self


# ---------------------------------------------------------------- PlannerResult
class ProposedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    role: str
    objective: str
    why_now: str = ""
    inputs: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    success_criteria: list[dict] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    escalation_conditions: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    budget: dict = Field(default_factory=dict)
    executor_profile: str | None = None


class PlannerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema: Literal["researchd.planner_result.v1"] = "researchd.planner_result.v1"
    proposed_tasks: list[ProposedTask] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    plan_revisions: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------- AuditResult
class AuditCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    status: Literal["PASS", "FAIL", "WARN"]
    summary: str
    refs: list[str] = Field(default_factory=list)


class EvidenceStatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    to_status: Literal["VERIFIED", "CONTESTED", "INVALID", "SUPERSEDED"]
    reason: str = ""


class ClaimStatusSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    evidence_state: str
    reason: str = ""


class AuditResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema: Literal["researchd.audit_result.v1"] = "researchd.audit_result.v1"
    task_id: str
    verdict: AuditVerdict
    checks: list[AuditCheck] = Field(default_factory=list)
    evidence_status_changes: list[EvidenceStatusChange] = Field(default_factory=list)
    claim_status_suggestions: list[ClaimStatusSuggestion] = Field(default_factory=list)
    issues: list[WorkIssue] = Field(default_factory=list)
    decision_candidates: list[DecisionCandidate] = Field(default_factory=list)
    revision_request: dict | None = None


def validate_work_result(raw: dict) -> WorkResult:
    try:
        return WorkResult.model_validate(raw)
    except (ValidationError, ValueError) as exc:
        raise ValidationFailure(f"WorkResult invalid: {exc}") from exc


def validate_planner_result(raw: dict) -> PlannerResult:
    try:
        return PlannerResult.model_validate(raw)
    except (ValidationError, ValueError) as exc:
        raise ValidationFailure(f"PlannerResult invalid: {exc}") from exc


def validate_audit_result(raw: dict) -> AuditResult:
    try:
        return AuditResult.model_validate(raw)
    except (ValidationError, ValueError) as exc:
        raise ValidationFailure(f"AuditResult invalid: {exc}") from exc


# ---------------------------------------------------------------- Adapter base
class ExecutorCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supports_session_new: bool = False
    supports_session_load: bool = False
    supports_session_resume: bool = False
    supports_steering: bool = False
    supports_cancel: bool = False
    supports_structured_output: bool = False
    supports_model_override: bool = False
    supports_reasoning_override: bool = False
    supports_tool_approval: bool = False


class ExecutorSessionInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executor: str
    process_instance_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    last_event_sequence: int | None = None
    termination_reason: str | None = None
    raw_events: list[dict] = Field(default_factory=list)


class ExecutorAdapter(ABC):
    """An executor runs ONE task turn. Raw output goes to the run directory only."""

    name: str = "base"
    capabilities: ExecutorCapabilities = ExecutorCapabilities()

    @abstractmethod
    async def run_planner(self, context: dict, *, profile: dict) -> tuple[PlannerResult, ExecutorSessionInfo]:
        """Planner turn: propose tasks. Never contacts PI directly."""

    @abstractmethod
    async def run_worker(self, context: dict, *, profile: dict) -> tuple[WorkResult, ExecutorSessionInfo]:
        """Worker turn: produce a structured WorkResult."""

    @abstractmethod
    async def run_auditor(self, context: dict, *, profile: dict) -> tuple[AuditResult, ExecutorSessionInfo]:
        """Auditor turn: ACCEPT/REVISE/BLOCK/REJECT a WorkResult."""

    async def close(self) -> None:
        """Release resources (sessions, processes)."""
