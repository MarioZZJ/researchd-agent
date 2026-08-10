"""Executor schema validation tests (IMPLEMENTATION.md §12, §25.4)."""

import pytest

from researchd.executors.base import ValidationFailure, validate_audit_result, validate_planner_result, validate_work_result


def test_work_result_valid():
    raw = {
        "schema": "researchd.work_result.v1",
        "task_id": "T-018",
        "outcome": "SUBMIT_FOR_REVIEW",
        "criteria_results": [{"criterion_id": "SC-1", "status": "PASS", "refs": ["artifact:a1", "evidence:e1"]}],
        "artifacts": [
            {"local_ref": "a1", "kind": "table", "path": "artifacts/field_subfield_comparison.parquet", "description": "x"}
        ],
        "evidence_candidates": [
            {
                "local_ref": "e1",
                "type": "analysis_result" if False else "computational",
                "statement": "two classification levels agree",
                "artifact_refs": ["a1"],
                "limitations": ["small strata differ"],
            }
        ],
        "claim_changes": [],
        "issues": [],
        "decision_candidates": [],
        "next_task_proposals": [],
    }
    result = validate_work_result(raw)
    assert result.outcome.value == "SUBMIT_FOR_REVIEW"
    assert result.criteria_results[0].status == "PASS"


def test_work_result_rejects_undeclared_artifact():
    raw = {
        "schema": "researchd.work_result.v1",
        "task_id": "T-018",
        "outcome": "SUBMIT_FOR_REVIEW",
        "criteria_results": [],
        "artifacts": [],
        "evidence_candidates": [
            {"local_ref": "e1", "type": "computational", "statement": "x", "artifact_refs": ["a-ghost"]}
        ],
        "claim_changes": [],
        "issues": [],
        "decision_candidates": [],
        "next_task_proposals": [],
    }
    with pytest.raises(ValidationFailure):
        validate_work_result(raw)


def test_work_result_rejects_bad_outcome():
    raw = {
        "schema": "researchd.work_result.v1",
        "task_id": "T-018",
        "outcome": "SUCCESS",
        "criteria_results": [],
        "artifacts": [],
        "evidence_candidates": [],
        "claim_changes": [],
        "issues": [],
        "decision_candidates": [],
        "next_task_proposals": [],
    }
    with pytest.raises(ValidationFailure):
        validate_work_result(raw)


def test_work_result_rejects_missing_fields():
    with pytest.raises(ValidationFailure):
        validate_work_result({"schema": "researchd.work_result.v1", "task_id": "T-1"})


def test_planner_result_valid():
    raw = {
        "schema": "researchd.planner_result.v1",
        "proposed_tasks": [
            {
                "task_id": "T-001",
                "role": "analysis_worker",
                "objective": "compare field classifications",
                "success_criteria": [{"id": "SC-1", "text": "reproducible"}],
                "depends_on": [],
            }
        ],
        "risks": ["field classification missingness"],
        "plan_revisions": [],
    }
    r = validate_planner_result(raw)
    assert r.proposed_tasks[0].task_id == "T-001"


def test_audit_result_verdicts():
    for verdict in ("ACCEPT", "REVISE", "BLOCK", "REJECT"):
        raw = {
            "schema": "researchd.audit_result.v1",
            "task_id": "T-018",
            "verdict": verdict,
            "checks": [{"check_id": "C1", "status": "PASS", "summary": "ok"}],
        }
        assert validate_audit_result(raw).verdict.value == verdict
    with pytest.raises(ValidationFailure):
        validate_audit_result(
            {"schema": "researchd.audit_result.v1", "task_id": "T-018", "verdict": "MAYBE", "checks": []}
        )


def test_fake_executor_scripted_schema_failure(tmp_path):
    from researchd.executors.fake import FakeExecutor

    ex = FakeExecutor(workspace_root=tmp_path)
    ex.script("worker", {"action": "return", "payload": {"schema": "researchd.work_result.v1", "task_id": "T-1", "outcome": "NOPE"}})
    with pytest.raises(ValidationFailure):
        import asyncio
        asyncio.run(ex.run_worker({"task": {"task_id": "T-1"}}, profile={}))


def test_fake_executor_default_result(tmp_path):
    import asyncio

    from researchd.executors.fake import FakeExecutor

    ex = FakeExecutor(workspace_root=tmp_path)
    result, session = asyncio.run(ex.run_worker({"task": {"task_id": "T-9"}}, profile={}))
    assert result.task_id == "T-9"
    assert session.session_id is not None
    assert ex.raw_outputs  # raw output is recorded (run dir only), never sent to Feishu
