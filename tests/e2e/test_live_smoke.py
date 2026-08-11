"""Deterministic LIVE smoke: the minimal real research loop.

Local (always runs, FakeExecutor): fixed input file in the project workspace
-> planner task -> worker reads/analyzes/writes a REAL artifact -> path is
inside the workspace, hash computed -> analysis evidence candidate -> audit
ACCEPT -> evidence VERIFIED -> task COMPLETED (after REVIEW) -> forced
service restart -> recovery WITHOUT duplicate model calls or evidence.

Real (gated by RESEARCHD_RUN_REAL_SMOKE=1): identical scenario through the
real ReasonixAdapter (paid model calls — requires host authorization; only
then may the interdisciplinary-citation-pilot be started).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from researchd.config import Settings
from researchd.domain.base import new_id
from researchd.domain.project import Project
from researchd.executors.fake import FakeDeliveryPort, FakeExecutor
from researchd.persistence.repositories import (
    ArtifactRepo,
    DecisionRepo,
    EvidenceRepo,
    ProjectRepo,
    RunRepo,
    TaskRepo,
)
from researchd.persistence.transaction import UnitOfWork
from researchd.scheduler.loop import SchedulerLoop

PROJECT = "P-SMOKE"

INPUT_TEXT = "id,n\nrow1,3\nrow2,5\nrow3,8\n"


def _setup(factory, tmp_path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "input.csv").write_text(INPUT_TEXT)
    with UnitOfWork(factory) as uow:
        ProjectRepo(uow.session).save(
            Project(project_id=PROJECT, name="smoke", description="live smoke project", workspace_root=str(ws))
        )
        uow.commit()
    return ws


def _smoke_executor() -> FakeExecutor:
    """Scripted planner + worker that reads the fixed input, writes a REAL
    artifact, and submits a computational evidence candidate."""
    ex = FakeExecutor()
    ex.script("planner", {"payload": {
        "schema": "researchd.planner_result.v1",
        "proposed_tasks": [{
            "task_id": "T-SMOKE-1",
            "objective": "读取 input.csv，统计行数并写出结果",
            "deliverables": ["out/result.json"],
            "inputs": ["input.csv"],
            "success_criteria": [{"id": "sc-1", "text": "out/result.json 存在且可解析"}],
            "role": "analysis_worker",
            "executor_profile": "fake_worker",
        }],
        "risks": [],
        "plan_revisions": [],
    }})
    ex.script("worker", {"payload": {
        "schema": "researchd.work_result.v1",
        "task_id": "T-SMOKE-1",
        "outcome": "SUBMIT_FOR_REVIEW",
        "criteria_results": [{"criterion_id": "sc-1", "status": "PASS"}],
        "artifacts": [
            {"local_ref": "A-1", "kind": "analysis_result", "path": "out/result.json",
             "description": "输入文件行数统计结果"},
        ],
        "evidence_candidates": [
            {"local_ref": "E-1", "type": "computational", "statement": "input.csv 共 4 行记录",
             "artifact_refs": ["A-1"], "computational": {"statistics": {"rows": 4}}},
        ],
        "claim_changes": [], "issues": [], "decision_candidates": [],
        "next_task_proposals": [],
    }})
    # make the worker actually WRITE the artifact file (real artifact on disk)
    original = ex.run_worker

    async def _worker_with_file(context, *, profile):
        result, info = await original(context, profile=profile)
        ws = _workspace_for(context)
        out = ws / "out"
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.json").write_text(json.dumps({"rows": 4, "source": "input.csv"}))
        return result, info

    ex.run_worker = _worker_with_file  # type: ignore[method-assign]
    return ex


def _workspace_for(context) -> Path:
    return Path(context.get("workspace_root"))


def _tick(loop, n=12):
    stats = []
    for _ in range(n):
        stats.append(asyncio.run(loop.tick()))
    return stats


def test_live_smoke_full_loop_with_restart(factory, tmp_path):
    ws = _setup(factory, tmp_path)
    port = FakeDeliveryPort()
    executor = _smoke_executor()
    settings = Settings()
    loop = SchedulerLoop(settings, factory, executor, port, max_parallel=2)

    # phase 1: planner -> worker -> audit -> COMPLETED
    _tick(loop, 14)
    with UnitOfWork(factory) as uow:
        task = TaskRepo(uow.session).get_by_task_id("T-SMOKE-1")
        assert task.status.value == "COMPLETED"
        # artifact: real file, hash computed, path inside the workspace
        art = ArtifactRepo(uow.session).list_by_project(PROJECT)[0]
        assert art.path == "out/result.json"
        assert art.sha256
        from hashlib import sha256

        real_hash = sha256((ws / "out" / "result.json").read_bytes()).hexdigest()
        assert art.sha256 == real_hash
        assert not Path(art.path).is_absolute()  # stored relative to the workspace
        # evidence VERIFIED only after the audit (gate)
        ev = EvidenceRepo(uow.session).get_by_evidence_id("E-1")
        assert ev is not None and ev.status.value == "VERIFIED"
        # REVIEW happened before COMPLETED (audit event chain)
        from researchd.persistence.models import EventRow
        from sqlalchemy import select

        types = [e.event_type for e in uow.session.execute(select(EventRow)).scalars()]
        assert "task.review_submitted" in types
        assert "audit.accepted" in types
        assert "task.completed" in types
        assert types.index("task.review_submitted") < types.index("task.completed")
        model_calls_before = executor.call_count()

    # phase 2: FORCED restart (fresh loop object == process restart) with a
    # fresh executor (no scripts, no calls permitted) — nothing re-runs,
    # nothing duplicates
    executor2 = FakeExecutor()
    loop2 = SchedulerLoop(Settings(), factory, executor2, FakeDeliveryPort(), max_parallel=2)
    _tick(loop2, 6)
    with UnitOfWork(factory) as uow:
        task = TaskRepo(uow.session).get_by_task_id("T-SMOKE-1")
        assert task.status.value == "COMPLETED"
        evs = [e for e in EvidenceRepo(uow.session).list_by_project(PROJECT) if e.evidence_id == "E-1"]
        assert len(evs) == 1  # exactly-once evidence
        arts = ArtifactRepo(uow.session).list_by_project(PROJECT)
        assert len(arts) == 1  # exactly-once artifact
        runs = RunRepo(uow.session).list_by_project(PROJECT)
        worker_runs = [r for r in runs if (r.metadata or {}).get("role") != "auditor"]
        assert len(worker_runs) == 1  # no duplicate model call
    assert executor2.call_count() == 0  # restart with a fresh executor: no calls


def test_worker_declares_path_outside_workspace_fails_closed(factory, tmp_path):
    """A WorkResult whose artifact path escapes the workspace must be
    rejected — the run FAILS, nothing is registered."""
    ws = _setup(factory, tmp_path)
    ex = FakeExecutor()
    ex.script("planner", {"payload": {
        "schema": "researchd.planner_result.v1", "proposed_tasks": [
            {"task_id": "T-ESC", "objective": "x", "deliverables": ["../evil.txt"],
             "success_criteria": [{"id": "sc", "text": "done"}], "role": "analysis_worker"},
        ],
        "risks": [], "plan_revisions": [],
    }})
    ex.script("worker", {"payload": {
        "schema": "researchd.work_result.v1", "task_id": "T-ESC", "outcome": "SUBMIT_FOR_REVIEW",
        "criteria_results": [{"criterion_id": "sc", "status": "PASS"}],
        "artifacts": [{"local_ref": "A-1", "kind": "analysis_result", "path": "../evil.txt", "description": "escape"}],
        "evidence_candidates": [], "claim_changes": [], "issues": [],
        "decision_candidates": [], "next_task_proposals": [],
    }})
    loop = SchedulerLoop(Settings(), factory, ex, FakeDeliveryPort(), max_parallel=2)
    _tick(loop, 10)
    with UnitOfWork(factory) as uow:
        task = TaskRepo(uow.session).get_by_task_id("T-ESC")
        assert task.status.value == "FAILED"
        assert "escape" in (task.error_message or "") or "workspace" in (task.error_message or "")
        arts = ArtifactRepo(uow.session).list_by_project(PROJECT)
        assert arts == []  # nothing registered
        assert not (tmp_path / "evil.txt").exists()  # nothing written outside


def test_real_reasonix_live_smoke(factory, tmp_path):
    """REAL smoke: reasonix worker + real models + real auditor. Gated on
    RESEARCHD_RUN_REAL_SMOKE=1 (paid model calls need host authorization)."""
    if not os.environ.get("RESEARCHD_RUN_REAL_SMOKE") == "1":
        pytest.skip("real reasonix smoke requires RESEARCHD_RUN_REAL_SMOKE=1 (paid model authorization)")
    from researchd.executors.reasonix.adapter import ReasonixAdapter

    ws = _setup(factory, tmp_path)
    settings = Settings()
    settings.data_dir = str(tmp_path / "data")
    adapter = ReasonixAdapter(settings, overlay_dir=str(tmp_path / "data"))
    port = FakeDeliveryPort()
    loop = SchedulerLoop(settings, factory, adapter, port, max_parallel=1)

    async def _scenario(timeout_s: float = 480.0):
        import time

        deadline = time.monotonic() + timeout_s
        last = ""
        while time.monotonic() < deadline:
            await loop.tick()
            with UnitOfWork(factory) as uow:
                task = TaskRepo(uow.session).get_by_task_id("T-SMOKE-1")
                if task is not None and task.status.value in ("COMPLETED", "FAILED"):
                    return task.status.value
            await asyncio.sleep(3)
        return last

    import asyncio as _aio

    final = _aio.run(_scenario())
    assert final == "COMPLETED", f"real smoke ended {final!r}"
    with UnitOfWork(factory) as uow:
        task = TaskRepo(uow.session).get_by_task_id("T-SMOKE-1")
        assert task.status.value == "COMPLETED"
        ev = EvidenceRepo(uow.session).get_by_evidence_id("E-1")
        assert ev is not None and ev.status.value == "VERIFIED"
        runs = RunRepo(uow.session).list_by_project(PROJECT)
        roles = [(r.task_id, (r.metadata or {}).get("role")) for r in runs]
        assert ("T-SMOKE-1", "auditor") in roles
        # the worker run recorded the resolved model + skills
        worker_runs = [r for r in runs if (r.metadata or {}).get("role") != "auditor"]
        assert worker_runs and worker_runs[0].resolved_model
    import asyncio as aio

    aio.run(adapter.close())
