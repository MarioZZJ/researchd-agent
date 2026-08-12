"""Crash recovery via runtime receipts (IMPLEMENTATION.md §25.5): when the
model already produced the full structured result but the service died before
collect, reconciliation finishes the run from the receipt WITHOUT calling the
model again; without a receipt the run is orphaned and retried."""

from __future__ import annotations

import json

import pytest

from researchd.domain.project import Project
from researchd.domain.task import Budget, SuccessCriterion, Task, TaskContract
from researchd.executors.fake import FakeDeliveryPort, FakeExecutor
from researchd.persistence.repositories import ProjectRepo, RunRepo, TaskRepo
from researchd.persistence.transaction import init_db, make_engine, make_session_factory, UnitOfWork
from researchd.scheduler.dispatch import reconcile_orphans
from researchd.scheduler.loop import SchedulerLoop
from researchd.domain.enums import TaskStatus

pytestmark = pytest.mark.recovery


@pytest.fixture()
def env(tmp_path):
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    ws = tmp_path / "ws"
    ws.mkdir()
    with UnitOfWork(factory) as uow:
        ProjectRepo(uow.session).save(
            Project(project_id="P-RCP", name="r", description="d", workspace_root=str(ws))
        )
        uow.commit()
    yield {"factory": factory, "tmp": tmp_path}
    engine.dispose()


def _make_task_and_dispatch(env):
    ex = FakeExecutor()
    ex.script("worker", {"payload": {
        "schema": "researchd.work_result.v1", "task_id": "T-RCP", "outcome": "SUBMIT_FOR_REVIEW",
        "criteria_results": [{"criterion_id": "SC-1", "status": "PASS"}],
        "artifacts": [], "evidence_candidates": [], "claim_changes": [], "issues": [],
        "decision_candidates": [], "next_task_proposals": [],
    }})
    with UnitOfWork(env["factory"]) as uow:
        t = Task(task_id="T-RCP", project_id="P-RCP", status="READY",
                 contract=TaskContract(task_id="T-RCP", role="analysis_worker", objective="o",
                                       success_criteria=[SuccessCriterion(id="SC-1", text="c")],
                                       budget=Budget(max_wall_seconds=60)),
                 blocked_by=[])
        TaskRepo(uow.session).save(t)
        uow.commit()
    return ex


def test_receipt_recovery_completes_without_model_reinvocation(env):
    """Model produced the result (receipt written), service died before
    collect: reconciliation finishes the run from the receipt; the executor
    is NEVER called again for this run."""
    ex = _make_task_and_dispatch(env)
    port = FakeDeliveryPort()
    settings = type("S", (), {"scheduler": type("SC", (), {"max_parallel": 4})(),
                              "profiles": {}, "data_dir": str(env["tmp"])})()
    loop = SchedulerLoop(settings, env["factory"], ex, port, max_parallel=4)

    # simulate the executor writing its runtime receipt BEFORE collect
    receipt = {
        "role": "worker",
        "run_id": "RUN-RCP",
        "session_id": "SES-RCP",
        "transcript_path": "/nonexistent/transcript.jsonl",  # path only
        "extracted_at": "2026-08-11T00:00:00Z",
        "raw": {
            "schema": "researchd.work_result.v1",
            "task_id": "T-RCP",
            "outcome": "SUBMIT_FOR_REVIEW",
            "criteria_results": [{"criterion_id": "SC-1", "status": "PASS"}],
            "artifacts": [], "evidence_candidates": [], "claim_changes": [],
            "issues": [], "decision_candidates": [], "next_task_proposals": [],
        },
    }
    (env["tmp"] / "receipts").mkdir()
    (env["tmp"] / "receipts" / "RUN-RCP.json").write_text(json.dumps(receipt))

    with UnitOfWork(env["factory"]) as uow:
        # a RUNNING run with an expired heartbeat (as after a crash)
        from researchd.domain.run import Run
        from researchd.persistence.repositories import RunRepo as RR

        run = Run(run_id="RUN-RCP", task_id="T-RCP", project_id="P-RCP", executor="reasonix",
                  status="RUNNING", lease_token="lease-1")
        RR(uow.session).save(run)
        task = TaskRepo(uow.session).get_by_task_id("T-RCP")
        task.status = TaskStatus.RUNNING
        task.current_run_id = "RUN-RCP"
        TaskRepo(uow.session).save(task)
        uow.commit()

        handled = reconcile_orphans(uow.session, data_dir=str(env["tmp"]))
        uow.commit()

        # the task RUNNING->REVIEW path (auditor would complete it next)
        from researchd.domain.enums import TaskStatus as TS
        task = TaskRepo(uow.session).get_by_task_id("T-RCP")
        run = RR(uow.session).get_by_run_id("RUN-RCP")
        assert "RUN-RCP" in handled
        assert run.status.value == "SUCCEEDED"
        assert run.termination_reason == "recovered from runtime receipt (no re-invocation)"
        assert task.status in (TS.REVIEW, TS.RUNNING)  # never READY (no requeue)
        # the receipt is deliberately KEPT: a SUCCEEDED run never re-enters
        # recovery, so a leftover receipt is inert (deleting it pre-commit
        # would lose the only completed result on a crash)
        assert (env["tmp"] / "receipts" / "RUN-RCP.json").exists()
        # a SECOND reconciliation pass must be a no-op (run already SUCCEEDED)
        handled2 = reconcile_orphans(uow.session, data_dir=str(env["tmp"]))
        uow.commit()
        assert "RUN-RCP" not in handled2
    assert ex.call_count("worker") == 0  # model NEVER re-invoked


def test_no_receipt_orphans_and_requeues(env):
    ex = _make_task_and_dispatch(env)
    with UnitOfWork(env["factory"]) as uow:
        from researchd.domain.run import Run
        from researchd.persistence.repositories import RunRepo as RR

        run = Run(run_id="RUN-NO", task_id="T-RCP", project_id="P-RCP", executor="reasonix",
                  status="RUNNING", lease_token="lease-2")
        RR(uow.session).save(run)
        task = TaskRepo(uow.session).get_by_task_id("T-RCP")
        task.status = TaskStatus.RUNNING
        task.current_run_id = "RUN-NO"
        TaskRepo(uow.session).save(task)
        uow.commit()

        handled = reconcile_orphans(uow.session, data_dir=str(env["tmp"]))
        uow.commit()

        run = RR(uow.session).get_by_run_id("RUN-NO")
        task = TaskRepo(uow.session).get_by_task_id("T-RCP")
        assert run.status.value == "ORPHANED"
        assert task.status == TaskStatus.READY  # requeued for retry
        assert "no completion receipt" in run.termination_reason
