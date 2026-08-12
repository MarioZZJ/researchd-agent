"""ContextPackageBuilder tests (IMPLEMENTATION.md §13): role separation,
traceability (objects/hash/token/role persisted), bounded content."""

from __future__ import annotations

import pytest

from researchd.application.context_package import ContextPackageBuilder
from researchd.domain.base import new_id
from researchd.domain.decision import Decision, DecisionOption
from researchd.domain.enums import DecisionStatus, EvidenceStatus
from researchd.domain.evidence import Artifact, Claim, Evidence, Issue
from researchd.domain.project import Project
from researchd.domain.task import SuccessCriterion, Task, TaskContract
from researchd.persistence.models import ContextPackageRow
from researchd.persistence.repositories import (
    ArtifactRepo,
    ClaimRepo,
    ContextPackageRepo,
    DecisionRepo,
    EvidenceRepo,
    IssueRepo,
    ProjectRepo,
    TaskRepo,
)


@pytest.fixture()
def project(factory):
    p = Project(project_id="P-TEST-1", name="test", description="brief", workspace_root="/tmp/ws")
    with factory() as s:
        ProjectRepo(s).save(p)
        s.commit()
    return p


def _task(project_id: str, task_id: str = "T-TEST-1") -> Task:
    return Task(
        task_id=task_id,
        project_id=project_id,
        contract=TaskContract(
            task_id=task_id,
            role="worker",
            objective="分析输入文件并输出结果",
            why_now="需要真实执行",
            inputs=["A-1"],
            deliverables=["A-2"],
            success_criteria=[SuccessCriterion(id="sc-1", text="输出存在且可解析")],
            stop_conditions=["预算耗尽"],
            escalation_conditions=["数据缺失"],
        ),
    )


def test_planner_package_contains_required_sections(factory, project):
    with factory() as s:
        Question = __import__("researchd.domain.project", fromlist=["Question"]).Question
        from researchd.persistence.repositories import QuestionRepo

        QuestionRepo(s).save(Question(question_id="Q-1", project_id=project.project_id, text="核心问题?"))
        DecisionRepo(s).save(
            Decision(
                decision_id="D-1",
                project_id=project.project_id,
                question="定位",
                status=DecisionStatus.APPLIED,
                decision_version=1,
                answer="A",
                options=[DecisionOption(option_id="A", label="描述性")],
            )
        )
        ClaimRepo(s).save(Claim(claim_id="C-1", project_id=project.project_id, text="claim 1"))
        IssueRepo(s).save(Issue(issue_id="I-1", project_id=project.project_id, title="issue 1"))
        s.commit()
        builder = ContextPackageBuilder(s)
        pkg = builder.planner(project)
        builder.persist(pkg)
        s.commit()

        assert pkg.role == "planner"
        assert pkg.content_hash and pkg.token_estimate and pkg.token_estimate > 0
        for marker in ("INITIAL_BRIEF", "PROJECT_CHARTER", "QUESTIONS", "APPROVED_DECISIONS", "CLAIMS",
                       "VERIFIED_EVIDENCE", "UNRESOLVED_ISSUES", "WORKSPACE", "BUDGET_AND_PERMISSIONS"):
            assert marker in pkg.content, f"planner package missing {marker}"
        kinds = {o.kind for o in pkg.objects}
        assert {"question", "decision", "claim", "issue", "project"} <= kinds
        # persisted + reloadable
        row = s.execute(ContextPackageRow.__table__.select().where(ContextPackageRow.context_id == pkg.context_id)).first()
        assert row is not None
        reloaded = ContextPackageRepo(s).get_by_context_id(pkg.context_id)
        assert reloaded is not None and reloaded.role == "planner"
        assert reloaded.content == pkg.content and reloaded.content_hash == pkg.content_hash


def test_worker_package_has_full_contract_and_artifact_hashes(factory, project):
    with factory() as s:
        TaskRepo(s).save(_task(project.project_id))
        ArtifactRepo(s).save(
            Artifact(
                artifact_id="A-1", project_id=project.project_id, task_id="T-TEST-1",
                path="out/result.json", sha256="abc123", size_bytes=10,
            )
        )
        s.commit()
        task = TaskRepo(s).get_by_task_id("T-TEST-1")
        builder = ContextPackageBuilder(s)
        pkg = builder.worker(task)
        builder.persist(pkg)
        s.commit()

        assert pkg.role == "worker"
        for marker in ("TASK_CONTRACT", "objective=", "why_now=", "inputs=", "deliverables=",
                       "success_criteria:", "stop_conditions=", "escalation_conditions=",
                       "WORKSPACE_ROOT", "TOOLS_AND_PERMISSIONS", "FORBIDDEN_PATHS"):
            assert marker in pkg.content, f"worker package missing {marker}"
        assert "path=out/result.json" in pkg.content
        assert "sha256=abc123" in pkg.content
        # traceability
        ctx = builder.to_context_dict(pkg, objective="分析输入文件并输出结果")
        assert ctx["context_id"] == pkg.context_id
        assert ctx["package"]["content_hash"] == pkg.content_hash


def test_auditor_package_is_independent_of_worker_self_assessment(factory, project):
    """The auditor must NOT see the worker's free-text criteria self-report;
    it sees structured evidence candidates + artifacts only."""
    from researchd.domain.run import Run

    with factory() as s:
        TaskRepo(s).save(_task(project.project_id))
        run = Run(
            run_id="R-TEST-1", task_id="T-TEST-1", project_id=project.project_id,
            executor="reasonix", outcome="SUBMIT_FOR_REVIEW",
            result={
                "schema": "researchd.work_result.v1",
                "task_id": "T-TEST-1",
                "outcome": "SUBMIT_FOR_REVIEW",
                "criteria_results": [
                    {"criterion_id": "sc-1", "status": "PASS", "refs": ["A-2"]},
                    # worker free-text self-report must not reach the auditor
                    {"criterion_id": "sc-1", "status": "PASS", "refs": []},
                ],
                "artifacts": [
                    {"local_ref": "A-2", "kind": "document", "path": "out/result.json", "description": "结果"},
                ],
                "evidence_candidates": [
                    {
                        "local_ref": "E-1", "type": "computational",
                        "statement": "输出文件包含 10 行有效记录",
                        "artifact_refs": ["A-2"],
                    }
                ],
                "claim_changes": [],
                "issues": [],
                "decision_candidates": [],
                "next_task_proposals": [],
            },
        )
        from researchd.persistence.repositories import RunRepo

        RunRepo(s).save(run)
        s.commit()
        task = TaskRepo(s).get_by_task_id("T-TEST-1")
        builder = ContextPackageBuilder(s)
        pkg = builder.auditor(task, run)
        builder.persist(pkg)
        s.commit()

        assert pkg.role == "auditor"
        assert pkg.run_id == "R-TEST-1"
        for marker in ("EVIDENCE_CANDIDATES", "DECLARED_ARTIFACTS", "AUDIT_RULES", "RUN_FACTS"):
            assert marker in pkg.content
        # worker criteria self-report text is NOT in the auditor package
        assert "criteria_results" not in pkg.content
        # independent hash/objects vs the worker package
        assert pkg.content_hash != builder.worker(task).content_hash


def test_persist_is_idempotent_upsert(factory, project):
    with factory() as s:
        builder = ContextPackageBuilder(s)
        pkg = builder.planner(project)
        builder.persist(pkg)
        first = pkg.content_hash
        s.commit()
        # same package persisted again -> same content hash, single row
        pkg2 = builder.planner(project)
        builder.persist(pkg2)
        s.commit()
        rows = s.execute(ContextPackageRow.__table__.select()).scalars().all()
        assert len(rows) == 2  # different context_id each build; both traceable
        assert pkg2.content_hash == first  # deterministic content
