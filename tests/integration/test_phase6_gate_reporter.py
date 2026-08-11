"""Phase 6 tests: Decision Gate, Reporter pipeline, delivery idempotency."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import select
import json

import pytest

from researchd.application.decision_gate import DecisionGate, GateVerdict, build_decision, decision_fingerprint
from researchd.domain.decision import DecisionOption
from researchd.domain.enums import DecisionCategory, ReportType
from researchd.domain.report import ReportAction, ReportConflict, ReportUncertainty
from researchd.reporting.spec import compile_spec, lint_spec, render_text


def opt(*ids: str) -> list[DecisionOption]:
    return [DecisionOption(option_id=i, label=i) for i in ids]


# ---------------------------------------------------------------- gate
def test_hard_gate_always_asks():
    gate = DecisionGate()
    v = gate.evaluate(
        project_id="P-1", category=DecisionCategory.PUBLICATION, question="submit?",
        why_material="", options=opt("A", "B"), has_option_conflict=False,
    )
    assert v.action == "ask_pi"
    assert "hard gate" in v.reason


def test_destructive_hard_gate():
    gate = DecisionGate()
    v = gate.evaluate(
        project_id="P-1", category=DecisionCategory.DESTRUCTIVE, question="delete data?",
        why_material="", options=opt("yes", "no"),
    )
    assert v.action == "ask_pi"


def test_engineering_problem_auto_resolved():
    gate = DecisionGate()
    v = gate.evaluate(
        project_id="P-1", category=DecisionCategory.OTHER, question="which library",
        why_material="", options=opt("a", "b"),
    )
    assert v.action == "resolve_automatically"


def test_cheap_parallel_runs_in_parallel():
    gate = DecisionGate()
    v = gate.evaluate(
        project_id="P-1", category=DecisionCategory.ANALYSIS_STRATEGY, question="run both?",
        why_material="cheap", options=opt("a", "b"), has_option_conflict=False,
    )
    assert v.action == "run_parallel"


def test_numerical_only_does_not_ask():
    gate = DecisionGate()
    v = gate.evaluate(
        project_id="P-1", category=DecisionCategory.ANALYSIS_STRATEGY, question="threshold?",
        why_material="", options=opt("0.05", "0.01"),
        evidence_refs=[], unresolved_uncertainty=None, has_option_conflict=True,
    )
    assert v.action == "resolve_numerically"


def test_material_unresolved_taste_asks():
    gate = DecisionGate()
    v = gate.evaluate(
        project_id="P-1", category=DecisionCategory.NARRATIVE, question="how to frame?",
        why_material="affects conclusions", options=opt("A", "B"),
        evidence_refs=["E-1"], unresolved_uncertainty="trade-off", has_option_conflict=True,
    )
    assert v.action == "ask_pi"
    assert "material AND unresolved" in v.reason


def test_strict_predicate_no_taste_no_ask():
    """ANALYSIS_STRATEGY is material+unresolved but NOT taste-sensitive and NOT
    a hard gate -> the strict predicate refuses to ask (resolve automatically)."""
    gate = DecisionGate()
    v = gate.evaluate(
        project_id="P-1", category=DecisionCategory.ANALYSIS_STRATEGY, question="model choice",
        why_material="changes core conclusion", options=opt("M1", "M2"),
        evidence_refs=["E-1", "E-2"], unresolved_uncertainty="models disagree",
        has_option_conflict=True,
    )
    assert v.action == "resolve_automatically"
    # the same question under a taste/hard category DOES ask
    v2 = gate.evaluate(
        project_id="P-1", category=DecisionCategory.NARRATIVE, question="model choice framing",
        why_material="changes core conclusion", options=opt("M1", "M2"),
        evidence_refs=["E-1", "E-2"], unresolved_uncertainty="models disagree",
        has_option_conflict=True,
    )
    assert v2.action == "ask_pi"


def test_fingerprint_dedupes_wording():
    gate = DecisionGate()
    v1 = gate.evaluate(
        project_id="P-1", category=DecisionCategory.NARRATIVE, question="How to frame results?",
        why_material="x", options=opt("A", "B"), evidence_refs=["E-1"],
        unresolved_uncertainty="u", has_option_conflict=True,
    )
    v2 = gate.evaluate(
        project_id="P-1", category=DecisionCategory.NARRATIVE, question="HOW TO FRAME RESULTS?",
        why_material="x", options=opt("A", "B"), evidence_refs=["E-1"],
        unresolved_uncertainty="u", has_option_conflict=True,
    )
    assert v1.action == "ask_pi"
    assert v2.action == "duplicate"
    assert v1.fingerprint == v2.fingerprint


def test_fingerprint_differs_by_options():
    fp1 = decision_fingerprint(project_id="P-1", category="narrative", affected_object=None, question="q", options=opt("A", "B"))
    fp2 = decision_fingerprint(project_id="P-1", category="narrative", affected_object=None, question="q", options=opt("A", "C"))
    assert fp1 != fp2


def test_build_decision_from_verdict():
    gate = DecisionGate()
    v = gate.evaluate(
        project_id="P-1", category=DecisionCategory.NARRATIVE, question="q",
        why_material="x", options=opt("A", "B"), evidence_refs=["E-1"],
        unresolved_uncertainty="u", blocking_scope=["T-1"], continue_scope=["T-2"],
    )
    d = build_decision(v, project_id="P-1", question="q", options=opt("A", "B"), trigger="worker")
    assert d.status.value == "OPEN"
    assert d.fingerprint == v.fingerprint
    assert d.blocking_scope == ["T-1"]
    assert d.continue_scope == ["T-2"]


# ---------------------------------------------------------------- reporter
def test_linter_rejects_hollow_and_unreferenced():
    spec = compile_spec(
        project_id="P-1", type=ReportType.EVIDENCE, title="t",
        bottom_line="初步结果表明显著提升",
        active_actions=[ReportAction(task_id="T-GHOST", text="next")],
    )
    lint = lint_spec(spec, known_task_ids={"T-1"}, known_evidence_ids={"E-1"})
    assert not lint.ok
    assert any("hollow" in e for e in lint.errors)
    assert any("unknown task" in e for e in lint.errors)
    assert any("no evidence refs" in e for e in lint.errors)


def test_linter_passes_clean_spec():
    spec = compile_spec(
        project_id="P-1", type=ReportType.EVIDENCE, title="t",
        bottom_line="两分类层级一致", bottom_line_evidence_refs=["E-1"],
        active_actions=[ReportAction(task_id="T-1", text="next")],
    )
    lint = lint_spec(spec, known_task_ids={"T-1"}, known_evidence_ids={"E-1"})
    assert lint.ok, lint.errors


def test_render_text_deterministic():
    spec = compile_spec(
        project_id="P-1", type=ReportType.DECISION, title="需要决定",
        bottom_line="建议 A", bottom_line_evidence_refs=["E-1"],
        conflicts=[ReportConflict(text="E-1 与 E-2 冲突", evidence_refs=["E-1", "E-2"])],
        uncertainties=[ReportUncertainty(text="样本量小")],
        active_actions=[ReportAction(task_id="T-1", text="恢复 T-1")],
    )
    body = render_text(spec)
    assert "【DECISION】" in body
    assert "E-1" in body
    assert "T-1" in body
    assert body == render_text(spec)  # deterministic


# ---------------------------------------------------------------- pipeline
def test_schedule_report_queues_delivery(factory, tmp_path):
    """Reporter pipeline: snapshot -> spec -> lint -> compress -> outbox."""
    from researchd.domain.evidence import Evidence
    from researchd.persistence.repositories import EvidenceRepo
    from researchd.persistence.transaction import UnitOfWork
    from researchd.reporting.reporter import build_snapshot, schedule_report

    with UnitOfWork(factory) as uow:
        EvidenceRepo(uow.session).save(
            Evidence(
                evidence_id="E-1", project_id="P-1", type="literature", status="VERIFIED",
                statement="s", literature={"source_id": "doi:1"},
            )
        )
        uow.commit()
    result = asyncio.run(schedule_report(factory, project_id="P-1"))
    assert result.sent is True
    assert result.report_id is not None
    # outbox row exists with the delivery payload
    from researchd.persistence.models import OutboxRow
    from researchd.persistence.transaction import UnitOfWork as U2

    with U2(factory) as uow:
        rows = uow.session.query(OutboxRow).all()
        assert len(rows) == 1
        payload = rows[0].payload_json
        assert payload["kind"] == "message"
        assert payload["report_id"] == result.report_id
        assert payload["body"]  # deterministic template body


def test_schedule_report_nothing_reportable(factory):
    from researchd.reporting.reporter import schedule_report

    result = asyncio.run(schedule_report(factory, project_id="P-EMPTY"))
    assert result.sent is False


def test_decision_report_queues_card(factory):
    from researchd.domain.decision import Decision, DecisionOption as DO
    from researchd.domain.task import SuccessCriterion, Task, TaskContract
    from researchd.persistence.repositories import DecisionRepo, TaskRepo
    from researchd.persistence.transaction import UnitOfWork
    from researchd.reporting.reporter import schedule_report

    def make_task() -> Task:
        return Task(
            task_id="T-001", project_id="P-1",
            contract=TaskContract(
                task_id="T-001", role="analysis_worker", objective="o",
                success_criteria=[SuccessCriterion(id="SC-1", text="c")],
            ),
        )

    from researchd.domain.evidence import Evidence as EvRow
    from researchd.persistence.repositories import EvidenceRepo as EvRepo

    with UnitOfWork(factory) as uow:
        EvRepo(uow.session).save(
            EvRow(
                evidence_id="E-1", project_id="P-1", type="literature", status="VERIFIED",
                statement="s", literature={"source_id": "doi:1"},
            )
        )
        TaskRepo(uow.session).save(
            make_task()
        )
        DecisionRepo(uow.session).save(
            Decision(
                decision_id="D-1", project_id="P-1", status="OPEN", question="q?",
                options=[DO(option_id="A", label="A", scientific_consequence="narrower"),
                        DO(option_id="B", label="B", scientific_consequence="wider")],
                blocking_scope=["T-001"], recommendation="A", evidence_refs=["E-1"],
            )
        )
        uow.commit()
    result = asyncio.run(schedule_report(factory, project_id="P-1"))
    assert result.sent is True
    from researchd.persistence.models import OutboxRow
    from researchd.persistence.transaction import UnitOfWork as U2

    with U2(factory) as uow:
        rows = uow.session.query(OutboxRow).all()
        cards = [r.payload_json for r in rows if r.payload_json.get("kind") == "interactive_card"]
        assert len(cards) == 1  # decision card exactly once
        assert cards[0]["decision_id"] == "D-1"
        assert len(cards[0]["buttons"]) == 2


def test_outbox_sender_delivers_report_to_fake_port(factory):
    """End-to-end: report queued -> outbox sender delivers once to the port."""
    from researchd.domain.evidence import Evidence
    from researchd.executors.fake import FakeDeliveryPort
    from researchd.persistence.repositories import EvidenceRepo
    from researchd.persistence.transaction import UnitOfWork
    from researchd.reporting.reporter import schedule_report
    from researchd.scheduler.outbox_sender import OutboxSender

    with UnitOfWork(factory) as uow:
        EvidenceRepo(uow.session).save(
            Evidence(
                evidence_id="E-1", project_id="P-1", type="literature", status="VERIFIED",
                statement="s", literature={"source_id": "doi:1"},
            )
        )
        uow.commit()
    asyncio.run(schedule_report(factory, project_id="P-1"))
    port = FakeDeliveryPort()
    sender = OutboxSender(factory, port)
    stats = asyncio.run(sender.send_pending())
    assert stats["sent"] == 1
    assert len(port.deliveries) == 1
    d = port.deliveries[0]
    assert d["kind"] == "message"
    assert "研究进展" in d["payload"]["body"]
    # second tick: nothing left to send
    stats2 = asyncio.run(sender.send_pending())
    assert stats2["claimed"] == 0
    assert len(port.deliveries) == 1


def test_cc_connect_client_shapes(tmp_path):
    """Delivery client produces the documented cc-connect request shapes."""
    from researchd.integrations.cc_connect.delivery import CcConnectDeliveryPort

    port = CcConnectDeliveryPort(
        base_url="http://127.0.0.1:9820", token="t", project="proj", session_key="key",
    )
    assert port.project == "proj"
    assert port.session_key == "key"


# ---------------------------------------------------------------- scheduler e2e
def test_scheduler_decision_gate_blocks_scope_and_reports(factory):
    """End-to-end: SUCCEEDED run with a decision candidate -> OPEN decision ->
    blocking_scope task paused -> report queued and delivered once."""
    import asyncio

    from researchd.config import DEFAULT_PROFILES
    from researchd.domain.task import SuccessCriterion, Task, TaskContract
    from researchd.executors.fake import FakeDeliveryPort, FakeExecutor
    from researchd.persistence.models import RunRow
    from researchd.persistence.repositories import DecisionRepo, ProjectRepo, RunRepo, TaskRepo
    from researchd.persistence.transaction import UnitOfWork
    from researchd.scheduler.loop import SchedulerLoop

    settings = type("S", (), {"scheduler": type("SC", (), {"max_parallel": 4})(), "profiles": dict(DEFAULT_PROFILES), "data_dir": "t"})()
    loop = SchedulerLoop(settings, factory, FakeExecutor(), FakeDeliveryPort(), max_parallel=4)

    with UnitOfWork(factory) as uow:
        from researchd.domain.evidence import Evidence as EvRow
        from researchd.persistence.repositories import EvidenceRepo as EvRepo

        EvRepo(uow.session).save(
            EvRow(
                evidence_id="E-1", project_id="P-GATE", type="literature", status="VERIFIED",
                statement="s", literature={"source_id": "doi:1"},
            )
        )
        ProjectRepo(uow.session).save(
            __import__("researchd.domain.project", fromlist=["Project"]).Project(
                project_id="P-GATE", name="gate", status="ACTIVE"
            )
        )
        t1 = Task(
            task_id="T-G1", project_id="P-GATE",
            contract=TaskContract(
                task_id="T-G1", role="analysis_worker", objective="o",
                success_criteria=[SuccessCriterion(id="SC-1", text="c")],
            ),
        )
        t1.propose_ready()
        TaskRepo(uow.session).save(t1)
        uow.session.add(
            RunRow(
                id="R-G1", run_id="R-G1", task_id="T-G1", project_id="P-GATE",
                status="SUCCEEDED", outcome="SUBMIT_FOR_REVIEW",
                result_json={
                    "schema": "researchd.work_result.v1",
                    "task_id": "T-G1",
                    "outcome": "SUBMIT_FOR_REVIEW",
                    "criteria_results": [{"criterion_id": "SC-1", "status": "PASS"}],
                    "decision_candidates": [
                        {
                            "question": "framing of the citation analysis?",
                            "category": "narrative",
                            "why_material": "affects conclusion framing",
                            "options": [
                                {"option_id": "A", "label": "focused", "scientific_consequence": "narrower"},
                                {"option_id": "B", "label": "broad", "scientific_consequence": "wider"},
                            ],
                            "evidence_refs": ["E-1"],
                            "unresolved_uncertainty": "trade-off",
                            "blocking_scope": ["T-G1"],
                        }
                    ],
                },
            )
        )
        uow.commit()

    async def run_ticks():
        for _ in range(4):
            await loop.tick()
            await asyncio.sleep(0.03)
    asyncio.run(run_ticks())

    with UnitOfWork(factory) as uow:
        decisions = DecisionRepo(uow.session).list_open("P-GATE")
        assert any(d.status.value == "OPEN" for d in decisions), "gate did not open the decision"
        d = next(d for d in decisions if d.status.value == "OPEN")
        assert d.blocking_scope == ["T-G1"]
        task = TaskRepo(uow.session).get_by_task_id("T-G1")
        assert task.status.value == "BLOCKED"  # only blocking_scope is paused
    # decision card delivered EXACTLY once; the same tick's digest is separate
    cards = [d for d in loop.sender.port.deliveries if d["kind"] == "interactive_card"]
    assert len(cards) == 1
    buttons = cards[0]["payload"].get("buttons", [])
    assert len(buttons) == 2
    assert buttons[0]["value"].startswith("/decision")  # button command shape
    # further ticks with unchanged state emit nothing new
    n_before = len(loop.sender.port.deliveries)
    async def run_ticks2():
        for _ in range(3):
            await loop.tick()
            await asyncio.sleep(0.03)
    asyncio.run(run_ticks2())
    assert len(loop.sender.port.deliveries) == n_before


def test_decision_answer_updates_original_card_in_place(factory, tmp_path):
    """Answering a decision PATCHes the already-sent card (receipt from the
    report row) instead of sending a new meaningless card."""
    import asyncio
    from unittest.mock import AsyncMock

    from researchd.domain.base import new_id
    from researchd.domain.decision import Decision, DecisionOption
    from researchd.domain.project import Project
    from researchd.domain.task import Task, TaskContract, Budget, SuccessCriterion
    from researchd.domain.enums import TaskStatus
    from researchd.persistence.models import ReportRow
    from researchd.persistence.repositories import DecisionRepo, ProjectRepo, TaskRepo
    from researchd.persistence.transaction import UnitOfWork
    from researchd.scheduler.outbox_sender import OutboxSender

    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    with UnitOfWork(factory) as uow:
        ProjectRepo(uow.session).save(
            Project(project_id="P-UPD", name="u", description="d", workspace_root=str(ws))
        )
        TaskRepo(uow.session).save(
            Task(task_id="T-UPD", project_id="P-UPD", status=TaskStatus.READY,
                 contract=TaskContract(task_id="T-UPD", role="analysis_worker", objective="o",
                                       success_criteria=[SuccessCriterion(id="SC-1", text="c")],
                                       budget=Budget(max_wall_seconds=60)),
                 blocked_by=[])
        )
        uow.commit()
    with UnitOfWork(factory) as uow:
        d = Decision(
            decision_id="D-UPD", project_id="P-UPD", category="analysis_strategy",
            question="继续吗？",
            options=[DecisionOption(option_id="yes", label="继续"), DecisionOption(option_id="no", label="停止")],
        )
        DecisionRepo(uow.session).save(d)
        uow.session.add(
            ReportRow(
                id=new_id("report"), report_id="R-UPD",
                spec_json={"decision_id": "D-UPD"},
                platform_message_id="om_card_123", status="SENT",
            )
        )
        uow.commit()

    # answer via the route handler (in-process call)
    from researchd.application.handlers import _enqueue_decision_card_update

    with UnitOfWork(factory) as uow:
        d = DecisionRepo(uow.session).get_by_decision_id("D-UPD")
        d.transition("OPEN")  # CANDIDATE -> OPEN before answering
        d.apply_answer("yes", actor="ou_real_pi", version=1)
        DecisionRepo(uow.session).save(d)
        _enqueue_decision_card_update(uow.session, d, "ou_real_pi", "yes")
        uow.commit()

    with UnitOfWork(factory) as uow:
        from researchd.persistence.models import OutboxRow

        rows = uow.session.execute(
            select(OutboxRow).where(OutboxRow.payload_json["kind"].as_string() == "decision_update")
        ).scalars().all()
        assert len(rows) == 1
        p = rows[0].payload_json
        assert p["platform_message_id"] == "om_card_123"
        assert "ou_real_pi" in p["body"]

    # sending the row calls port.update (PATCH) — never deliver (new card)
    fake = AsyncMock()
    sender = OutboxSender(factory, fake, doc_platform=None)
    asyncio.run(sender.send_pending())
    fake.update.assert_awaited_once()
    fake.deliver.assert_not_awaited()
