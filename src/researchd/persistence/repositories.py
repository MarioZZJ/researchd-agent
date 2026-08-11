"""Repositories: aggregate persistence with optimistic version control.

Each repository maps between domain objects (Pydantic) and ORM rows.
Updates use `WHERE version = :old` — zero rows affected raises
OptimisticConcurrencyError instead of silently overwriting (§25.1).
"""

from __future__ import annotations

import json
from typing import Any, Generic, TypeVar

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..domain.base import utcnow
from ..domain.decision import Decision, DecisionOption  # noqa: F401  (re-export)
from ..domain.events import EVENT_SCHEMA, Event, make_event  # noqa: F401
from ..domain.project import Project, Question  # noqa: F401
from ..domain.report import Report  # noqa: F401
from ..domain.run import Run
from ..domain.task import Task
from ..domain.evidence import Evidence, Claim, Issue  # noqa: F401
from .models import (  # noqa: F401
    ArtifactRow,
    ClaimRow,
    ContextPackageRow,
    DecisionOptionRow,
    DecisionRow,
    EventRow,
    EvidenceRow,
    IssueRow,
    ProjectBindingRow,
    ProjectRow,
    QuestionRow,
    ReportRow,
    RunRow,
    TaskRow,
)
from .transaction import OptimisticConcurrencyError

R = TypeVar("R")


class BaseRepo(Generic[R]):
    row_cls: type

    def __init__(self, session: Session):
        self.session = session

    def _to_row(self, obj: Any) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def _to_domain(self, row: Any) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def get(self, public_id: str) -> Any | None:
        row = self.session.execute(
            select(self.row_cls).where(self.row_cls.id == public_id)  # type: ignore[attr-defined]
        ).scalar_one_or_none()
        return self._to_domain(row) if row else None

    def save(self, obj: Any) -> Any:
        """Insert new aggregate (idempotent by public id) or update with version check."""
        existing = self.session.execute(
            select(self.row_cls).where(self.row_cls.id == obj.id)  # type: ignore[attr-defined]
        ).scalar_one_or_none()
        if existing is None:
            self.session.add(self._to_row(obj))
            return obj
        return self._update(obj, existing)

    def _update(self, obj: Any, existing: Any) -> Any:
        row = self._to_row(obj)
        values = {c.name: getattr(row, c.name) for c in row.__table__.columns}
        values.pop("created_at", None)
        values["version"] = obj.version + 1
        values["updated_at"] = utcnow()
        result = self.session.execute(
            update(self.row_cls)
            .where(self.row_cls.id == obj.id, self.row_cls.version == obj.version)  # type: ignore[attr-defined]
            .values(**values)
        )
        if result.rowcount == 0:
            raise OptimisticConcurrencyError(
                f"{self.row_cls.__tablename__} {obj.id}: version {obj.version} is stale"
            )
        obj.version += 1
        obj.updated_at = values["updated_at"]
        return obj

    def list_by_project(self, project_id: str) -> list[Any]:
        rows = self.session.execute(
            select(self.row_cls)
            .where(self.row_cls.project_id == project_id)  # type: ignore[attr-defined]
            .order_by(self.row_cls.created_at)  # type: ignore[attr-defined]
        ).scalars()
        return [self._to_domain(r) for r in rows]


# ---------------------------------------------------------------- Project
class ProjectRepo(BaseRepo[Project]):
    row_cls = ProjectRow

    def _to_row(self, p: Project) -> ProjectRow:
        return ProjectRow(
            id=p.id,
            project_id=p.project_id,
            name=p.name,
            description=p.description,
            status=p.status.value if hasattr(p.status, "value") else p.status,
            workspace_root=p.workspace_root,
            policy_json=p.policy.model_dump() if p.policy else None,
            paused_reason=p.paused_reason,
            initial_brief_hash=p.initial_brief_hash,
            version=p.version,
            created_by=p.created_by,
            created_at=p.created_at,
            updated_at=p.updated_at,
            metadata_json=p.metadata or None,
        )

    def _to_domain(self, row: ProjectRow) -> Project:
        from ..domain.project import ExecutorPolicy

        policy_data = dict(row.policy_json or {})
        # backward-compat: interaction fields moved out of the policy in Phase 2
        policy_data.pop("interaction_profile", None)
        policy_data.pop("interaction_reasoning", None)
        return Project(
            id=row.id,
            project_id=row.project_id,
            name=row.name,
            description=row.description,
            status=row.status,
            workspace_root=row.workspace_root,
            policy=ExecutorPolicy(**policy_data) if policy_data else ExecutorPolicy(),
            paused_reason=row.paused_reason,
            initial_brief_hash=row.initial_brief_hash,
            version=row.version,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
            metadata=row.metadata_json or {},
        )

    def get_by_project_id(self, project_id: str) -> Project | None:
        row = self.session.execute(
            select(ProjectRow).where(ProjectRow.project_id == project_id)
        ).scalar_one_or_none()
        return self._to_domain(row) if row else None

    def list_all(self) -> list[Project]:
        rows = self.session.execute(select(ProjectRow).order_by(ProjectRow.created_at)).scalars()
        return [self._to_domain(r) for r in rows]


# ---------------------------------------------------------------- Task
class TaskRepo(BaseRepo[Task]):
    row_cls = TaskRow

    def _to_row(self, t: Task) -> TaskRow:
        return TaskRow(
            id=t.id,
            task_id=t.task_id,
            project_id=t.project_id,
            status=t.status.value if hasattr(t.status, "value") else t.status,
            contract_json=t.contract.model_dump(),
            parent_task_id=t.parent_task_id,
            blocked_by_json=t.blocked_by or None,
            depends_on_json=t.depends_on or None,
            current_run_id=t.current_run_id,
            lease_token=t.lease_token,
            error_message=t.error_message,
            version=t.version,
            created_by=t.created_by,
            created_at=t.created_at,
            updated_at=t.updated_at,
            metadata_json=t.metadata or None,
        )

    def _to_domain(self, row: TaskRow) -> Task:
        from ..domain.task import TaskContract

        return Task(
            id=row.id,
            task_id=row.task_id,
            project_id=row.project_id,
            status=row.status,
            contract=TaskContract(**row.contract_json),
            parent_task_id=row.parent_task_id,
            blocked_by=row.blocked_by_json or [],
            depends_on=row.depends_on_json or [],
            current_run_id=row.current_run_id,
            lease_token=row.lease_token,
            error_message=row.error_message,
            version=row.version,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
            metadata=row.metadata_json or {},
        )

    def get_by_task_id(self, task_id: str) -> Task | None:
        row = self.session.execute(select(TaskRow).where(TaskRow.task_id == task_id)).scalar_one_or_none()
        return self._to_domain(row) if row else None

    def list_by_status(self, project_id: str | None, statuses: list[str]) -> list[Task]:
        stmt = select(TaskRow)
        if project_id:
            stmt = stmt.where(TaskRow.project_id == project_id)
        if statuses:
            stmt = stmt.where(TaskRow.status.in_(statuses))
        stmt = stmt.order_by(TaskRow.created_at)
        return [self._to_domain(r) for r in self.session.execute(stmt).scalars()]


# ---------------------------------------------------------------- Run
class RunRepo(BaseRepo[Run]):
    row_cls = RunRow

    def _to_row(self, r: Run) -> RunRow:
        return RunRow(
            id=r.id,
            run_id=r.run_id,
            project_id=r.project_id,
            task_id=r.task_id,
            status=r.status.value if hasattr(r.status, "value") else r.status,
            executor=r.executor,
            executor_profile=r.executor_profile,
            resolved_model=r.resolved_model,
            reasoning_effort=r.reasoning_effort,
            configuration_source=r.configuration_source,
            process_instance_id=r.process_instance_id,
            session_id=r.session_id,
            turn_id=r.turn_id,
            last_event_sequence=r.last_event_sequence,
            started_at=r.started_at,
            heartbeat_at=r.heartbeat_at,
            termination_reason=r.termination_reason,
            lease_token=r.lease_token,
            outcome=r.outcome,
            result_json=r.result,
            error_message=r.error_message,
            repair_attempts=r.repair_attempts,
            version=r.version,
            created_by=r.created_by,
            created_at=r.created_at,
            updated_at=r.updated_at,
            metadata_json=r.metadata or None,
        )

    def _to_domain(self, row: RunRow) -> Run:
        return Run(
            id=row.id,
            run_id=row.run_id,
            project_id=row.project_id,
            task_id=row.task_id,
            status=row.status,
            executor=row.executor,
            executor_profile=row.executor_profile,
            resolved_model=row.resolved_model,
            reasoning_effort=row.reasoning_effort,
            configuration_source=row.configuration_source,
            process_instance_id=row.process_instance_id,
            session_id=row.session_id,
            turn_id=row.turn_id,
            last_event_sequence=row.last_event_sequence,
            started_at=row.started_at,
            heartbeat_at=row.heartbeat_at,
            termination_reason=row.termination_reason,
            lease_token=row.lease_token,
            outcome=row.outcome,
            result=row.result_json,
            error_message=row.error_message,
            repair_attempts=row.repair_attempts,
            version=row.version,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
            metadata=row.metadata_json or {},
        )

    def get_by_run_id(self, run_id: str) -> Run | None:
        row = self.session.execute(select(RunRow).where(RunRow.run_id == run_id)).scalar_one_or_none()
        return self._to_domain(row) if row else None

    def list_active(self) -> list[Run]:
        rows = self.session.execute(
            select(RunRow).where(RunRow.status.in_(["QUEUED", "STARTING", "RUNNING"])).order_by(RunRow.created_at)
        ).scalars()
        return [self._to_domain(r) for r in rows]


# ---------------------------------------------------------------- Events
class EventRepo:
    def __init__(self, session: Session):
        self.session = session

    def append(self, event: Event) -> Event:
        """Append-only; idempotency_key unique. Duplicate -> IntegrityError (caller decides)."""
        row = EventRow(
            id=f"EVTROW-{event.event_id}",
            schema=EVENT_SCHEMA,
            event_id=event.event_id,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            project_id=event.project_id,
            aggregate_type=event.aggregate.type,
            aggregate_id=event.aggregate.id,
            aggregate_version=event.aggregate.version,
            actor_json=event.actor.model_dump() if event.actor else None,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            idempotency_key=event.idempotency_key,
            payload_json=event.payload,
        )
        self.session.add(row)
        return event

    def exists(self, idempotency_key: str) -> bool:
        row = self.session.execute(
            select(EventRow.id).where(EventRow.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        return row is not None

    def list_for_aggregate(self, aggregate_type: str, aggregate_id: str, limit: int = 500) -> list[Event]:
        rows = self.session.execute(
            select(EventRow)
            .where(EventRow.aggregate_type == aggregate_type, EventRow.aggregate_id == aggregate_id)
            .order_by(EventRow.occurred_at)
            .limit(limit)
        ).scalars()
        return [self._to_domain(r) for r in rows]

    def _to_domain(self, row: EventRow) -> Event:
        from ..domain.base import Actor, AggregateRef

        return Event(
            event_id=row.event_id,
            event_type=row.event_type,
            occurred_at=row.occurred_at,
            project_id=row.project_id,
            aggregate=AggregateRef(type=row.aggregate_type, id=row.aggregate_id, version=row.aggregate_version),
            actor=Actor(**row.actor_json) if row.actor_json else None,
            correlation_id=row.correlation_id,
            causation_id=row.causation_id,
            idempotency_key=row.idempotency_key,
            payload=row.payload_json or {},
        )

    def latest_version(self, aggregate_type: str, aggregate_id: str) -> int | None:
        row = self.session.execute(
            select(EventRow.aggregate_version)
            .where(EventRow.aggregate_type == aggregate_type, EventRow.aggregate_id == aggregate_id)
            .order_by(EventRow.occurred_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        return row


# ---------------------------------------------------------------- JSON helpers
def dumps_compact(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def delete_all(session: Session, tables: list[type]) -> None:
    """Test helper: truncate tables in FK-safe order."""
    for table in tables:
        session.execute(delete(table))


# ---------------------------------------------------------------- Artifact
class ArtifactRepo(BaseRepo):
    row_cls = ArtifactRow

    def _to_row(self, a) -> ArtifactRow:  # noqa: ANN001
        return ArtifactRow(
            id=a.id, artifact_id=a.artifact_id, project_id=a.project_id, task_id=a.task_id,
            run_id=a.run_id, kind=a.kind, path=a.path, sha256=a.sha256, size_bytes=a.size_bytes,
            mime_type=a.mime_type, description=a.description, code_commit=a.code_commit,
            data_version=a.data_version, status=a.status, version=a.version, created_by=a.created_by,
            created_at=a.created_at, updated_at=a.updated_at, metadata_json=a.metadata or None,
        )

    def _to_domain(self, row: ArtifactRow):
        from ..domain.evidence import Artifact

        return Artifact(
            id=row.id, artifact_id=row.artifact_id, project_id=row.project_id, task_id=row.task_id,
            run_id=row.run_id, kind=row.kind, path=row.path, sha256=row.sha256, size_bytes=row.size_bytes,
            mime_type=row.mime_type, description=row.description, code_commit=row.code_commit,
            data_version=row.data_version, status=row.status, version=row.version, created_by=row.created_by,
            created_at=row.created_at, updated_at=row.updated_at, metadata=row.metadata_json or {},
        )

    def get_by_artifact_id(self, artifact_id: str):
        row = self.session.execute(select(ArtifactRow).where(ArtifactRow.artifact_id == artifact_id)).scalar_one_or_none()
        return self._to_domain(row) if row else None


# ---------------------------------------------------------------- Evidence
class EvidenceRepo(BaseRepo):
    row_cls = EvidenceRow

    def _to_row(self, e) -> EvidenceRow:  # noqa: ANN001
        return EvidenceRow(
            id=e.id, evidence_id=e.evidence_id, project_id=e.project_id, type=e.type.value if hasattr(e.type, "value") else e.type,
            status=e.status.value if hasattr(e.status, "value") else e.status, statement=e.statement,
            task_id=e.task_id, run_id=e.run_id, artifact_refs_json=e.artifact_refs or None,
            literature_json=e.literature.model_dump() if e.literature else None,
            computational_json=e.computational.model_dump() if e.computational else None,
            model_annotation_json=e.model_annotation.model_dump() if e.model_annotation else None,
            limitations_json=e.limitations or None, version=e.version, created_by=e.created_by,
            created_at=e.created_at, updated_at=e.updated_at, metadata_json=e.metadata or None,
        )

    def _to_domain(self, row: EvidenceRow):
        from ..domain.evidence import (
            ComputationalProvenance,
            Evidence,
            LiteratureProvenance,
            ModelAnnotationProvenance,
        )

        return Evidence(
            id=row.id, evidence_id=row.evidence_id, project_id=row.project_id, type=row.type,
            status=row.status, statement=row.statement, task_id=row.task_id, run_id=row.run_id,
            artifact_refs=row.artifact_refs_json or [],
            literature=LiteratureProvenance(**row.literature_json) if row.literature_json else None,
            computational=ComputationalProvenance(**row.computational_json) if row.computational_json else None,
            model_annotation=ModelAnnotationProvenance(**row.model_annotation_json) if row.model_annotation_json else None,
            limitations=row.limitations_json or [], version=row.version, created_by=row.created_by,
            created_at=row.created_at, updated_at=row.updated_at, metadata=row.metadata_json or {},
        )

    def get_by_evidence_id(self, evidence_id: str):
        row = self.session.execute(select(EvidenceRow).where(EvidenceRow.evidence_id == evidence_id)).scalar_one_or_none()
        return self._to_domain(row) if row else None

    def list_verified(self, project_id: str) -> list:
        rows = self.session.execute(
            select(EvidenceRow).where(EvidenceRow.project_id == project_id, EvidenceRow.status == "VERIFIED")
        ).scalars()
        return [self._to_domain(r) for r in rows]


# ---------------------------------------------------------------- Claim
class ClaimRepo(BaseRepo):
    row_cls = ClaimRow

    def _to_row(self, c) -> ClaimRow:  # noqa: ANN001
        return ClaimRow(
            id=c.id, claim_id=c.claim_id, project_id=c.project_id, text=c.text, is_core=c.is_core,
            evidence_state=c.evidence_state.value if hasattr(c.evidence_state, "value") else c.evidence_state,
            review_level=c.review_level.value if hasattr(c.review_level, "value") else c.review_level,
            use_state=c.use_state.value if hasattr(c.use_state, "value") else c.use_state,
            version=c.version, created_by=c.created_by, created_at=c.created_at, updated_at=c.updated_at,
            metadata_json=c.metadata or None,
        )

    def _to_domain(self, row: ClaimRow):
        from ..domain.evidence import Claim

        return Claim(
            id=row.id, claim_id=row.claim_id, project_id=row.project_id, text=row.text, is_core=row.is_core,
            evidence_state=row.evidence_state, review_level=row.review_level, use_state=row.use_state,
            version=row.version, created_by=row.created_by, created_at=row.created_at, updated_at=row.updated_at,
            metadata=row.metadata_json or {},
        )

    def get_by_claim_id(self, claim_id: str):
        row = self.session.execute(select(ClaimRow).where(ClaimRow.claim_id == claim_id)).scalar_one_or_none()
        return self._to_domain(row) if row else None


# ---------------------------------------------------------------- Issue
class IssueRepo(BaseRepo):
    row_cls = IssueRow

    def _to_row(self, i) -> IssueRow:  # noqa: ANN001
        return IssueRow(
            id=i.id, issue_id=i.issue_id, project_id=i.project_id, status=i.status.value if hasattr(i.status, "value") else i.status,
            title=i.title, description=i.description, severity=i.severity, task_id=i.task_id,
            investigation_plan=i.investigation_plan, version=i.version, created_by=i.created_by,
            created_at=i.created_at, updated_at=i.updated_at, metadata_json=i.metadata or None,
        )

    def _to_domain(self, row: IssueRow):
        from ..domain.evidence import Issue

        return Issue(
            id=row.id, issue_id=row.issue_id, project_id=row.project_id, status=row.status,
            title=row.title, description=row.description, severity=row.severity, task_id=row.task_id,
            investigation_plan=row.investigation_plan, version=row.version, created_by=row.created_by,
            created_at=row.created_at, updated_at=row.updated_at, metadata=row.metadata_json or {},
        )

    def get_by_issue_id(self, issue_id: str):
        row = self.session.execute(select(IssueRow).where(IssueRow.issue_id == issue_id)).scalar_one_or_none()
        return self._to_domain(row) if row else None


# ---------------------------------------------------------------- Decision
class DecisionRepo(BaseRepo):
    row_cls = DecisionRow

    def _to_row(self, d) -> DecisionRow:  # noqa: ANN001
        return DecisionRow(
            id=d.id, decision_id=d.decision_id, project_id=d.project_id, status=d.status.value if hasattr(d.status, "value") else d.status,
            question=d.question, trigger=d.trigger, why_material=d.why_material,
            recommendation=d.recommendation, recommendation_basis=d.recommendation_basis,
            evidence_refs_json=d.evidence_refs or None, unresolved_uncertainty=d.unresolved_uncertainty,
            reversibility=d.reversibility, blocking_scope_json=d.blocking_scope or None,
            continue_scope_json=d.continue_scope or None, decision_version=d.decision_version,
            category=d.category.value if hasattr(d.category, "value") else d.category,
            affected_object=d.affected_object, fingerprint=d.fingerprint, answer=d.answer,
            answered_by=d.answered_by, answered_at=d.answered_at, applied_revision=d.applied_revision,
            version=d.version, created_by=d.created_by, created_at=d.created_at, updated_at=d.updated_at,
            metadata_json=d.metadata or None,
        )

    def _to_domain(self, row: DecisionRow):
        from ..domain.decision import Decision, DecisionOption

        options: list[DecisionOption] = []
        for opt_row in self.session.execute(
            select(DecisionOptionRow).where(DecisionOptionRow.decision_id == row.decision_id).order_by(DecisionOptionRow.id)
        ).scalars():
            options.append(
                DecisionOption(
                    option_id=opt_row.option_id, label=opt_row.label, description=opt_row.description,
                    scientific_consequence=opt_row.scientific_consequence,
                )
            )
        return Decision(
            id=row.id, decision_id=row.decision_id, project_id=row.project_id, status=row.status,
            question=row.question, trigger=row.trigger, why_material=row.why_material,
            recommendation=row.recommendation, recommendation_basis=row.recommendation_basis,
            evidence_refs=row.evidence_refs_json or [], unresolved_uncertainty=row.unresolved_uncertainty,
            reversibility=row.reversibility, blocking_scope=row.blocking_scope_json or [],
            continue_scope=row.continue_scope_json or [], decision_version=row.decision_version,
            category=row.category, affected_object=row.affected_object, fingerprint=row.fingerprint,
            answer=row.answer, answered_by=row.answered_by, answered_at=row.answered_at,
            applied_revision=row.applied_revision, options=options, version=row.version,
            created_by=row.created_by, created_at=row.created_at, updated_at=row.updated_at,
            metadata=row.metadata_json or {},
        )

    def get_by_decision_id(self, decision_id: str):
        row = self.session.execute(select(DecisionRow).where(DecisionRow.decision_id == decision_id)).scalar_one_or_none()
        return self._to_domain(row) if row else None

    def list_open(self, project_id: str | None = None) -> list:
        stmt = select(DecisionRow).where(DecisionRow.status.in_(["OPEN", "ANSWERED"]))
        if project_id:
            stmt = stmt.where(DecisionRow.project_id == project_id)
        return [self._to_domain(r) for r in self.session.execute(stmt.order_by(DecisionRow.created_at)).scalars()]

    def list_all_statuses(self, project_id: str | None = None) -> list:
        """All decisions regardless of status (fingerprint dedup + unblocking)."""
        stmt = select(DecisionRow)
        if project_id:
            stmt = stmt.where(DecisionRow.project_id == project_id)
        return [self._to_domain(r) for r in self.session.execute(stmt.order_by(DecisionRow.created_at)).scalars()]

    def save(self, d) -> Decision:  # noqa: ANN001
        """Save decision + replace its options in the same transaction."""
        result = super().save(d)
        # options: delete + reinsert (small fixed set); flush the decision row
        # first so the FK target exists (autoflush is off in UoW sessions)
        self.session.flush()
        self.session.execute(delete(DecisionOptionRow).where(DecisionOptionRow.decision_id == d.decision_id))
        for idx, opt in enumerate(d.options):
            self.session.add(
                DecisionOptionRow(
                    id=f"{d.id}-O{idx}", decision_id=d.decision_id, option_id=opt.option_id,
                    label=opt.label, description=opt.description, scientific_consequence=opt.scientific_consequence,
                )
            )
        return result


# ---------------------------------------------------------------- Report
class ReportRepo(BaseRepo):
    row_cls = ReportRow

    def _to_row(self, r) -> ReportRow:  # noqa: ANN001
        return ReportRow(
            id=r.id, report_id=r.report_id, project_id=r.project_id, spec_json=r.spec.model_dump(),
            platform_message_id=r.platform_message_id, sent_at=r.sent_at,
            delivery_idempotency_key=r.delivery_idempotency_key, body_hash=r.body_hash,
            version=r.version, created_by=r.created_by, created_at=r.created_at, updated_at=r.updated_at,
            metadata_json=r.metadata or None,
        )

    def _to_domain(self, row: ReportRow):
        from ..domain.report import Report, ReportSpec

        return Report(
            id=row.id, report_id=row.report_id, project_id=row.project_id, spec=ReportSpec(**row.spec_json),
            platform_message_id=row.platform_message_id, sent_at=row.sent_at,
            delivery_idempotency_key=row.delivery_idempotency_key, body_hash=row.body_hash,
            version=row.version, created_by=row.created_by, created_at=row.created_at, updated_at=row.updated_at,
            metadata=row.metadata_json or {},
        )

    def get_by_report_id(self, report_id: str):
        row = self.session.execute(select(ReportRow).where(ReportRow.report_id == report_id)).scalar_one_or_none()
        return self._to_domain(row) if row else None


# ---------------------------------------------------------------- Question
class QuestionRepo(BaseRepo):
    row_cls = QuestionRow

    def _to_row(self, q) -> QuestionRow:  # noqa: ANN001
        return QuestionRow(
            id=q.id, question_id=q.question_id, project_id=q.project_id, text=q.text, is_core=q.is_core,
            status=q.status, version=q.version, created_by=q.created_by, created_at=q.created_at,
            updated_at=q.updated_at, metadata_json=q.metadata or None,
        )

    def _to_domain(self, row: QuestionRow):
        from ..domain.project import Question

        return Question(
            id=row.id, question_id=row.question_id, project_id=row.project_id, text=row.text,
            is_core=row.is_core, status=row.status, version=row.version, created_by=row.created_by,
            created_at=row.created_at, updated_at=row.updated_at, metadata=row.metadata_json or {},
        )


# ---------------------------------------------------------------- Binding / Member
class ProjectBindingRepo(BaseRepo):
    row_cls = ProjectBindingRow

    def _to_row(self, b) -> ProjectBindingRow:  # noqa: ANN001
        return ProjectBindingRow(
            id=b.id, binding_id=b.binding_id, project_id=b.project_id, kind=b.kind,
            cc_project=b.cc_project, session_key=b.session_key, chat_id=b.chat_id, enabled=b.enabled,
            version=b.version, created_by=b.created_by, created_at=b.created_at, updated_at=b.updated_at,
            metadata_json=b.metadata or None,
        )

    def _to_domain(self, row: ProjectBindingRow):
        from ..domain.project import ProjectBinding

        return ProjectBinding(
            id=row.id, binding_id=row.binding_id, project_id=row.project_id, kind=row.kind,
            cc_project=row.cc_project, session_key=row.session_key, chat_id=row.chat_id, enabled=row.enabled,
            version=row.version, created_by=row.created_by, created_at=row.created_at, updated_at=row.updated_at,
            metadata=row.metadata_json or {},
        )

    def list_for_project(self, project_id: str) -> list:
        rows = self.session.execute(
            select(ProjectBindingRow).where(ProjectBindingRow.project_id == project_id).order_by(ProjectBindingRow.created_at)
        ).scalars()
        return [self._to_domain(r) for r in rows]


# ---------------------------------------------------------------- Context Package
class ContextPackageRepo(BaseRepo):
    """Persist context packages so every model judgment is traceable to the
    exact objects + rendered text it received (IMPLEMENTATION.md §13)."""

    row_cls = ContextPackageRow

    def _to_row(self, p) -> ContextPackageRow:  # noqa: ANN001
        meta = dict(p.metadata or {})
        meta.update({"role": p.role, "run_id": p.run_id, "content": p.content})
        return ContextPackageRow(
            id=p.id,
            context_id=p.context_id,
            project_id=p.project_id,
            task_id=p.task_id,
            objects_json=[o.model_dump() for o in p.objects],
            token_estimate=p.token_estimate,
            excluded_by_budget_json=p.excluded_by_budget or None,
            content_hash=p.content_hash,
            version=p.version,
            created_by=p.created_by,
            created_at=p.created_at,
            updated_at=p.updated_at,
            status=p.role,
            metadata_json=meta,
        )

    def _to_domain(self, row: ContextPackageRow):
        from ..domain.context import ContextObjectRef, ContextPackage

        meta = dict(row.metadata_json or {})
        pkg_meta = {k: v for k, v in meta.items() if k not in ("role", "run_id", "content")}
        return ContextPackage(
            id=row.id,
            context_id=row.context_id,
            role=meta.get("role") or row.status or "worker",
            task_id=row.task_id,
            run_id=meta.get("run_id"),
            project_id=row.project_id,
            objects=[ContextObjectRef(**o) for o in (row.objects_json or [])],
            content=meta.get("content") or "",
            token_estimate=row.token_estimate,
            excluded_by_budget=row.excluded_by_budget_json or [],
            content_hash=row.content_hash,
            version=row.version,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
            metadata=pkg_meta,
        )

    def get_by_context_id(self, context_id: str):
        row = self.session.execute(
            select(ContextPackageRow).where(ContextPackageRow.context_id == context_id)
        ).scalar_one_or_none()
        return self._to_domain(row) if row else None

    def list_for_run(self, run_id: str) -> list:
        rows = self.session.execute(
            select(ContextPackageRow).where(ContextPackageRow.metadata_json["run_id"].as_string() == run_id)
            .order_by(ContextPackageRow.created_at)
        ).scalars()
        return [self._to_domain(r) for r in rows]
