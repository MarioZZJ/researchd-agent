"""State machine unit tests (IMPLEMENTATION.md §25.1)."""

import pytest

from researchd.domain.enums import RunStatus, TaskStatus
from researchd.domain.run import Run
from researchd.domain.state_machine import InvalidTransition
from researchd.domain.task import Budget, SuccessCriterion, Task, TaskContract


def make_task(**kw) -> Task:
    contract = TaskContract(
        task_id="T-001",
        role="analysis_worker",
        objective="test objective",
        why_now="test",
        success_criteria=[SuccessCriterion(id="SC-1", text="criterion one")],
        budget=Budget(max_wall_seconds=60),
    )
    return Task(task_id="T-001", project_id="P-TEST", contract=contract, **kw)


def test_illegal_jump_rejected():
    t = make_task()
    with pytest.raises(InvalidTransition):
        t.transition(TaskStatus.COMPLETED)  # PROPOSED -> COMPLETED
    assert t.status is TaskStatus.PROPOSED


def test_running_cannot_go_directly_to_completed():
    """RUNNING -> COMPLETED must be impossible (not in transition table)."""
    t = make_task()
    t.propose_ready()
    t.start(run_id="R-1", lease_token="L-1")
    assert t.status is TaskStatus.RUNNING
    with pytest.raises(InvalidTransition):
        t.transition(TaskStatus.COMPLETED)
    assert t.status is TaskStatus.RUNNING


def test_complete_requires_all_criteria_pass():
    t = make_task()
    t.propose_ready()
    t.start(run_id="R-1", lease_token="L-1")
    t.submit_review()
    with pytest.raises(InvalidTransition):
        t.complete(criteria_results=[{"criterion_id": "SC-1", "status": "FAIL"}])
    t.complete(criteria_results=[{"criterion_id": "SC-1", "status": "PASS"}])
    assert t.status is TaskStatus.COMPLETED


def test_completed_is_terminal():
    t = make_task()
    t.propose_ready()
    t.start(run_id="R-1", lease_token="L-1")
    t.submit_review()
    t.complete(criteria_results=[{"criterion_id": "SC-1", "status": "PASS"}])
    with pytest.raises(InvalidTransition):
        t.transition(TaskStatus.READY)


def test_start_requires_ready_and_lease():
    t = make_task()
    with pytest.raises(InvalidTransition):
        t.start(run_id="R-1", lease_token="L-1")  # still PROPOSED
    t.propose_ready()
    t.start(run_id="R-1", lease_token="L-1")
    assert t.current_run_id == "R-1"
    assert t.lease_token == "L-1"


def test_propose_ready_rejects_invalid_contract():
    bad = Task(
        task_id="T-002",
        project_id="P-TEST",
        contract=TaskContract(task_id="T-002", role="worker", objective="", success_criteria=[]),
    )
    with pytest.raises(InvalidTransition):
        bad.propose_ready()
    assert bad.status is TaskStatus.PROPOSED


def test_run_success_does_not_complete_task():
    """A SUCCEEDED Run is not scientific acceptance (IMPLEMENTATION.md §7.2)."""
    r = Run(run_id="R-1", task_id="T-001", project_id="P-TEST")
    r.transition(RunStatus.STARTING)
    r.transition(RunStatus.RUNNING)
    r.transition(RunStatus.SUCCEEDED)
    assert r.status is RunStatus.SUCCEEDED
    t = make_task()
    t.propose_ready()
    t.start(run_id="R-1", lease_token="L-1")
    # run succeeded but task must still go through REVIEW
    assert t.status is TaskStatus.RUNNING


def test_block_and_unblock():
    t = make_task()
    t.propose_ready()
    t.start(run_id="R-1", lease_token="L-1")
    t.block(decision_id="D-002")
    assert t.status is TaskStatus.BLOCKED
    assert t.blocked_by == ["D-002"]
    t.transition(TaskStatus.READY)
    assert t.status is TaskStatus.READY


def test_review_requeue_and_fail():
    t = make_task()
    t.propose_ready()
    t.start(run_id="R-1", lease_token="L-1")
    t.submit_review()
    t.requeue(reason="rerun")
    assert t.status is TaskStatus.READY
    t.start(run_id="R-2", lease_token="L-2")
    t.fail("boom")
    assert t.status is TaskStatus.FAILED


def test_run_machine_full_cycle():
    r = Run(run_id="R-1", task_id="T-001")
    r.transition(RunStatus.STARTING)
    r.transition(RunStatus.RUNNING)
    r.transition(RunStatus.SUCCEEDED)
    with pytest.raises(InvalidTransition):
        r.transition(RunStatus.FAILED)  # terminal


def test_run_orphan_transition():
    r = Run(run_id="R-1", task_id="T-001")
    r.transition(RunStatus.STARTING)
    r.transition(RunStatus.RUNNING)
    r.transition(RunStatus.ORPHANED)
    assert r.status is RunStatus.ORPHANED
