"""Reporter regression tests: evidence-citing bottom lines, real task
actions, real milestone events (no evidence-count thresholds)."""

from __future__ import annotations

import pytest

from researchd.domain.base import new_id
from researchd.domain.evidence import Evidence, Issue
from researchd.domain.task import SuccessCriterion, Task, TaskContract
from researchd.persistence.repositories import (
    DecisionRepo,
    EvidenceRepo,
    IssueRepo,
    ProjectRepo,
    TaskRepo,
)
from researchd.persistence.transaction import UnitOfWork
from researchd.reporting.reporter import _active_task_actions, _evidence_bottom_line, build_snapshot
from researchd.scheduler.extensions import check_milestones


@pytest.fixture()
def project(factory):
    p = type("P", (), {"project_id": "P-REP", "status": type("S", (), {"value": "ACTIVE"})(), "name": "rep"})()
    with UnitOfWork(factory) as uow:
        from researchd.domain.project import Project

        ProjectRepo(uow.session).save(Project(project_id="P-REP", name="rep", description="d"))
        uow.commit()
    return p


def test_evidence_bottom_line_cites_ids_and_statements(factory):
    with UnitOfWork(factory) as uow:
        EvidenceRepo(uow.session).save(
            Evidence(evidence_id="E-1", project_id="P-REP", status="VERIFIED", statement="疫情后跨学科引用上升")
        )
        EvidenceRepo(uow.session).save(
            Evidence(evidence_id="E-2", project_id="P-REP", status="VERIFIED", statement="方法学可复现")
        )
        uow.commit()
        line = _evidence_bottom_line(uow.session, ["E-1", "E-2"])
        assert "E-1" in line and "E-2" in line
        assert "疫情后跨学科引用上升" in line
        assert "2 条" not in line  # never a bare count


def test_active_actions_reference_real_tasks(factory):
    with UnitOfWork(factory) as uow:
        for tid, status in (("T-1", "READY"), ("T-2", "RUNNING"), ("T-3", "COMPLETED")):
            t = Task(
                task_id=tid,
                project_id="P-REP",
                contract=TaskContract(task_id=tid, role="worker", objective=f"任务 {tid}",
                success_criteria=[SuccessCriterion(id="sc", text="c")]),
            )
            if status == "READY":
                t.propose_ready()
            elif status == "RUNNING":
                t.propose_ready()
                t.start(run_id=new_id("run"), lease_token="L")
            else:
                t.propose_ready()
                t.start(run_id=new_id("run"), lease_token="L")
                t.submit_review()
                t.complete([{"criterion_id": "sc", "status": "PASS"}])
            TaskRepo(uow.session).save(t)
        uow.commit()
        actions = _active_task_actions(uow.session, "P-REP")
        ids = {a.task_id for a in actions}
        assert ids == {"T-1", "T-2"}
        assert "T-3" not in ids
        assert all(a.text for a in actions)


def test_milestones_are_real_events_idempotent(factory):
    from researchd.domain.project import Project

    with UnitOfWork(factory) as uow:
        ProjectRepo(uow.session).save(Project(project_id="P-REP", name="rep", description="d"))
        uow.commit()
    with UnitOfWork(factory) as uow:
        t = Task(
            task_id="T-M1",
            project_id="P-REP",
            contract=TaskContract(task_id="T-M1", role="worker", objective="完成分析",
            success_criteria=[SuccessCriterion(id="sc", text="c")]),
        )
        t.propose_ready()
        t.start(run_id="R-1", lease_token="L")
        t.submit_review()
        t.complete([{"criterion_id": "sc", "status": "PASS"}])
        TaskRepo(uow.session).save(t)
        from researchd.domain.decision import Decision, DecisionOption

        DecisionRepo(uow.session).save(
            Decision(
                decision_id="D-1", project_id="P-REP", question="定位", answer="A",
                status="APPLIED", decision_version=1,
                options=[DecisionOption(option_id="A", label="A")],
            )
        )
        uow.commit()

    # first call emits both milestones
    n = check_milestones(factory)
    assert n == 2
    # idempotent: replay emits nothing
    assert check_milestones(factory) == 0
    assert check_milestones(factory) == 0
    from sqlalchemy import select

    from researchd.persistence.models import EventRow

    with UnitOfWork(factory) as uow:
        events = uow.session.execute(
            select(EventRow).where(EventRow.event_type == "milestone.reached")
        ).scalars().all()
        assert len(events) == 2
        keys = {e.idempotency_key for e in events}
        assert "milestone:P-REP:first-completed" in keys
        assert "milestone:P-REP:first-decision-applied" in keys


def test_no_threshold_milestone_from_bare_evidence(factory):
    """An evidence-count threshold must NEVER trigger a milestone: verified
    evidence alone (no completed task, no applied decision) emits nothing."""
    with UnitOfWork(factory) as uow:
        EvidenceRepo(uow.session).save(
            Evidence(evidence_id="E-9", project_id="P-REP", status="VERIFIED", statement="x")
        )
        EvidenceRepo(uow.session).save(
            Evidence(evidence_id="E-8", project_id="P-REP", status="VERIFIED", statement="y")
        )
        uow.commit()
    assert check_milestones(factory) == 0
