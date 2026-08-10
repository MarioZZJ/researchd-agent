"""Transaction + idempotency + outbox tests (IMPLEMENTATION.md §10, §25.2)."""

import pytest
from sqlalchemy import select

from researchd.domain.base import Actor, AggregateRef, new_id
from researchd.domain.events import make_event
from researchd.persistence.models import EventRow, OutboxRow
from researchd.persistence.outbox import OutboxRepo
from researchd.persistence.repositories import EventRepo
from researchd.persistence.transaction import (
    OptimisticConcurrencyError,
    UnitOfWork,
    init_db,
    make_engine,
    make_session_factory,
)
from researchd.persistence.repositories import TaskRepo
from researchd.domain.task import Task, TaskContract, SuccessCriterion


@pytest.fixture()
def uow_factory(tmp_path):
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()


def make_task(project_id="P-TEST") -> Task:
    return Task(
        task_id="T-001",
        project_id=project_id,
        contract=TaskContract(
            task_id="T-001",
            role="analysis_worker",
            objective="o",
            success_criteria=[SuccessCriterion(id="SC-1", text="c")],
        ),
    )


def test_event_outbox_same_transaction(uow_factory):
    """Aggregate update + event append + outbox insert commit atomically."""
    with UnitOfWork(uow_factory) as uow:
        repo = TaskRepo(uow.session)
        task = make_task()
        repo.save(task)
        ev = make_event(
            event_type="task.proposed",
            aggregate=AggregateRef(type="task", id=task.id, version=1),
            idempotency_key="task:T-001:propose:v1",
            project_id=task.project_id,
        )
        uow.session.add(EventRow(
            id="EVTROW-1", event_id=ev.event_id, event_type=ev.event_type, occurred_at=ev.occurred_at,
            project_id=ev.project_id, aggregate_type="task", aggregate_id=task.id, aggregate_version=1,
            actor_json=ev.actor.model_dump(), idempotency_key=ev.idempotency_key, payload_json=ev.payload,
        ))
        OutboxRepo(uow.session).enqueue(
            destination="delivery", idempotency_key="out:T-001:notify:v1", payload={"x": 1}
        )
        uow.commit()
    with UnitOfWork(uow_factory) as uow:
        events = uow.session.execute(select(EventRow)).scalars().all()
        outbox = uow.session.execute(select(OutboxRow)).scalars().all()
        assert len(events) == 1
        assert len(outbox) == 1
        assert outbox[0].idempotency_key == "out:T-001:notify:v1"


def test_rollback_discards_all(uow_factory):
    with pytest.raises(RuntimeError):
        with UnitOfWork(uow_factory) as uow:
            TaskRepo(uow.session).save(make_task())
            uow.session.add(EventRow(
                id="EVTROW-1", event_id="EVT-1", event_type="task.proposed", occurred_at=uow.session.bind.connect().exec_driver_sql("SELECT datetime('now')").scalar(),
                project_id="P-TEST", aggregate_type="task", aggregate_id="X", aggregate_version=1,
                actor_json={}, idempotency_key="k1", payload_json={},
            ))
            raise RuntimeError("boom")
    with UnitOfWork(uow_factory) as uow:
        assert uow.session.execute(select(EventRow)).scalars().all() == []
        assert uow.session.execute(select(OutboxRow)).scalars().all() == []


def test_event_idempotency_key_unique(uow_factory):
    with UnitOfWork(uow_factory) as uow:
        ev = make_event(
            event_type="task.proposed",
            aggregate=AggregateRef(type="task", id="T-001", version=1),
            idempotency_key="dup-key",
        )
        EventRepo(uow.session).append(ev)
        uow.commit()
    from sqlalchemy.exc import IntegrityError

    with UnitOfWork(uow_factory) as uow:
        ev2 = make_event(
            event_type="task.proposed",
            aggregate=AggregateRef(type="task", id="T-001", version=1),
            idempotency_key="dup-key",
        )
        EventRepo(uow.session).append(ev2)
        with pytest.raises(IntegrityError):
            uow.commit()


def test_duplicate_detection(uow_factory):
    with UnitOfWork(uow_factory) as uow:
        ev = make_event(
            event_type="inbound.message_received",
            aggregate=AggregateRef(type="inbound_message", id="MSG-1", version=1),
            idempotency_key="feishu:MSG-1",
        )
        EventRepo(uow.session).append(ev)
        uow.commit()
    with UnitOfWork(uow_factory) as uow:
        assert EventRepo(uow.session).exists("feishu:MSG-1") is True
        assert EventRepo(uow.session).exists("feishu:MSG-2") is False


def test_optimistic_concurrency_conflict(uow_factory):
    with UnitOfWork(uow_factory) as uow:
        TaskRepo(uow.session).save(make_task())
        uow.commit()
    with UnitOfWork(uow_factory) as uow:
        repo = TaskRepo(uow.session)
        task = repo.get("T-001")
        task.status = "RUNNING"
        repo.save(task)
        uow.commit()
    # a second writer still holds version 1 -> conflict, no silent overwrite
    with UnitOfWork(uow_factory) as uow:
        repo = TaskRepo(uow.session)
        stale = make_task()  # version 1
        with pytest.raises(OptimisticConcurrencyError):
            repo.save(stale)
        uow.rollback()
    with UnitOfWork(uow_factory) as uow:
        fresh = TaskRepo(uow.session).get("T-001")
        assert fresh.status == "RUNNING"


def test_outbox_claim_and_backoff(uow_factory):
    with UnitOfWork(uow_factory) as uow:
        OutboxRepo(uow.session).enqueue(destination="delivery", idempotency_key="k1", payload={})
        uow.commit()
    with UnitOfWork(uow_factory) as uow:
        repo = OutboxRepo(uow.session)
        rows = repo.pending()
        assert len(rows) == 1
        row = rows[0]
        assert repo.claim(row.id, attempts=0) is True
        assert repo.claim(row.id, attempts=0) is False  # double claim rejected
        assert repo.backoff(row.id, attempts=1) is True
        uow.commit()
    with UnitOfWork(uow_factory) as uow:
        assert OutboxRepo(uow.session).pending() == []  # backed off, not due yet


def test_outbox_dead_letter(uow_factory):
    with UnitOfWork(uow_factory) as uow:
        OutboxRepo(uow.session).enqueue(destination="delivery", idempotency_key="k2", payload={}, max_attempts=1)
        uow.commit()
    with UnitOfWork(uow_factory) as uow:
        repo = OutboxRepo(uow.session)
        row = repo.pending()[0]
        repo.claim(row.id, attempts=0)
        repo.record_attempt(row.id, success=False, error="boom")
        repo.mark_dead(row.id, attempts=1, error="boom")
        uow.commit()
    with UnitOfWork(uow_factory) as uow:
        from researchd.domain.enums import OutboxStatus
        row = uow.session.execute(select(OutboxRow)).scalars().one()
        assert row.status == OutboxStatus.DEAD.value
        attempts = uow.session.execute(select(__import__("researchd.persistence.models", fromlist=["OutboxAttemptRow"]).OutboxAttemptRow)).scalars().all()
        assert len(attempts) == 1
