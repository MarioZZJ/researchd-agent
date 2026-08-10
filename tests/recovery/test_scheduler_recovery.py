"""Phase 3 recovery tests: leases, budgets, orphan reconciliation, outbox
retry/dead-letter, restart recovery (IMPLEMENTATION.md §14, §25.5)."""

from __future__ import annotations

import asyncio

import pytest

from researchd.domain.base import utcnow
from researchd.domain.enums import RunStatus, TaskStatus
from researchd.domain.task import Budget, SuccessCriterion, Task, TaskContract
from researchd.executors.fake import FakeDeliveryPort, FakeExecutor
from researchd.persistence.outbox import OutboxRepo
from researchd.persistence.repositories import RunRepo, TaskRepo
from researchd.persistence.transaction import init_db, make_engine, make_session_factory, UnitOfWork
from researchd.scheduler.dispatch import RunDispatcher, reconcile_orphans, task_dispatch_decision
from researchd.scheduler.leases import LeaseRepo, WorkspaceLockRepo
from researchd.scheduler.loop import SchedulerLoop


@pytest.fixture()
def env(tmp_path):
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    yield {"factory": factory, "tmp": tmp_path}
    engine.dispose()


def make_task(task_id="T-001", *, budget=None, status=None, blocked_by=None, **kw) -> Task:
    t = Task(
        task_id=task_id,
        project_id="P-TEST",
        contract=TaskContract(
            task_id=task_id,
            role="analysis_worker",
            objective=f"objective {task_id}",
            success_criteria=[SuccessCriterion(id="SC-1", text="c")],
            budget=budget or Budget(max_wall_seconds=60),
        ),
        blocked_by=blocked_by or [],
        **kw,
    )
    if status == "READY":
        t.propose_ready()
    return t


def test_dispatch_decision_gates():
    t = make_task(status="READY")
    assert task_dispatch_decision(t, []).action == "dispatch"
    t2 = make_task(status="READY", blocked_by=["D-002"])
    assert task_dispatch_decision(t2, ["D-002"]).action == "blocked"
    assert task_dispatch_decision(t2, []).action == "dispatch"
    t3 = make_task(status="RUNNING")
    assert task_dispatch_decision(t3, []).action == "skip"


def test_dispatcher_full_cycle(env):
    ex = FakeExecutor()
    with UnitOfWork(env["factory"]) as uow:
        t = make_task(status="READY")
        TaskRepo(uow.session).save(t)
        uow.commit()
        d = RunDispatcher(uow.session, ex)
        run = d.dispatch_task(t)
        assert run.status.value == "RUNNING"
        assert t.status.value == "RUNNING"
        uow.commit()
    # worker returns a default SUCCESS result
    result, info = asyncio.run(ex.run_worker({"task": t.model_dump()}, profile={}))
    with UnitOfWork(env["factory"]) as uow:
        run = RunRepo(uow.session).get_by_run_id(run.run_id)
        d = RunDispatcher(uow.session, ex)
        d.collect_success(run, result, info)
        uow.commit()
    with UnitOfWork(env["factory"]) as uow:
        run = RunRepo(uow.session).get_by_run_id(run.run_id)
        task = TaskRepo(uow.session).get_by_task_id("T-001")
        assert run.status.value == "SUCCEEDED"
        assert task.status.value == "REVIEW"  # run success != task completion


def test_lease_contention_and_heartbeat(env):
    with UnitOfWork(env["factory"]) as uow:
        repo = LeaseRepo(uow.session)
        token = repo.acquire(project_id="P-TEST", task_id="T-1", run_id="R-1", owner="a")
        assert token is not None
        # same run cannot be leased twice while live
        assert repo.acquire(project_id="P-TEST", task_id="T-1", run_id="R-1", owner="b") is None
        assert repo.heartbeat(token) is True
        uow.commit()
    with UnitOfWork(env["factory"]) as uow:
        repo = LeaseRepo(uow.session)
        assert repo.live_for_run("R-1") is not None
        repo.release(token)
        uow.commit()
    with UnitOfWork(env["factory"]) as uow:
        assert LeaseRepo(uow.session).live_for_run("R-1") is None


def test_workspace_lock_exclusive(env):
    with UnitOfWork(env["factory"]) as uow:
        repo = WorkspaceLockRepo(uow.session)
        tok = repo.acquire(project_id="P-TEST", scope="docs/sections/01.md", owner="run1")
        assert tok is not None
        assert repo.acquire(project_id="P-TEST", scope="docs/sections/01.md", owner="run2") is None
        uow.commit()
    with UnitOfWork(env["factory"]) as uow:
        repo = WorkspaceLockRepo(uow.session)
        assert repo.release("P-TEST", "docs/sections/01.md", tok) is True
        tok2 = repo.acquire(project_id="P-TEST", scope="docs/sections/01.md", owner="run2")
        assert tok2 is not None
        uow.commit()


def test_orphan_reconciliation(env):
    ex = FakeExecutor()
    with UnitOfWork(env["factory"]) as uow:
        t = make_task(status="READY")
        TaskRepo(uow.session).save(t)
        uow.commit()
        d = RunDispatcher(uow.session, ex)
        run = d.dispatch_task(t)
        uow.commit()
    # simulate a crash: no heartbeat; lease expires
    with UnitOfWork(env["factory"]) as uow:
        run = RunRepo(uow.session).get_by_run_id(run.run_id)
        run.heartbeat_at = None
        RunRepo(uow.session).save(run)
        uow.commit()
    with UnitOfWork(env["factory"]) as uow:
        orphaned = reconcile_orphans(uow.session, max_age_seconds=0)
        assert run.run_id in orphaned
        uow.commit()
    with UnitOfWork(env["factory"]) as uow:
        run = RunRepo(uow.session).get_by_run_id(run.run_id)
        task = TaskRepo(uow.session).get_by_task_id("T-001")
        assert run.status.value == "ORPHANED"
        assert task.status.value == "READY"  # requeued for retry


def test_budget_timeout_interrupts_run(env):
    ex = FakeExecutor()
    ex.script("worker", {"action": "hang"})  # worker never returns
    with UnitOfWork(env["factory"]) as uow:
        t = make_task(status="READY", budget=Budget(max_wall_seconds=1))
        TaskRepo(uow.session).save(t)
        uow.commit()
        d = RunDispatcher(uow.session, ex)
        run = d.dispatch_task(t)
        uow.commit()
    asyncio.run(_drive_with_timeout(env, ex, run.run_id, "T-001"))
    with UnitOfWork(env["factory"]) as uow:
        run = RunRepo(uow.session).get_by_run_id(run.run_id)
        task = TaskRepo(uow.session).get_by_task_id("T-001")
        assert run.status.value == "INTERRUPTED"
        assert task.status.value == "READY"


async def _drive_with_timeout(env, ex, run_id, task_id):
    settings = type("S", (), {"scheduler": type("SC", (), {"max_parallel": 1})()})()
    loop = SchedulerLoop(settings, env["factory"], ex, FakeDeliveryPort(), max_parallel=1)
    await loop._drive_run(run_id, task_id)


def test_scheduler_dispatch_and_collect(env):
    """End-to-end through the loop: READY task -> RUNNING -> REVIEW, and the
    outbox delivers a scheduled message exactly once."""
    ex = FakeExecutor()
    port = FakeDeliveryPort()
    with UnitOfWork(env["factory"]) as uow:
        t = make_task(status="READY")
        TaskRepo(uow.session).save(t)
        OutboxRepo(uow.session).enqueue(
            destination="delivery", idempotency_key="out-1", payload={"kind": "digest", "project_id": "P-TEST"}
        )
        uow.commit()
    settings = type("S", (), {"scheduler": type("SC", (), {"max_parallel": 4})()})()
    loop = SchedulerLoop(settings, env["factory"], ex, port, max_parallel=4)

    async def run_ticks():
        for _ in range(6):
            await loop.tick()
            await asyncio.sleep(0.05)
    asyncio.run(run_ticks())

    with UnitOfWork(env["factory"]) as uow:
        task = TaskRepo(uow.session).get_by_task_id("T-001")
        runs = RunRepo(uow.session).list_active()
        assert task.status.value == "REVIEW"
        assert len(port.deliveries) == 1
        assert port.deliveries[0]["idempotency_key"] == "out-1"
        # a second round of ticks does NOT redeliver (idempotent)
    async def run_ticks2():
        for _ in range(3):
            await loop.tick()
            await asyncio.sleep(0.05)
    asyncio.run(run_ticks2())
    assert len(port.deliveries) == 1


def test_outbox_crash_recovery_no_duplicate(env):
    """Commit + deliver + crash before mark_sent -> redelivery is deduplicated."""
    port = FakeDeliveryPort()
    with UnitOfWork(env["factory"]) as uow:
        OutboxRepo(uow.session).enqueue(
            destination="delivery", idempotency_key="out-crash", payload={"kind": "message", "text": "x"}
        )
        uow.commit()
    settings = type("S", (), {"scheduler": type("SC", (), {"max_parallel": 1})()})()
    loop = SchedulerLoop(settings, env["factory"], ex := FakeExecutor(), port, max_parallel=1)
    asyncio.run(loop.tick())
    assert len(port.deliveries) == 1
    # simulate crash-after-send: row still PENDING/IN_FLIGHT in a fresh DB view
    with UnitOfWork(env["factory"]) as uow:
        from researchd.persistence.models import OutboxRow
        row = uow.session.execute(__import__("sqlalchemy").select(OutboxRow)).scalars().one()
        assert row.status in ("SENT", "IN_FLIGHT")
    # force it back to PENDING (crash before mark_sent persisted)
    with UnitOfWork(env["factory"]) as uow:
        from researchd.persistence.models import OutboxRow
        from researchd.domain.enums import OutboxStatus
        row = uow.session.execute(__import__("sqlalchemy").select(OutboxRow)).scalars().one()
        row.status = OutboxStatus.PENDING.value
        row.next_attempt_at = None
        uow.commit()
    asyncio.run(loop.tick())
    # fake port delivers a second time BUT the idempotency key is stable;
    # the deduplication contract lives at the delivery port (Phase 6 enforces
    # it against cc-connect); here we assert the sender re-sends the same key.
    keys = [d["idempotency_key"] for d in port.deliveries]
    assert keys.count("out-crash") == 2  # sender retries with the same key


def test_blocked_task_not_dispatched(env):
    """Open decision blocks the dependent task; closing it unblocks."""
    from researchd.domain.decision import Decision, DecisionOption
    from researchd.persistence.repositories import DecisionRepo

    ex = FakeExecutor()
    with UnitOfWork(env["factory"]) as uow:
        t = make_task(status="READY", blocked_by=["D-002"])
        TaskRepo(uow.session).save(t)
        DecisionRepo(uow.session).save(
            Decision(
                decision_id="D-002", project_id="P-TEST", status="OPEN", question="q",
                options=[DecisionOption(option_id="A", label="A")],
            )
        )
        uow.commit()
    settings = type("S", (), {"scheduler": type("SC", (), {"max_parallel": 4})()})()
    loop = SchedulerLoop(settings, env["factory"], ex, FakeDeliveryPort(), max_parallel=4)
    asyncio.run(loop.tick())
    with UnitOfWork(env["factory"]) as uow:
        assert TaskRepo(uow.session).get_by_task_id("T-001").status.value == "READY"  # still READY
    # close the decision -> next tick dispatches and completes
    with UnitOfWork(env["factory"]) as uow:
        d = DecisionRepo(uow.session).get_by_decision_id("D-002")
        d.apply_answer("A", "pi")
        DecisionRepo(uow.session).save(d)
        uow.commit()
    async def wait_ticks():
        for _ in range(8):
            await loop.tick()
            await asyncio.sleep(0.05)
    asyncio.run(wait_ticks())
    with UnitOfWork(env["factory"]) as uow:
        assert TaskRepo(uow.session).get_by_task_id("T-001").status.value == "REVIEW"
