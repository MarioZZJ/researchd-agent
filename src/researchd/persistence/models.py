"""SQLAlchemy 2 ORM models for all researchd tables (IMPLEMENTATION.md §11).

Portability: only generic SQLAlchemy types (no SQLite-specific constructs),
so the schema can migrate to PostgreSQL later. SQLite runs with WAL + FK +
busy_timeout + NORMAL synchronous (set on engine connect).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..domain.base import utcnow


class Base(DeclarativeBase):
    pass


class TimestampedMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class CommonFieldsMixin(TimestampedMixin):
    """Generic fields per IMPLEMENTATION.md §11."""

    project_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), default="system", nullable=False)
    status: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ProjectRow(CommonFieldsMixin, Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    workspace_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    initial_brief_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ProjectBindingRow(CommonFieldsMixin, Base):
    __tablename__ = "project_bindings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    binding_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="project_group")
    cc_project: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ProjectMemberRow(CommonFieldsMixin, Base):
    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "platform_user_id", name="uq_project_member_user"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    member_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    platform_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default="member")
    can_approve_decisions: Mapped[bool] = mapped_column(Boolean, default=False)


class QuestionRow(CommonFieldsMixin, Base):
    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    question_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_core: Mapped[bool] = mapped_column(Boolean, default=False)


class TaskRow(CommonFieldsMixin, Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    contract_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    parent_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    blocked_by_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    depends_on_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    current_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class TaskDependencyRow(Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (UniqueConstraint("task_id", "depends_on", name="uq_task_dep"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("tasks.task_id"), index=True, nullable=False)
    depends_on: Mapped[str] = mapped_column(String(64), index=True, nullable=False)


class RunRow(CommonFieldsMixin, Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    executor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    executor_profile: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    configuration_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    process_instance_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_event_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    termination_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    repair_attempts: Mapped[int] = mapped_column(Integer, default=0)


class ExecutorSessionRow(CommonFieldsMixin, Base):
    __tablename__ = "executor_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    executor: Mapped[str] = mapped_column(String(64), nullable=False)
    executor_profile: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    remote_session_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_event_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")


class InvocationRow(CommonFieldsMixin, Base):
    """One model-call invocation (planner/worker/auditor), recorded for every
    executor turn so model usage is traceable even for project-level planner
    turns that have no task/run row (IMPLEMENTATION.md §13)."""

    __tablename__ = "invocations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    invocation_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # planner|worker|auditor
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    skills_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    budget_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # RUNNING|SUCCEEDED|FAILED|INTERRUPTED
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ArtifactRow(CommonFieldsMixin, Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default="file")
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    code_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data_version: Mapped[str | None] = mapped_column(String(64), nullable=True)


class EvidenceRow(CommonFieldsMixin, Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_refs_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    literature_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    computational_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    model_annotation_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    limitations_json: Mapped[list | None] = mapped_column(JSON, nullable=True)


class ClaimRow(CommonFieldsMixin, Base):
    __tablename__ = "claims"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    claim_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_core: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_state: Mapped[str] = mapped_column(String(32), default="UNTESTED")
    review_level: Mapped[str] = mapped_column(String(32), default="NONE")
    use_state: Mapped[str] = mapped_column(String(32), default="DRAFT")


class ClaimEvidenceRow(Base):
    __tablename__ = "claim_evidence"
    __table_args__ = (UniqueConstraint("claim_id", "evidence_id", name="uq_claim_evidence"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    claim_id: Mapped[str] = mapped_column(String(64), ForeignKey("claims.claim_id"), index=True, nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(64), ForeignKey("evidence.evidence_id"), index=True, nullable=False)
    relation: Mapped[str] = mapped_column(String(32), default="supports")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class IssueRow(CommonFieldsMixin, Base):
    __tablename__ = "issues"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    issue_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(16), default="info")
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    investigation_plan: Mapped[str | None] = mapped_column(Text, nullable=True)


class DecisionRow(CommonFieldsMixin, Base):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, default="")
    why_material: Mapped[str] = mapped_column(Text, default="")
    recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommendation_basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_refs_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    unresolved_uncertainty: Mapped[str | None] = mapped_column(Text, nullable=True)
    reversibility: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocking_scope_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    continue_scope_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    decision_version: Mapped[int] = mapped_column(Integer, default=1)
    category: Mapped[str] = mapped_column(String(32), default="other")
    affected_object: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    answer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    answered_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)


class DecisionOptionRow(Base):
    __tablename__ = "decision_options"
    __table_args__ = (UniqueConstraint("decision_id", "option_id", name="uq_decision_option"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(64), ForeignKey("decisions.decision_id"), index=True, nullable=False)
    option_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    scientific_consequence: Mapped[str] = mapped_column(Text, default="")


class ReportRow(CommonFieldsMixin, Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    spec_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    platform_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_idempotency_key: Mapped[str | None] = mapped_column(String(256), unique=True, nullable=True)
    body_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ContextPackageRow(CommonFieldsMixin, Base):
    __tablename__ = "context_packages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    context_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    objects_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    token_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    excluded_by_budget_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema: Mapped[str] = mapped_column(String(32), default="researchd.event.v1", nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True, nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    actor_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    causation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class InboundMessageRow(CommonFieldsMixin, Base):
    __tablename__ = "inbound_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), default="feishu")
    cc_project: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cc_session_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    actor_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    attachments_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    processed_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class OutboxRow(Base):
    __tablename__ = "outbox"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    destination: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutboxAttemptRow(Base):
    __tablename__ = "outbox_attempts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    outbox_id: Mapped[str] = mapped_column(String(64), ForeignKey("outbox.id"), index=True, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class LeaseRow(Base):
    __tablename__ = "leases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    owner: Mapped[str] = mapped_column(String(256), nullable=False)
    token: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspaceLockRow(Base):
    __tablename__ = "workspace_locks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    scope: Mapped[str] = mapped_column(String(512), nullable=False)  # path / section key
    owner: Mapped[str] = mapped_column(String(256), nullable=False)
    token: Mapped[str] = mapped_column(String(128), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    UniqueConstraint("project_id", "scope", name="uq_workspace_lock_scope")


class ProjectionStateRow(Base):
    __tablename__ = "projection_states"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    document_id: Mapped[str] = mapped_column(String(256), nullable=False)
    section_key: Mapped[str] = mapped_column(String(128), nullable=False)
    block_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    UniqueConstraint("project_id", "document_id", "section_key", name="uq_projection_section")


class PlanRevisionRow(CommonFieldsMixin, Base):
    __tablename__ = "plan_revisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes: Mapped[str | None] = mapped_column(String(64), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TasteRuleProposalRow(CommonFieldsMixin, Base):
    __tablename__ = "taste_rule_proposals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    edited_text: Mapped[str] = mapped_column(Text, nullable=False)
    inferred_rule: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(String(32), default="reporting")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="human_edit")


ALL_TABLES = [
    ProjectRow,
    ProjectBindingRow,
    ProjectMemberRow,
    QuestionRow,
    TaskRow,
    TaskDependencyRow,
    RunRow,
    ExecutorSessionRow,
    ArtifactRow,
    EvidenceRow,
    ClaimRow,
    ClaimEvidenceRow,
    IssueRow,
    DecisionRow,
    DecisionOptionRow,
    ReportRow,
    ContextPackageRow,
    EventRow,
    InboundMessageRow,
    OutboxRow,
    OutboxAttemptRow,
    LeaseRow,
    WorkspaceLockRow,
    ProjectionStateRow,
    PlanRevisionRow,
    TasteRuleProposalRow,
]
