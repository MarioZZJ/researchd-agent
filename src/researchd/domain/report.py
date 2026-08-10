"""Report domain objects: deterministic ReportSpec and compiled Report (IMPLEMENTATION.md §21)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .base import DomainModel, new_id
from .enums import ReportType


class ReportUncertainty(BaseModel):
    model_config = {"extra": "forbid"}

    text: str
    issue_ref: str | None = None


class ReportConflict(BaseModel):
    model_config = {"extra": "forbid"}

    text: str
    evidence_refs: list[str] = Field(default_factory=list)


class ReportAction(BaseModel):
    model_config = {"extra": "forbid"}

    task_id: str
    text: str


class ReportSpec(BaseModel):
    """Deterministic report content. A language model may only COMPRESS existing
    fields — never add conclusions, drop uncertainty, change evidence refs,
    decide buttons, or alter message type (IMPLEMENTATION.md §21.2)."""

    model_config = {"extra": "forbid"}

    schema: str = "researchd.report_spec.v1"
    type: ReportType
    title: str
    bottom_line: str | None = None
    bottom_line_evidence_refs: list[str] = Field(default_factory=list)
    conflicts: list[ReportConflict] = Field(default_factory=list)
    uncertainties: list[ReportUncertainty] = Field(default_factory=list)
    active_actions: list[ReportAction] = Field(default_factory=list)
    decision_id: str | None = None
    milestone_id: str | None = None
    digest_period: str | None = None


class Report(DomainModel):
    report_id: str = Field(default_factory=lambda: new_id("report"))
    spec: ReportSpec
    state: str = "COMPILED"  # COMPILED | SENT | FAILED
    platform_message_id: str | None = None
    sent_at: str | None = None
    delivery_idempotency_key: str | None = None
    body_hash: str | None = None

    def __init__(self, **data):
        data.setdefault("id", data.get("report_id") or new_id("report"))
        super().__init__(**data)
