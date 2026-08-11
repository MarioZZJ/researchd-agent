"""Model-call invocation ledger (IMPLEMENTATION.md §13): planner turns are
recorded even though they have no task/run row; usage is recorded as reported
or explicitly "unavailable" — never fabricated."""

from __future__ import annotations

import asyncio

import pytest

from researchd.domain.base import utcnow
from researchd.domain.project import Project
from researchd.executors.fake import FakeDeliveryPort, FakeExecutor
from researchd.persistence.repositories import InvocationRepo, ProjectRepo
from researchd.persistence.transaction import init_db, make_engine, make_session_factory, UnitOfWork
from researchd.scheduler.extensions import plan_projects

pytestmark = pytest.mark.integration


@pytest.fixture()
def env(tmp_path):
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    ws = tmp_path / "ws"
    ws.mkdir()
    with UnitOfWork(factory) as uow:
        ProjectRepo(uow.session).save(
            Project(project_id="P-INV", name="inv", description="d", workspace_root=str(ws))
        )
        uow.commit()
    yield {"factory": factory}
    engine.dispose()


def test_planner_invocation_recorded_with_unavailable_usage(env):
    ex = FakeExecutor()
    ex.script("planner", {"payload": {
        "schema": "researchd.planner_result.v1",
        "proposed_tasks": [],
    }})
    n = asyncio.run(plan_projects(
        env["factory"], ex, data_dir="t",
        planner_profile={"name": "fake_planner", "model": "fake-model", "reasoning_effort": "low"},
    ))
    assert n == 0  # no tasks proposed, but the call still happened
    with UnitOfWork(env["factory"]) as uow:
        invs = InvocationRepo(uow.session).list_by_project("P-INV")
    assert len(invs) == 1
    inv = invs[0]
    assert inv.role == "planner"
    assert inv.status == "SUCCEEDED"
    assert inv.profile_name == "fake_planner"
    assert inv.resolved_model == "fake-model"
    assert inv.context_id  # bounded context persisted + linked
    assert inv.usage == {"available": False, "reason": "executor does not report usage"}
    assert inv.finished_at is not None


def test_planner_failure_recorded_as_failed(env):
    class Boom:
        async def run_planner(self, context, *, profile):
            raise RuntimeError("boom")

        installed_skills = []

    n = asyncio.run(plan_projects(env["factory"], Boom(), data_dir="t"))
    assert n == 0
    with UnitOfWork(env["factory"]) as uow:
        invs = InvocationRepo(uow.session).list_by_project("P-INV")
    assert len(invs) == 1
    assert invs[0].status == "FAILED"
    assert invs[0].error_message
