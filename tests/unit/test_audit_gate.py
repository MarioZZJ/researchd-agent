"""Auditor gate tests (IMPLEMENTATION.md §7.3, §26): the ONLY path to
COMPLETED tasks and VERIFIED evidence; verdict routing; idempotency."""

from __future__ import annotations

import pytest

from researchd.application.audit_gate import apply_audit_result
from researchd.domain.evidence import Evidence
from researchd.domain.run import Run
from researchd.domain.task import SuccessCriterion, Task, TaskContract
from researchd.executors.base import AuditResult, WorkResult
from researchd.persistence.repositories import EvidenceRepo, RunRepo, TaskRepo
from researchd.persistence.transaction import UnitOfWork

from researchd.domain.enums import EvidenceStatus, TaskStatus


def make_env(factory, *, criteria="PASS", evidence_candidates=None):
    from researchd.application.apply_result import apply_work_result

    with UnitOfWork(factory) as uow:
        task = Task(
            task_id="T-AUD-1",
            project_id="P-AUD",
            contract=TaskContract(
                task_id="T-AUD-1",
                role="analysis_worker",
                objective="分析",
                success_criteria=[SuccessCriterion(id="sc1", text="输出")],
            ),
        )
        task.propose_ready()
        task.start(run_id="R-WORK", lease_token="L")
        task.submit_review()
        TaskRepo(uow.session).save(task)
        work_result = WorkResult(
            schema="researchd.work_result.v1",
            task_id="T-AUD-1",
            outcome="SUBMIT_FOR_REVIEW",
            criteria_results=[{"criterion_id": "sc1", "status": criteria}],
            artifacts=[
                {"local_ref": "A-1", "kind": "document", "path": "out/r.json", "description": "d"}
            ],
            evidence_candidates=evidence_candidates or [],
            claim_changes=[],
            issues=[],
            decision_candidates=[],
            next_task_proposals=[],
        )
        work_run = Run(
            run_id="R-WORK",
            task_id="T-AUD-1",
            project_id="P-AUD",
            executor="fake",
            status="RUNNING",
            result=work_result.model_dump(),
        )
        RunRepo(uow.session).save(work_run)
        uow.session.flush()
        # candidates land as CANDIDATE rows through the real worker-apply path
        apply_work_result(uow.session, work_run, work_result)
        uow.commit()
        return task.task_id


def _audit_run(factory, *, verdict="ACCEPT", revision=None) -> Run:
    with UnitOfWork(factory) as uow:
        run = Run(
            run_id="R-AUD-1",
            task_id="T-AUD-1",
            project_id="P-AUD",
            executor="fake",
            status="RUNNING",
            resolved_model="fake/auditor",
            metadata={"role": "auditor", "worker_run_id": "R-WORK"},
        )
        RunRepo(uow.session).save(run)
        uow.commit()
    result = AuditResult(
        schema="researchd.audit_result.v1",
        task_id="T-AUD-1",
        verdict=verdict,
        checks=[{"check_id": "c1", "status": "PASS", "summary": "ok"}],
        revision_request=revision,
    )
    return run, result


def test_accept_completes_task_and_verifies_literature_evidence(factory):
    make_env(factory, evidence_candidates=[
        {"local_ref": "E-1", "type": "literature", "statement": "文献支持",
         "literature": {"source_id": "doi:10.1/x"}},
    ])
    run, result = _audit_run(factory, verdict="ACCEPT")
    with UnitOfWork(factory) as uow:
        counts = apply_audit_result(uow.session, run, TaskRepo(uow.session).get_by_task_id("T-AUD-1"), result)
        uow.commit()
        assert counts["completed"] == 1 and counts["verified_evidence"] == 1
        task = TaskRepo(uow.session).get_by_task_id("T-AUD-1")
        assert task.status is TaskStatus.COMPLETED
        ev = EvidenceRepo(uow.session).get_by_evidence_id("E-1")
        assert ev.status is EvidenceStatus.VERIFIED


def test_accept_without_provenance_keeps_candidate(factory):
    """Auditor opinion is necessary but NOT sufficient: real provenance is a
    hard gate, so a candidate whose artifact does not exist stays CANDIDATE."""
    make_env(factory, evidence_candidates=[
        {"local_ref": "E-1", "type": "human", "statement": "PI 陈述"},
    ])
    run, result = _audit_run(factory, verdict="ACCEPT")
    with UnitOfWork(factory) as uow:
        counts = apply_audit_result(uow.session, run, TaskRepo(uow.session).get_by_task_id("T-AUD-1"), result)
        uow.commit()
        assert counts["completed"] == 1
        assert counts["verified_evidence"] == 0
        ev = EvidenceRepo(uow.session).get_by_evidence_id("E-1")
        assert ev.status is EvidenceStatus.CANDIDATE


def test_revise_requeues_task(factory):
    make_env(factory)
    run, result = _audit_run(factory, verdict="REVISE", revision={"note": "需要补充方法"})
    with UnitOfWork(factory) as uow:
        counts = apply_audit_result(uow.session, run, TaskRepo(uow.session).get_by_task_id("T-AUD-1"), result)
        uow.commit()
        assert counts["revised"] == 1
        task = TaskRepo(uow.session).get_by_task_id("T-AUD-1")
        assert task.status is TaskStatus.READY
        assert "需要补充方法" in (task.error_message or "")


def test_reject_fails_task(factory):
    make_env(factory)
    run, result = _audit_run(factory, verdict="REJECT", revision={"note": "不可接受"})
    with UnitOfWork(factory) as uow:
        counts = apply_audit_result(uow.session, run, TaskRepo(uow.session).get_by_task_id("T-AUD-1"), result)
        uow.commit()
        assert counts["rejected"] == 1
        task = TaskRepo(uow.session).get_by_task_id("T-AUD-1")
        assert task.status is TaskStatus.FAILED


def test_apply_is_idempotent(factory):
    make_env(factory, evidence_candidates=[
        {"local_ref": "E-1", "type": "literature", "statement": "s",
         "literature": {"source_id": "doi:10.1/x"}},
    ])
    run, result = _audit_run(factory)
    with UnitOfWork(factory) as uow:
        first = apply_audit_result(uow.session, run, TaskRepo(uow.session).get_by_task_id("T-AUD-1"), result)
        uow.commit()
    with UnitOfWork(factory) as uow:
        task = TaskRepo(uow.session).get_by_task_id("T-AUD-1")
        replay = apply_audit_result(uow.session, run, task, result)
        uow.commit()
        assert replay == {"replayed": True}
        # task state untouched by replay
        assert TaskRepo(uow.session).get_by_task_id("T-AUD-1").status is TaskStatus.COMPLETED
        # exactly one evidence row, one audit event
        evs = [e for e in EvidenceRepo(uow.session).list_by_project("P-AUD") if e.evidence_id == "E-1"]
        assert len(evs) == 1


def test_accept_requires_all_criteria_pass(factory):
    make_env(factory, criteria="FAIL")
    run, result = _audit_run(factory)
    with UnitOfWork(factory) as uow:
        counts = apply_audit_result(uow.session, run, TaskRepo(uow.session).get_by_task_id("T-AUD-1"), result)
        uow.commit()
        assert counts["completed"] == 0
        assert TaskRepo(uow.session).get_by_task_id("T-AUD-1").status is TaskStatus.FAILED


def test_audit_crash_recovery_reviews_again_not_completes(factory):
    """A crashed audit run (RUNNING, no heartbeat) leaves the task REVIEW;
    reconciliation orphans the run and the next tick re-audits — never an
    implicit COMPLETE."""
    from researchd.scheduler.dispatch import reconcile_orphans

    make_env(factory)
    run, _result = _audit_run(factory)  # audit run created but never applied
    with UnitOfWork(factory) as uow:
        # simulate crash: stale heartbeat
        r = RunRepo(uow.session).get_by_run_id("R-AUD-1")
        r.heartbeat_at = None
        RunRepo(uow.session).save(r)
        uow.commit()
        orphaned = reconcile_orphans(uow.session, max_age_seconds=0)
        assert "R-AUD-1" in orphaned
        uow.commit()
        task = TaskRepo(uow.session).get_by_task_id("T-AUD-1")
        assert task.status is TaskStatus.REVIEW  # REVIEW preserved, no double-complete
