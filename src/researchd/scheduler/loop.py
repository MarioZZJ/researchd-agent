"""Scheduler loop: reconcile -> dispatch -> drive -> collect -> outbox
(IMPLEMENTATION.md §14). Runs inside `researchd service`."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import sessionmaker

from ..config import Settings
from ..domain.enums import TaskStatus
from ..executors.base import ExecutorAdapter
from ..persistence.repositories import RunRepo, TaskRepo
from .dispatch import RunDispatcher, reconcile_orphans, task_dispatch_decision
from .leases import LeaseRepo
from .outbox_sender import OutboxSender, DeliveryPort

logger = logging.getLogger("researchd.scheduler")

TICK_SECONDS = 2.0
HEARTBEAT_SECONDS = 10.0


@dataclass
class ActiveRun:
    run_id: str
    task_id: str
    task: object  # Task domain object (reloaded per heartbeat)
    task_handle: asyncio.Task | None = None


class SchedulerLoop:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker,
        executor: ExecutorAdapter,
        delivery_port: DeliveryPort,
        *,
        max_parallel: int = 4,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.executor = executor
        self.sender = OutboxSender(session_factory, delivery_port)
        self.max_parallel = max_parallel
        self.active: dict[str, ActiveRun] = {}
        self._stop = asyncio.Event()
        self._dispatch_sem = asyncio.Semaphore(max_parallel)
        self.last_tick_stats: dict = {}

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.last_tick_stats = await self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ tick
    async def tick(self) -> dict:
        stats: dict = {"orphans": 0, "dispatched": 0, "heartbeats": 0, "outbox": {}}
        # 1. recovery: any stale run (restart/crash) -> ORPHANED, task -> READY
        with self.session_factory() as session:
            stats["orphans"] = len(reconcile_orphans(session))
            session.commit()
        # 2. outbox delivery (deduplicated, lease-protected)
        stats["outbox"] = await self.sender.send_pending()
        # 3. dispatch ready tasks (bounded by max_parallel)
        stats["dispatched"] = await self._dispatch_ready()
        # 4. heartbeats for active runs
        stats["heartbeats"] = await self._heartbeat_active()
        return stats

    async def _dispatch_ready(self) -> int:
        dispatched = 0
        with self.session_factory() as session:
            tasks = TaskRepo(session).list_by_status(None, [TaskStatus.READY.value])
            open_decisions = []
            if tasks:
                from ..persistence.repositories import DecisionRepo

                # only truly unanswered decisions block tasks (ANSWERED ones
                # have been resolved by the PI and unblock their scope)
                open_decisions = [
                    d.decision_id
                    for d in DecisionRepo(session).list_open()
                    if d.status.value == "OPEN"
                ]
            slots = self.max_parallel - len(self.active)
            for task in tasks:
                if slots <= 0:
                    break
                if task.task_id in self.active:
                    continue
                decision = task_dispatch_decision(task, open_decisions)
                if decision.action != "dispatch":
                    continue
                dispatcher = RunDispatcher(session, self.executor)
                run = dispatcher.dispatch_task(task)
                if run is None:
                    continue
                session.commit()
                handle = asyncio.create_task(self._drive_run(run.run_id, run.task_id))
                self.active[run.task_id] = ActiveRun(run_id=run.run_id, task_id=run.task_id, task=task, task_handle=handle)
                dispatched += 1
                slots -= 1
        return dispatched

    async def _heartbeat_active(self) -> int:
        n = 0
        for task_id in list(self.active.keys()):
            entry = self.active[task_id]
            if entry.task_handle.done():
                self.active.pop(task_id, None)
                continue
            with self.session_factory() as session:
                run = RunRepo(session).get_by_run_id(entry.run_id)
                if run is None or run.status.value not in ("STARTING", "RUNNING"):
                    self.active.pop(task_id, None)
                    continue
                run.heartbeat()
                if run.lease_token:
                    LeaseRepo(session).heartbeat(run.lease_token)
                session.commit()
            n += 1
        return n

    # ------------------------------------------------------------ drive
    async def _drive_run(self, run_id: str, task_id: str) -> None:
        """Execute one run with budget enforcement; heartbeat + lease renewal."""
        async with self._dispatch_sem:
            budget = await self._run_budget(task_id)
            try:
                async with asyncio.timeout(budget):
                    await self._execute_with_heartbeat(run_id)
            except TimeoutError:
                await self._collect_interrupt(run_id, "budget exceeded")
            except asyncio.CancelledError:
                await self._collect_interrupt(run_id, "cancelled")
            except Exception as exc:  # noqa: BLE001
                await self._collect_failure(run_id, str(exc))

    async def _run_budget(self, task_id: str) -> float:
        with self.session_factory() as session:
            task = TaskRepo(session).get_by_task_id(task_id)
            if task is None:
                return 300.0
            return float(task.contract.budget.max_wall_seconds or 300.0)

    async def _execute_with_heartbeat(self, run_id: str) -> None:
        """Run the executor turn, refreshing heartbeat every HEARTBEAT_SECONDS."""
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(run_id))
        try:
            with self.session_factory() as session:
                run = RunRepo(session).get_by_run_id(run_id)
                task = TaskRepo(session).get_by_task_id(run.task_id)
                context = {"task": task.model_dump(), "project_id": task.project_id}
            result, session_info = await self.executor.run_worker(context, profile={})
            with self.session_factory() as session:
                run = RunRepo(session).get_by_run_id(run_id)
                RunDispatcher(session, self.executor).collect_success(run, result, session_info)
                session.commit()
        finally:
            heartbeat_task.cancel()

    async def _heartbeat_loop(self, run_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                with self.session_factory() as session:
                    run = RunRepo(session).get_by_run_id(run_id)
                    if run is None:
                        return
                    run.heartbeat()
                    if run.lease_token:
                        LeaseRepo(session).heartbeat(run.lease_token)
                    session.commit()
        except asyncio.CancelledError:
            pass

    async def _collect_failure(self, run_id: str, error: str) -> None:
        with self.session_factory() as session:
            run = RunRepo(session).get_by_run_id(run_id)
            if run is None or run.status.value not in ("STARTING", "RUNNING"):
                return
            RunDispatcher(session, self.executor).collect_failure(run, error)
            session.commit()

    async def _collect_interrupt(self, run_id: str, reason: str) -> None:
        with self.session_factory() as session:
            run = RunRepo(session).get_by_run_id(run_id)
            if run is None or run.status.value not in ("STARTING", "RUNNING"):
                return
            RunDispatcher(session, self.executor).collect_interrupt(run, reason)
            session.commit()
