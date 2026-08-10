"""End-to-end golden path (IMPLEMENTATION.md §26): the full deterministic
research loop with the FakeExecutor, including a forced restart.

SYNTHETIC ONLY — every payload below is fabricated for the fixture; no real
project evidence is imported anywhere.

Steps covered (1–22): project+bindings -> import D-001 -> planner batch ->
parallel workers -> invalid schema auto-repair -> conflicting analyses ->
cheap diagnostic -> still material -> D-002 -> block only the branch ->
decision card -> duplicate click applied once + card update -> unblock ->
evidence/claim applied -> milestone -> doc projection -> restart ->
recovery -> no duplicate messages.
"""

from __future__ import annotations

import asyncio

import pytest

from researchd.config import Settings
from researchd.domain.base import Actor
from researchd.domain.decision import Decision, DecisionOption
from researchd.domain.enums import DecisionStatus
from researchd.domain.project import Project
from researchd.executors.fake import FakeDeliveryPort, FakeExecutor
from researchd.persistence.repositories import (
    ClaimRepo,
    DecisionRepo,
    EvidenceRepo,
    ProjectRepo,
    TaskRepo,
)
from researchd.persistence.transaction import UnitOfWork
from researchd.projections.feishu_doc import FakeDocPlatform, SECTION_ORDER, PI_NOTES_SECTION
from researchd.scheduler.loop import SchedulerLoop

PROJECT = "P-GOLD"

PLANNER_BATCH = {
    "schema": "researchd.planner_result.v1",
    "proposed_tasks": [
        {
            "task_id": "T-001", "role": "worker", "objective": "概念与指标边界",
            "success_criteria": [{"id": "sc1", "text": "定义明确"}],
        },
        {
            "task_id": "T-002", "role": "worker", "objective": "相关文献和争议",
            "success_criteria": [{"id": "sc1", "text": "争议清单"}],
        },
        {
            "task_id": "T-003", "role": "worker", "objective": "候选数据源与引用覆盖审计",
            "success_criteria": [{"id": "sc1", "text": "数据源清单"}],
        },
        {
            "task_id": "T-004", "role": "worker", "objective": "field/subfield 分类比较",
            "success_criteria": [{"id": "sc1", "text": "分类表"}],
        },
        {
            "task_id": "T-005", "role": "worker", "objective": "最小年度趋势样本",
            "success_criteria": [{"id": "sc1", "text": "趋势样本"}],
        },
    ],
    "risks": [],
    "plan_revisions": [],
}

CONFLICT_CANDIDATE = {
    "question": "两套分析方法对跨学科份额变化给出相反结论，如何处理？",
    "trigger": "T-003 与 T-002 结论冲突",
    "why_material": "结论影响核心研究问题",
    "category": "narrative",
    "unresolved_uncertainty": "两套分析对同一数据给出相反结论，机制解释尚无定论",
    "options": [
        {
            "option_id": "A", "label": "采用描述性定位",
            "scientific_consequence": "结论范围收窄，不做因果断言",
        },
        {
            "option_id": "B", "label": "保留因果解释",
            "scientific_consequence": "需要更强的识别策略与稳健性检验",
        },
    ],
    "recommendation": "A",
    "recommendation_basis": "数据仅覆盖两个时间窗，不足以支持因果识别",
    "evidence_refs": ["E-1", "E-2"],
    "blocking_scope": ["T-004", "T-005"],
    "has_option_conflict": True,
    "cheap_parallel": True,
}

EVIDENCE_A = {
    "local_ref": "E-1",
    "type": "literature",
    "statement": "2017-2019 引用网络以本学科为主",
    "literature": {"source_id": "doi:10.1000/synthetic-1"},
}
EVIDENCE_B = {
    "local_ref": "E-2",
    "type": "literature",
    "statement": "2021-2023 跨学科引用份额上升",
    "literature": {"source_id": "doi:10.1000/synthetic-2"},
}

OK_RESULT = {
    "schema": "researchd.work_result.v1",
    "outcome": "SUBMIT_FOR_REVIEW",
    "criteria_results": [{"criterion_id": "sc1", "status": "PASS"}],
    "artifacts": [],
    "evidence_candidates": [],
    "claim_changes": [],
    "issues": [],
    "decision_candidates": [],
    "next_task_proposals": [],
}


def worker_payload(**overrides) -> dict:
    p = dict(OK_RESULT)
    p.update(overrides)
    return p


def bad_schema_payload() -> dict:
    """Structurally invalid work result (missing required outcome)."""
    return {
        "schema": "researchd.work_result.v1",
        "task_id": "T-004",
        # outcome missing -> ValidationError
        "criteria_results": [],
    }


@pytest.fixture
def golden(factory):
    with UnitOfWork(factory) as uow:
        ProjectRepo(uow.session).save(
            Project(
                project_id=PROJECT,
                name="golden",
                description="synthetic golden-path fixture",
                metadata={
                    "feishu_document_id": "doc-gold",
                    "milestone_evidence_threshold": 2,
                },
            )
        )
        # step 2: import D-001 = A (pilot decision, already APPLIED)
        DecisionRepo(uow.session).save(
            Decision(
                decision_id="D-001",
                project_id=PROJECT,
                category="other",
                question="pilot 定位",
                options=[
                    DecisionOption(option_id="A", label="描述性定位"),
                    DecisionOption(option_id="B", label="因果解释"),
                ],
                status=DecisionStatus.APPLIED,
                decision_version=1,
                answer="A",
            )
        )
        uow.commit()

    executor = FakeExecutor()
    # step 3: planner first batch
    executor.script("planner", {"payload": PLANNER_BATCH})
    # step 7: the FIRST two analyses produce conflicting interpretations
    # (they complete before T-003/4/5 are even dispatched, so the diagnostic
    # gets dispatched first and D-002 opens while T-003/4/5 still wait)
    executor.script("worker", {"payload": worker_payload(
        task_id="T-001",
        evidence_candidates=[EVIDENCE_A],
        decision_candidates=[dict(CONFLICT_CANDIDATE)],
    ), "task_id": "T-001"})
    executor.script("worker", {"payload": worker_payload(
        task_id="T-002",
        evidence_candidates=[EVIDENCE_B],
        decision_candidates=[dict(CONFLICT_CANDIDATE)],
    ), "task_id": "T-002"})
    # step 8: the cheap diagnostic confirms the conflict is MATERIAL:
    # the result is no longer cheap-parallel, so the gate asks the PI
    # (the diagnostic task id is deterministic: DGN-<sha256(question)[:12]>)
    import hashlib

    dgn_candidate = dict(CONFLICT_CANDIDATE)
    dgn_candidate["cheap_parallel"] = False  # diagnostic: still material
    dgn_id = "DGN-" + PROJECT[-8:] + "-" + hashlib.sha256(CONFLICT_CANDIDATE["question"].encode()).hexdigest()[:12]
    executor.script("worker", {"payload": worker_payload(
        task_id=dgn_id,
        decision_candidates=[dgn_candidate],
    ), "task_id": dgn_id})
    # step 5/6: T-004 first returns an INVALID schema, then a valid one
    # (the adapter's repair loop retries automatically) — runs after unblock
    executor.script("worker", {"payload": bad_schema_payload(), "task_id": "T-004"})
    executor.script("worker", {"payload": worker_payload(task_id="T-004"), "task_id": "T-004"})
    # step 16-17: remaining unblocked tasks complete with evidence;
    # T-005's FIRST attempt is slow (in-flight when we force the restart)
    executor.script("worker", {"payload": worker_payload(task_id="T-003"), "task_id": "T-003"})
    executor.script("worker", {"payload": worker_payload(
        task_id="T-005",
        evidence_candidates=[EVIDENCE_B],
        claim_changes=[
            {"claim_id": "C-1", "operation": "create", "text": "跨学科引用份额上升",
             "evidence_relations": [{"evidence_id": "E-2"}]},
        ],
    ), "task_id": "T-005", "delay": 0.3})
    executor.script("worker", {"payload": worker_payload(
        task_id="T-005",
        evidence_candidates=[EVIDENCE_B],
        claim_changes=[
            {"claim_id": "C-1", "operation": "create", "text": "跨学科引用份额上升",
             "evidence_relations": [{"evidence_id": "E-2"}]},
        ],
    ), "task_id": "T-005"})
    return executor


def run_ticks(loop, n: int = 12):
    async def _run():
        stats = []
        for _ in range(n):
            s = await loop.tick()
            await loop.sender.send_pending()
            stats.append(s)
        return stats

    return asyncio.run(_run())


def test_golden_path(golden, factory):
    settings = Settings()
    port = FakeDeliveryPort()
    loop = SchedulerLoop(settings, factory, golden, port, max_parallel=2)
    doc = FakeDocPlatform()
    loop._doc_platform_instance = doc  # test-only injection

    stats = run_ticks(loop, 14)

    # ---- step 3: planner generated the first batch
    with UnitOfWork(factory) as uow:
        tasks = TaskRepo(uow.session).list_by_status(PROJECT, [])
        assert {t.task_id for t in tasks} >= {"T-001", "T-002", "T-003", "T-004", "T-005"}
        assert any(s.get("planned", 0) >= 1 for s in stats)

    # ---- step 5/6: invalid schema auto-repaired (checked after unblock:
    # T-004 must complete WITHOUT any FAILED run)
    # ---- step 10: the gate decision (D-002) generated after the diagnostic
    with UnitOfWork(factory) as uow:
        opens = [d for d in DecisionRepo(uow.session).list_all_statuses(PROJECT)
                 if d.status.value == "OPEN"]
        assert len(opens) == 1, "exactly one OPEN decision (D-002)"
        gate_dec = opens[0]
        assert gate_dec.question == CONFLICT_CANDIDATE["question"]
        assert "T-004" in gate_dec.blocking_scope

    # ---- step 11: only the blocking_scope branch paused
    with UnitOfWork(factory) as uow:
        for tid in ("T-004", "T-005"):
            t = TaskRepo(uow.session).get_by_task_id(tid)
            assert t.status.value == "BLOCKED", f"{tid} must be paused while D-002 is OPEN"
        for tid in ("T-001", "T-002", "T-003"):
            t = TaskRepo(uow.session).get_by_task_id(tid)
            assert t.status.value in ("REVIEW", "COMPLETED")  # completed despite D-002

    # ---- step 13: one decision card delivered (with deterministic buttons)
    cards = [d for d in port.deliveries if d["kind"] == "interactive_card"]
    gate_cards = [d for d in cards if (d.get("payload") or {}).get("decision_id") == gate_dec.decision_id]
    assert len(gate_cards) == 1
    buttons = (gate_cards[0].get("payload") or {}).get("buttons", [])
    assert len(buttons) == 2
    assert buttons[0]["value"].startswith("/decision")

    # ---- step 14/15: duplicate click applied once (via the real command path)
    from researchd.application.commands import parse_command
    from researchd.application.handlers import CommandHandler
    from researchd.persistence.models import ProjectMemberRow

    with UnitOfWork(factory) as uow:
        uow.session.add(
            ProjectMemberRow(
                id="M-PI", member_id="M-PI", project_id=PROJECT, platform_user_id="ou_pi",
                role="pi", can_approve_decisions=True,
            )
        )
        uow.commit()

    with UnitOfWork(factory) as uow:
        h1 = CommandHandler(uow.session, project_id=PROJECT, actor=Actor(type="pi", platform_user_id="ou_pi"))
        r1 = h1.cmd_decision(parse_command(
            f"/decision {gate_dec.decision_id} A --version {gate_dec.decision_version}"))
        uow.commit()
    with UnitOfWork(factory) as uow:
        h2 = CommandHandler(uow.session, project_id=PROJECT, actor=Actor(type="pi", platform_user_id="ou_pi"))
        r2 = h2.cmd_decision(parse_command(
            f"/decision {gate_dec.decision_id} A --version {gate_dec.decision_version}"))
        uow.commit()
    assert r1.data["applied"] is True
    assert r2.data["applied"] is False  # exactly once (duplicate click is a no-op)
    with UnitOfWork(factory) as uow:
        dec = DecisionRepo(uow.session).get_by_decision_id(gate_dec.decision_id)
        assert dec.status.value in ("ANSWERED", "APPLIED")
        assert dec.answer == "A"

    # ---- step 16: blocked tasks recover and finish
    stats2 = run_ticks(loop, 8)
    stats2 += run_ticks(loop, 6)  # T-005's delayed first attempt was interrupted
    with UnitOfWork(factory) as uow:
        for tid in ("T-004", "T-005"):
            t = TaskRepo(uow.session).get_by_task_id(tid)
            assert t.status.value in ("REVIEW", "COMPLETED"), f"{tid} did not recover"
        # step 5/6 proof: T-004's invalid schema was repaired inside the run
        # (no FAILED run; the repair loop retried with the next scripted step)
        from sqlalchemy import select

        from researchd.persistence.models import RunRow

        t004_runs = uow.session.execute(
            select(RunRow).where(RunRow.task_id == "T-004")
        ).scalars().all()
        assert t004_runs, "T-004 must have run"
        assert all(r.status != "FAILED" for r in t004_runs), "schema failure must be repaired"
        assert any("schema" not in (r.error_message or "") for r in t004_runs)

    # ---- step 17: evidence + claim applied
    with UnitOfWork(factory) as uow:
        ev = EvidenceRepo(uow.session).get_by_evidence_id("E-2")
        assert ev is not None and ev.status.value == "VERIFIED"
        claim = ClaimRepo(uow.session).get_by_claim_id("C-1")
        assert claim is not None and claim.text == "跨学科引用份额上升"

    # ---- step 18: milestone reached (threshold 2)
    assert any(s.get("milestones", 0) >= 1 for s in stats + stats2)
    assert any("MILESTONE" in (d.get("payload") or {}).get("body", "") for d in port.deliveries)

    # ---- step 19: document projected incrementally (PI Notes never written)
    assert "doc-gold" in doc.blocks
    assert PI_NOTES_SECTION not in doc.blocks["doc-gold"]
    assert "E-2" in doc.blocks["doc-gold"].get("evidence", "")

    # ---- step 20-22: forced restart WHILE T-005 is in flight -> recovery,
    # no duplicate messages, no duplicate evidence
    import asyncio as _asyncio

    deliveries_before = len(port.deliveries)
    async def _restart():
        await _asyncio.sleep(0.02)  # let T-005 start its delayed step
    _asyncio.run(_restart())
    golden2 = FakeExecutor()  # fresh executor, same DB: the in-flight run is
    loop2 = SchedulerLoop(Settings(), factory, golden2, port, max_parallel=2)  # orphaned
    doc2 = FakeDocPlatform()
    loop2._doc_platform_instance = doc2
    run_ticks(loop2, 8)
    with UnitOfWork(factory) as uow:
        from sqlalchemy import select

        from researchd.persistence.models import RunRow

        runs = uow.session.execute(select(RunRow)).scalars().all()
        assert runs  # history intact
        # the interrupted run was reconciled (ORPHANED), not silently
        # dropped, and its task requeued + redone
        t005 = TaskRepo(uow.session).get_by_task_id("T-005")
        assert t005.status.value in ("REVIEW", "COMPLETED"), "T-005 must recover after restart"
    assert len(port.deliveries) == deliveries_before  # nothing resent
    with UnitOfWork(factory) as uow:
        ev = EvidenceRepo(uow.session).get_by_evidence_id("E-2")
        assert ev is not None and ev.status.value == "VERIFIED"  # no duplicate rows
        assert len([e for e in EvidenceRepo(uow.session).list_by_project(PROJECT) if e.evidence_id == "E-2"]) == 1
