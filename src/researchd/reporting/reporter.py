"""Reporter: eligibility -> spec -> lint -> compress -> outbox
(IMPLEMENTATION.md §21). Runs inside `researchd service` (async pipeline).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from ..domain.base import new_id, utcnow
from ..domain.enums import ReportType
from ..domain.report import ReportAction, ReportSpec, ReportUncertainty
from ..persistence.models import ReportRow
from ..persistence.outbox import OutboxRepo
from ..persistence.repositories import ClaimRepo, DecisionRepo, EvidenceRepo, TaskRepo
from .compressor import compress_report
from .eligibility import StateSnapshot, diff_snapshots
from .spec import compile_spec, lint_spec

logger = logging.getLogger("researchd.reporter")


@dataclass
class ReporterResult:
    report_id: str | None = None
    sent: bool = False
    reason: str = ""
    lint_errors: list[str] = field(default_factory=list)


def build_snapshot(session, project_id: str) -> StateSnapshot:  # noqa: ANN001
    """Deterministic snapshot of reportable state."""
    evidence = EvidenceRepo(session).list_verified(project_id)
    claims = ClaimRepo(session).list_by_project(project_id)
    decisions = DecisionRepo(session).list_open(project_id)
    from ..persistence.repositories import IssueRepo

    issues = IssueRepo(session).list_by_project(project_id)
    return StateSnapshot(
        project_id=project_id,
        verified_evidence_ids=[e.evidence_id for e in evidence],
        claim_state={c.claim_id: c.evidence_state.value for c in claims},
        open_decisions=[d.decision_id for d in decisions if d.status.value == "OPEN"],
        answered_decisions=[
            d.decision_id for d in decisions if d.status.value in ("ANSWERED", "APPLIED")
        ],
        unresolved_issues=[i.issue_id for i in issues if i.status.value in ("OPEN", "INVESTIGATING")],
        milestones=[],
        ts=utcnow(),
    )


def build_spec_from_snapshot(session, snapshot: StateSnapshot, reasons: list[str]) -> ReportSpec | None:  # noqa: ANN001
    """Compile a ReportSpec from the snapshot diff reasons."""
    project_id = snapshot.project_id
    decisions = DecisionRepo(session).list_open(project_id)
    open_dec = next((d for d in decisions if d.status.value == "OPEN"), None)
    if open_dec is not None:
        uncertainties = []
        if open_dec.unresolved_uncertainty:
            uncertainties.append(ReportUncertainty(text=open_dec.unresolved_uncertainty))
        return compile_spec(
            project_id=project_id,
            type=ReportType.DECISION,
            title=f"需要你的决定：{open_dec.question[:80]}",
            bottom_line=open_dec.recommendation or "研究出现需要科学判断的分叉",
            bottom_line_evidence_refs=open_dec.evidence_refs,
            uncertainties=uncertainties,
            active_actions=[
                ReportAction(task_id=t, text=f"等待决策 {open_dec.decision_id} 后恢复")
                for t in open_dec.blocking_scope
            ],
            decision_id=open_dec.decision_id,
        )
    if snapshot.verified_evidence_ids:
        return compile_spec(
            project_id=project_id,
            type=ReportType.EVIDENCE,
            title="研究进展",
            bottom_line=_evidence_bottom_line(session, snapshot.verified_evidence_ids),
            bottom_line_evidence_refs=snapshot.verified_evidence_ids[-5:],
            active_actions=_active_task_actions(session, project_id),
        )
    return None


def _evidence_bottom_line(session, evidence_ids: list[str]) -> str:  # noqa: ANN001
    """Bottom line that cites each new evidence by id + clipped statement
    (never a bare count — the report must reference what actually changed)."""
    parts = []
    for eid in evidence_ids[:5]:
        ev = EvidenceRepo(session).get_by_evidence_id(eid)
        if ev is None:
            continue
        statement = (ev.statement or "").strip().replace("\n", " ")
        parts.append(f"{eid}「{statement[:40]}」")
    if not parts:
        return f"新增 {len(evidence_ids)} 条已验证证据"
    return "新增已验证证据：" + "；".join(parts)


def _active_task_actions(session, project_id: str) -> list[ReportAction]:  # noqa: ANN001
    """Current actions must reference REAL tasks (READY/RUNNING), never
    generic "下一步继续深入" filler."""
    from ..domain.enums import TaskStatus

    actions = []
    for t in TaskRepo(session).list_by_status(project_id, []):
        if t.status.value not in (TaskStatus.READY.value, TaskStatus.RUNNING.value):
            continue
        actions.append(
            ReportAction(task_id=t.task_id, text=f"进行中：{t.contract.objective[:60]}")
        )
    return actions[:10]


async def schedule_report(session_factory, *, project_id: str) -> ReporterResult:
    """Async pipeline: snapshot -> diff (persisted signature) -> per-decision
    cards + digest -> lint -> compress -> outbox.

    The last emitted signature is persisted in projection_states as
    "<full-signature>|open:<sorted decision ids>"; a tick with an unchanged
    full signature emits nothing. NEW decisions (ids not in the previous
    open: list) each get their own card; other state changes produce one
    digest message. Outbox idempotency keys are report ids — a crash after
    commit-and-send replays the same key and never double-delivers.
    """
    with session_factory() as session:
        snapshot = build_snapshot(session, project_id)
        signature = snapshot.signature()

        from sqlalchemy import select

        from ..persistence.models import ProjectionStateRow

        row = session.execute(
            select(ProjectionStateRow).where(
                ProjectionStateRow.project_id == project_id,
                ProjectionStateRow.document_id == "report-state",
                ProjectionStateRow.section_key == "snapshot",
            )
        ).scalar_one_or_none()
        previous_open: set[str] = set()
        if row is not None:
            stored = row.content_hash or ""
            sig_part, _, open_part = stored.partition("|open:")
            if sig_part == signature:
                return ReporterResult(sent=False, reason="no state diff")
            if open_part:
                previous_open = set(open_part.split(","))

        current_open = set(snapshot.open_decisions)
        new_decisions = current_open - previous_open
        specs = _specs_for_snapshot(session, snapshot, new_decisions)
        if not specs:
            if row is None:
                session.add(
                    ProjectionStateRow(
                        id=new_id("other"),
                        project_id=project_id,
                        document_id="report-state",
                        section_key="snapshot",
                        content_hash=signature + "|open:" + ",".join(sorted(current_open)),
                    )
                )
            session.commit()
            return ReporterResult(sent=False, reason="nothing reportable")

        task_ids = {t.task_id for t in TaskRepo(session).list_by_status(project_id, [])}
        evidence_ids = set(snapshot.verified_evidence_ids)
        from ..application.decision_gate import HARD_GATE_CATEGORIES
        from ..domain.enums import DecisionCategory as DC

        last_report_id: str | None = None
        for spec in specs:
            procedural = False
            if spec.decision_id:
                from ..persistence.repositories import DecisionRepo

                dec = DecisionRepo(session).get_by_decision_id(spec.decision_id)
                if dec is not None:
                    try:
                        procedural = DC(dec.category) in HARD_GATE_CATEGORIES
                    except ValueError:
                        procedural = False
            if spec.decision_id:
                from ..persistence.repositories import DecisionRepo

                dec = DecisionRepo(session).get_by_decision_id(spec.decision_id)
                if dec is not None:
                    missing = [
                        o.option_id for o in dec.options if not o.scientific_consequence.strip()
                    ]
                    if missing:
                        return ReporterResult(
                            sent=False,
                            reason="linter failed",
                            lint_errors=[f"decision options lack scientific_consequence: {missing}"],
                        )
            lint = lint_spec(
                spec,
                known_task_ids=task_ids,
                known_evidence_ids=evidence_ids,
                allow_procedural_bottom_line=procedural,
            )
            if not lint.ok:
                return ReporterResult(sent=False, reason="linter failed", lint_errors=lint.errors)
            compressed = await compress_report(spec, allow_model=False)  # model gated (B-01/B-03)
            report_id = new_id("report")
            last_report_id = report_id
            _emit_report(session, project_id, report_id, spec, compressed.body, compressed.source)
        assert last_report_id is not None

        # persist the new snapshot signature atomically with the emission
        new_hash = signature + "|open:" + ",".join(sorted(current_open))
        if row is None:
            session.add(
                ProjectionStateRow(
                    id=new_id("other"),
                    project_id=project_id,
                    document_id="report-state",
                    section_key="snapshot",
                    content_hash=new_hash,
                )
            )
        else:
            row.content_hash = new_hash
        session.commit()
        return ReporterResult(report_id=last_report_id, sent=True, reason="queued")


def _specs_for_snapshot(session, snapshot: StateSnapshot, new_decisions: set[str]) -> list[ReportSpec]:  # noqa: ANN001
    """One spec per NEW decision (in stable order), plus a digest spec for
    other reportable changes when nothing new is a decision."""
    from ..persistence.repositories import DecisionRepo

    project_id = snapshot.project_id
    specs: list[ReportSpec] = []
    if new_decisions:
        decisions = DecisionRepo(session).list_open(project_id)
        for d in decisions:
            if d.decision_id in new_decisions and d.status.value == "OPEN":
                uncertainties = []
                if d.unresolved_uncertainty:
                    uncertainties.append(ReportUncertainty(text=d.unresolved_uncertainty))
                specs.append(
                    compile_spec(
                        project_id=project_id,
                        type=ReportType.DECISION,
                        title=f"需要你的决定：{d.question[:80]}",
                        bottom_line=d.recommendation or "研究出现需要科学判断的分叉",
                        bottom_line_evidence_refs=d.evidence_refs,
                        uncertainties=uncertainties,
                        active_actions=[
                            ReportAction(task_id=t, text=f"等待决策 {d.decision_id} 后恢复")
                            for t in d.blocking_scope
                        ],
                        decision_id=d.decision_id,
                    )
                )
        # decision cards plus (when other state changed) a digest for the rest
        if snapshot.verified_evidence_ids:
            specs.append(
                compile_spec(
                    project_id=project_id,
                    type=ReportType.EVIDENCE,
                    title="研究进展",
                    bottom_line=_evidence_bottom_line(session, snapshot.verified_evidence_ids),
                    bottom_line_evidence_refs=snapshot.verified_evidence_ids[-5:],
                    active_actions=_active_task_actions(session, project_id),
                )
            )
        return specs
    if snapshot.verified_evidence_ids:
        specs.append(
            compile_spec(
                project_id=project_id,
                type=ReportType.EVIDENCE,
                title="研究进展",
                bottom_line=_evidence_bottom_line(session, snapshot.verified_evidence_ids),
                bottom_line_evidence_refs=snapshot.verified_evidence_ids[-5:],
                active_actions=_active_task_actions(session, project_id),
            )
        )
    return specs


def _emit_report(session, project_id: str, report_id: str, spec: ReportSpec, body: str, source: str) -> None:  # noqa: ANN001
    """Persist the Report row + enqueue the outbox delivery (same transaction)."""
    session.add(
        ReportRow(
            id=report_id,
            report_id=report_id,
            project_id=project_id,
            spec_json=spec.model_dump(),
            status="COMPILED",
            body_hash=hashlib.sha256(body.encode()).hexdigest(),
        )
    )
    payload: dict = {
        "kind": "interactive_card" if spec.type == ReportType.DECISION else "message",
        "project_id": project_id,
        "report_id": report_id,
        "body": body,
        "source": source,
        "decision_id": spec.decision_id,
    }
    if spec.decision_id:
        # deterministic card buttons from the PERSISTED decision options
        from ..persistence.repositories import DecisionRepo

        decision = DecisionRepo(session).get_by_decision_id(spec.decision_id)
        if decision is not None:
            payload["buttons"] = [
                {
                    "text": o.label,
                    "value": f"/decision {decision.decision_id} {o.option_id} --version {decision.decision_version}",
                    "scientific_consequence": o.scientific_consequence,
                }
                for o in decision.options
            ]
    OutboxRepo(session).enqueue(
        destination="delivery",
        idempotency_key=f"report:{project_id}:{report_id}:v1",
        payload=payload,
        project_id=project_id,
    )


def diff_reason(session, project_id: str) -> list[str]:  # noqa: ANN001
    """Current diff reasons vs an empty baseline (used by researchctl/report)."""
    snapshot = build_snapshot(session, project_id)
    return diff_snapshots(None, snapshot).reasons
