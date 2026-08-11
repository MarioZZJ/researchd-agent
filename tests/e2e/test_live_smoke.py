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
import signal
import subprocess
import sys
import time
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


def _setup(factory, tmp_path, *, description: str = "live smoke project") -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "input.csv").write_text(INPUT_TEXT)
    with UnitOfWork(factory) as uow:
        ProjectRepo(uow.session).save(
            Project(project_id=PROJECT, name="smoke", description=description, workspace_root=str(ws))
        )
        uow.commit()
    return ws


# The real planner decides the task id, role and wording — the brief must be
# explicit enough that the model plans EXACTLY ONE task whose deliverable is
# out/result.json; the test then reads the ACTUAL persisted task id instead of
# waiting on a hard-coded one.
REAL_PROJECT_BRIEF = (
    "只规划一个任务（不要规划其他任务）：读取 workspace 根目录下的 input.csv，"
    "统计其非空数据行数（含表头共 4 行），把统计结果写入 out/result.json，"
    "并提交一条 computational evidence。"
)


def _safe_state_summary(factory, *, project_id: str = PROJECT) -> dict:
    """Structured state ONLY — never transcripts, credentials or raw model
    output. Used by fail-fast/timeout paths so a failure can be diagnosed
    from the DB alone."""
    with UnitOfWork(factory) as uow:
        from researchd.persistence.repositories import ArtifactRepo as AR
        from researchd.persistence.repositories import EvidenceRepo as ER
        from researchd.persistence.repositories import InvocationRepo as IR
        from researchd.persistence.repositories import RunRepo as RR
        from researchd.persistence.repositories import TaskRepo as TR

        def _st(v):
            return getattr(v, "value", v)

        tasks = TR(uow.session).list_by_project(project_id)
        runs = RR(uow.session).list_by_project(project_id)
        invs = IR(uow.session).list_by_project(project_id)
        arts = AR(uow.session).list_by_project(project_id)
        evs = ER(uow.session).list_by_project(project_id)
        return {
            "tasks": [(t.task_id, _st(t.status), (getattr(t, "error_message", None) or "")[:120]) for t in tasks],
            "runs": [(r.task_id, (r.metadata or {}).get("role"), _st(r.status), (getattr(r, "error_message", None) or "")[:100]) for r in runs],
            "invocations": [(i.invocation_id, i.role, _st(i.status)) for i in invs],
            "artifacts": [(a.path, (a.sha256 or "")[:12], a.size_bytes) for a in arts],
            "evidence": [(e.evidence_id, _st(e.status)) for e in evs],
        }


async def _real_scenario(loop, factory, *, timeout_s: float = 900.0, planner_window_s: float = 600.0):
    """Wait SEMANTICALLY for the real loop: read whatever task the real
    planner created; fail FAST (with a safe state summary) instead of
    spinning until the deadline when the planner invocation FAILED, produced
    no task within the planner window, or a task failed.

    planner_window_s bounds how long we wait for the FIRST task; it must
    cover the longest plausible single planner call (transport timeout is
    600s), while a FAILED planner invocation fails immediately."""
    deadline = time.monotonic() + timeout_s
    start = time.monotonic()
    while time.monotonic() < deadline:
        await loop.tick()
        with UnitOfWork(factory) as uow:
            from researchd.persistence.repositories import InvocationRepo as IR

            invs = IR(uow.session).list_by_project(PROJECT)
            failed_inv = [i for i in invs if getattr(i.status, "value", i.status) == "FAILED"]
            if failed_inv:
                raise AssertionError(
                    f"real smoke: invocation FAILED ({failed_inv[0].role}) "
                    f"— state={_safe_state_summary(factory)}"
                )
            tasks = TaskRepo(uow.session).list_by_project(PROJECT)
            if tasks:
                statuses = {getattr(t.status, "value", t.status) for t in tasks}
                if "FAILED" in statuses:
                    raise AssertionError(
                        f"real smoke: a task FAILED — state={_safe_state_summary(factory)}"
                    )
                if all(st == "COMPLETED" for st in statuses):
                    return tasks
            elif time.monotonic() - start > planner_window_s:
                # planner window exhausted and the planner never produced a
                # task: planner returned zero tasks (no invocation FAILED)
                raise AssertionError(
                    f"real smoke: planner produced no task within {planner_window_s:.0f}s "
                    f"— state={_safe_state_summary(factory)}"
                )
        await asyncio.sleep(3)
    raise AssertionError(
        f"real smoke timed out after {timeout_s:.0f}s — state={_safe_state_summary(factory)}"
    )


def _snapshot_state(factory, *, project_id: str = PROJECT) -> dict:
    """Full restart-comparison snapshot: tasks, invocations, runs, artifacts
    (path/sha256/size), evidence and outbox — order-independent."""
    with UnitOfWork(factory) as uow:
        from sqlalchemy import select

        from researchd.persistence.models import OutboxRow
        from researchd.persistence.repositories import ArtifactRepo as AR
        from researchd.persistence.repositories import EvidenceRepo as ER
        from researchd.persistence.repositories import InvocationRepo as IR
        from researchd.persistence.repositories import RunRepo as RR
        from researchd.persistence.repositories import TaskRepo as TR

        def _st(v):
            return getattr(v, "value", v)

        tasks = TR(uow.session).list_by_project(project_id)
        runs = RR(uow.session).list_by_project(project_id)
        invs = IR(uow.session).list_by_project(project_id)
        arts = AR(uow.session).list_by_project(project_id)
        evs = ER(uow.session).list_by_project(project_id)
        outbox = uow.session.execute(select(OutboxRow).order_by(OutboxRow.id)).scalars().all()
        return {
            "tasks": sorted((t.task_id, _st(t.status)) for t in tasks),
            "runs": sorted((r.run_id, r.task_id, (r.metadata or {}).get("role"), _st(r.status)) for r in runs),
            "invocations": sorted((i.invocation_id, i.role, _st(i.status)) for i in invs),
            "artifacts": sorted((a.path, a.sha256, a.size_bytes) for a in arts),
            "evidence": sorted((e.evidence_id, _st(e.status)) for e in evs),
            "outbox": sorted((o.idempotency_key, o.status) for o in outbox),
        }


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
    """REAL smoke: reasonix planner + worker + independent auditor with real
    models. Gated on RESEARCHD_RUN_REAL_SMOKE=1 (paid model calls need host
    authorization).

    The task id comes from the REAL planner output (never hard-coded): the
    brief demands exactly one task with the out/result.json deliverable, and
    the test waits for whatever id the model produced."""
    if not os.environ.get("RESEARCHD_RUN_REAL_SMOKE") == "1":
        pytest.skip("real reasonix smoke requires RESEARCHD_RUN_REAL_SMOKE=1 (paid model authorization)")
    from researchd.executors.reasonix.adapter import ReasonixAdapter

    ws = _setup(factory, tmp_path, description=REAL_PROJECT_BRIEF)
    settings = Settings()
    settings.data_dir = str(tmp_path / "data")
    adapter = ReasonixAdapter(settings, overlay_dir=str(tmp_path / "data"))
    port = FakeDeliveryPort()
    loop = SchedulerLoop(settings, factory, adapter, port, max_parallel=1)
    try:
        tasks = asyncio.run(_real_scenario(loop, factory))
        # the real planner must have planned EXACTLY ONE task matching the brief
        assert len(tasks) == 1, (
            f"expected exactly one planned task, got {[t.task_id for t in tasks]} "
            f"— state={_safe_state_summary(factory)}"
        )
        task = tasks[0]
        contract_text = " ".join(
            [(task.contract.objective or "")] + list(task.contract.deliverables or [])
        )
        assert "result.json" in contract_text, (
            f"planned task does not mention out/result.json: {contract_text[:200]!r} "
            f"— state={_safe_state_summary(factory)}"
        )
        with UnitOfWork(factory) as uow:
            task2 = TaskRepo(uow.session).get_by_task_id(task.task_id)
            assert task2.status.value == "COMPLETED"
            runs = RunRepo(uow.session).list_by_project(PROJECT)
            # independent auditor run exists (never RUNNING -> COMPLETED)
            roles = [(r.task_id, (r.metadata or {}).get("role")) for r in runs]
            assert (task.task_id, "auditor") in roles
            # worker run recorded resolved model + skills + usage (or explicit
            # unavailable) — full model-call traceability
            worker_runs = [r for r in runs if (r.metadata or {}).get("role") != "auditor"]
            assert worker_runs and worker_runs[0].resolved_model
            for r in runs:
                if r.outcome:
                    assert (r.usage or {}).get("available") is not None  # never fabricated
            # REAL artifacts: relative path, full sha256, file on disk, hash matches
            from researchd.persistence.repositories import ArtifactRepo as AR

            arts = AR(uow.session).list_by_project(PROJECT)
            assert arts, f"no artifact registered — state={_safe_state_summary(factory)}"
            for a in arts:
                assert not Path(a.path).is_absolute(), f"artifact path must be relative: {a.path}"
                assert a.sha256 and len(a.sha256) == 64, f"artifact must carry a full sha256: {a.path}"
                assert a.size_bytes and a.size_bytes > 0
                on_disk = ws / a.path
                assert on_disk.exists(), f"artifact file missing on disk: {a.path}"
                from hashlib import sha256

                assert a.sha256 == sha256(on_disk.read_bytes()).hexdigest()
            # any evidence that WAS accepted got verified via the audit gate
            from researchd.persistence.repositories import EvidenceRepo as ER

            evs = ER(uow.session).list_by_project(PROJECT)
            assert evs, f"no evidence registered — state={_safe_state_summary(factory)}"
            for ev in evs:
                assert ev.status.value in ("VERIFIED", "CANDIDATE")
            # context packages persisted for every turn
            from researchd.persistence.repositories import ContextPackageRepo as CR

            pkgs = CR(uow.session).list_for_run(worker_runs[0].run_id)
            assert pkgs
            # REVIEW happened before COMPLETED (audit event chain)
            from sqlalchemy import select

            from researchd.persistence.models import EventRow

            types = [e.event_type for e in uow.session.execute(select(EventRow)).scalars()]
            for want in ("task.review_submitted", "audit.accepted", "task.completed"):
                assert want in types, f"missing event {want}"
            assert types.index("task.review_submitted") < types.index("task.completed")
    finally:
        # always release reasonix/bwrap child processes, even on assertion failure
        asyncio.run(adapter.close())


def test_live_smoke_service_process_restart(factory, tmp_path):
    """REAL smoke with ACTUAL service processes (subprocess): the service
    drives planner/worker/auditor with real reasonix model calls; after
    COMPLETED the service process is killed and restarted, and the FULL
    snapshot (tasks/invocations/runs/artifacts+hash/evidence/outbox) must be
    IDENTICAL — zero re-invocation, zero duplicates.
    Gated on RESEARCHD_RUN_REAL_SMOKE=1 (paid model calls + real service)."""
    if not os.environ.get("RESEARCHD_RUN_REAL_SMOKE") == "1":
        pytest.skip("real reasonix smoke requires RESEARCHD_RUN_REAL_SMOKE=1 (paid model authorization)")

    _setup(factory, tmp_path, description=REAL_PROJECT_BRIEF)
    data_dir = str(tmp_path / "data")
    sock = Path(data_dir) / "run" / "researchd.sock"
    repo_root = str(Path(__file__).resolve().parent.parent.parent)

    # Explicit, caller-independent environment: the fixture DB, reasonix
    # executor, fake delivery and a unique UDS — never production config.
    env = dict(os.environ)
    env.update(
        {
            "RESEARCHD_RUN_REAL_SMOKE": "1",
            "RESEARCHD_DB": str(tmp_path / "test.db"),
            "RESEARCHD_DATA_DIR": data_dir,
            "RESEARCHD_SCHEDULER__EXECUTOR": "reasonix",
            "RESEARCHD_SCHEDULER__DELIVERY": "fake",
            "RESEARCHD_SCHEDULER__MAX_PARALLEL": "1",
            "RESEARCHD_DOC_PLATFORM": "none",
            "RESEARCHD_API__TRANSPORT": "uds",
        }
    )
    # Click group option BEFORE the subcommand: `researchd --data-dir X service`
    cmd = [sys.executable, "-m", "researchd.cli", "--data-dir", data_dir, "service"]

    def _start(phase: str):
        log_path = tmp_path / f"service-{phase}.log"
        log = open(log_path, "wb")
        proc = subprocess.Popen(cmd, cwd=repo_root, env=env, stdout=log, stderr=subprocess.STDOUT)
        return proc, log, log_path

    def _wait_ready(proc, log_path, timeout: float = 45.0):
        """Fail FAST when the service exits early; wait for the UDS socket."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(
                    f"service exited early (code={proc.returncode}); log={log_path}"
                )
            if sock.exists():
                return
            time.sleep(0.5)
        raise AssertionError(f"service not ready within {timeout:.0f}s; log={log_path}")

    def _stop(proc, log, phase: str):
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        log.close()

    # ---- phase 1: real service completes the single real task ----
    proc1, log1, log1_path = _start("first")
    try:
        _wait_ready(proc1, log1_path)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            with UnitOfWork(factory) as uow:
                tasks = TaskRepo(uow.session).list_by_project(PROJECT)
                if tasks and all(
                    getattr(t.status, "value", t.status) in ("COMPLETED", "FAILED")
                    for t in tasks
                ):
                    break
            time.sleep(3)
        with UnitOfWork(factory) as uow:
            tasks = TaskRepo(uow.session).list_by_project(PROJECT)
            assert tasks, f"no tasks after first run; log={log1_path}"
            for t in tasks:
                assert getattr(t.status, "value", t.status) == "COMPLETED", (
                    f"task {t.task_id} {getattr(t.status, 'value', t.status)}: "
                    f"{(getattr(t, 'error_message', None) or '')[:120]}; log={log1_path}"
                )
        before = _snapshot_state(factory)
        assert len(before["invocations"]) >= 3  # planner + worker + auditor
        assert len(before["runs"]) >= 2
    finally:
        _stop(proc1, log1, "first")

    # ---- phase 2: restart the ACTUAL service on the SAME fixture DB ----
    proc2, log2, log2_path = _start("second")
    try:
        _wait_ready(proc2, log2_path)
        time.sleep(25)  # several ticks with nothing left to do
        with UnitOfWork(factory) as uow:
            tasks = TaskRepo(uow.session).list_by_project(PROJECT)
            for t in tasks:
                assert getattr(t.status, "value", t.status) == "COMPLETED"
        after = _snapshot_state(factory)
        assert after == before, f"restart re-ran work: {before} -> {after}"
    finally:
        _stop(proc2, log2, "second")
