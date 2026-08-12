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

try:  # noqa: PLC2701  circular-safe import
    from ..projections.feishu_doc import DocPlatform
except ImportError:  # pragma: no cover
    DocPlatform = object  # type: ignore[assignment, misc]

logger = logging.getLogger("researchd.scheduler")

TICK_SECONDS = 2.0
HEARTBEAT_SECONDS = 10.0

# TaskRole -> default profile suffix (IMPLEMENTATION.md §15.1). Every role
# must map to an existing DEFAULT_PROFILES entry.
ROLE_TO_PROFILE = {
    "planner": "planner",
    "interaction": "worker",
    "worker": "worker",
    "worker_default": "worker",
    "literature_worker": "literature",
    "analysis_worker": "worker",
    "auditor": "auditor",
    "cross_model_reviewer": "auditor",
    "report_compressor": "worker",
    "manuscript_writer": "worker",
}


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
        doc_platform = delivery_port if isinstance(delivery_port, DocPlatform) else None
        self.sender = OutboxSender(session_factory, delivery_port, doc_platform=doc_platform)
        self.max_parallel = max_parallel
        self.active: dict[str, ActiveRun] = {}
        self._stop = asyncio.Event()
        self._dispatch_sem = asyncio.Semaphore(max_parallel)
        self._doc_platform_instance = None
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
        stats: dict = {"orphans": 0, "dispatched": 0, "heartbeats": 0, "outbox": {}, "decisions": 0, "reports": 0, "projection": 0, "diagnostics": 0, "planned": 0, "milestones": 0}
        # 1. recovery: any stale run (restart/crash) -> ORPHANED, task -> READY
        with self.session_factory() as session:
            stats["orphans"] = len(
                reconcile_orphans(session, data_dir=self.settings.data_dir)
            )
            session.commit()
        # 2. decision gate: evaluate pending candidates -> OPEN decisions
        #    (cheap-parallel conflict candidates are deferred to diagnostics)
        stats["decisions"] = await self._evaluate_decision_candidates()
        # 2b. cheap diagnostics: queue diagnostic tasks for cheap conflicts
        from ..scheduler.extensions import check_milestones, ensure_cheap_diagnostics, plan_projects

        stats["diagnostics"] = ensure_cheap_diagnostics(self.session_factory)
        # 2c. planning: first task batch for task-less ACTIVE projects
        stats["planned"] = await plan_projects(
            self.session_factory,
            self.executor,
            planner_profile=self._planner_profile(),
            data_dir=self.settings.data_dir,
        )
        # 2d. milestones: verified-evidence threshold reached -> one report
        stats["milestones"] = check_milestones(self.session_factory)
        # 2e. auditor: REVIEW tasks get an independent auditor run; ACCEPT
        #     completes the task, REVISE sends it back to READY
        stats["audited"] = await self._audit_review_tasks()
        # 3. reporting: emit queued reports for active projects
        stats["reports"] = await self._report_projects()
        # 4. project document projection (incremental, PI Notes protected)
        stats["projection"] = await self._project_projection()
        # 5. outbox delivery (deduplicated, lease-protected)
        stats["outbox"] = await self.sender.send_pending()
        # 6. dispatch ready tasks (bounded by max_parallel)
        stats["dispatched"] = await self._dispatch_ready()
        # yield once so freshly created run tasks can make progress even when
        # ticks are driven back-to-back (production also sleeps between ticks)
        await asyncio.sleep(0)
        # 7. heartbeats for active runs
        stats["heartbeats"] = await self._heartbeat_active()
        return stats

    async def _evaluate_decision_candidates(self) -> int:
        """Evaluate decision_candidates from recent SUCCEEDED runs through the
        Decision Gate; materialize OPEN decisions and block their scope."""
        from ..application.decision_gate import DecisionGate, build_decision
        from ..domain.decision import DecisionOption
        from ..domain.enums import TaskStatus as TS
        from ..persistence.repositories import DecisionRepo, RunRepo, TaskRepo

        opened = 0
        with self.session_factory() as session:
            repo = DecisionRepo(session)
            existing = {d.fingerprint for d in repo.list_all_statuses(None) if d.fingerprint}
            gate = DecisionGate(existing_fingerprints=existing)
            from sqlalchemy import select

            from ..persistence.models import RunRow

            # scan SUCCEEDED runs that were never evaluated (marked at the end)
            from sqlalchemy import func

            rows = session.execute(
                select(RunRow)
                .where(RunRow.status == "SUCCEEDED")
                .where(
                    func.json_extract(RunRow.metadata_json, "$.decisions_evaluated").is_(None)
                    | (func.json_extract(RunRow.metadata_json, "$.decisions_evaluated") != 1)
                )
                .order_by(RunRow.created_at.desc())
                .limit(200)
            ).scalars().all()
            blocked_task_ids: set[str] = set()
            evaluated: list[str] = []
            for row in rows:
                if (row.metadata_json or {}).get("decisions_evaluated"):
                    continue
                result = row.result_json or {}
                deferred = False  # cheap-parallel candidate waiting for a diagnostic
                for cand in result.get("decision_candidates", []):
                    options = [
                        DecisionOption(
                            option_id=o["option_id"], label=o.get("label", o["option_id"]),
                            description=o.get("description", ""),
                            scientific_consequence=o.get("scientific_consequence", ""),
                        )
                        for o in cand.get("options", [])
                    ]
                    has_conflict = cand.get("has_option_conflict", True)
                    cheap = cand.get("cheap_parallel", False)
                    numerical = cand.get("numerical_only", False)
                    # cheap parallel candidates do not fork: the first time we
                    # see one, a diagnostic task is queued (ensure_cheap_diagnostics)
                    # and this candidate is NOT evaluated yet; the diagnostic
                    # run's own result is evaluated normally below
                    from ..scheduler.extensions import CHEAP_DIAGNOSTIC_MARKER

                    task = TaskRepo(session).get_by_task_id(row.task_id)
                    from_diagnostic = bool(
                        task is not None
                        and (task.contract.why_now or "").startswith(CHEAP_DIAGNOSTIC_MARKER)
                    )
                    if cheap and has_conflict and not from_diagnostic:
                        deferred = True
                        continue  # diagnostic queued separately; evaluate later
                    verdict = gate.evaluate(
                        project_id=row.project_id,
                        category=cand.get("category", "other"),
                        question=cand.get("question", ""),
                        why_material=cand.get("why_material", ""),
                        options=options,
                        affected_object=cand.get("affected_object"),
                        trigger=cand.get("trigger", ""),
                        recommendation=cand.get("recommendation"),
                        recommendation_basis=cand.get("recommendation_basis"),
                        evidence_refs=cand.get("evidence_refs"),
                        unresolved_uncertainty=cand.get("unresolved_uncertainty"),
                        reversibility=cand.get("reversibility"),
                        blocking_scope=cand.get("blocking_scope"),
                        continue_scope=cand.get("continue_scope"),
                        has_option_conflict=has_conflict,
                        numerical_only=bool(cand.get("numerical_only", False)),
                        hard_gate_override=bool(cand.get("hard_gate_override", False)),
                    )
                    if verdict.action != "ask_pi":
                        continue
                    decision = build_decision(
                        verdict,
                        project_id=row.project_id,
                        question=cand.get("question", ""),
                        options=options,
                        category=cand.get("category", "other"),
                        trigger=cand.get("trigger", ""),
                        why_material=cand.get("why_material", ""),
                        recommendation=cand.get("recommendation"),
                        recommendation_basis=cand.get("recommendation_basis"),
                        evidence_refs=cand.get("evidence_refs"),
                        unresolved_uncertainty=cand.get("unresolved_uncertainty"),
                        reversibility=cand.get("reversibility"),
                    )
                    repo.save(decision)
                    opened += 1
                    blocked_task_ids.update(verdict.blocking_scope)
                if not deferred:
                    # fully evaluated (no cheap-parallel candidate pending a
                    # diagnostic): mark so stale candidates are never re-scanned
                    evaluated.append(row.id)
            # mark evaluated runs (so stale candidates are never re-scanned)
            # JSON-merge, never overwrite: metadata carries role/skills/context_id
            if evaluated:
                from sqlalchemy import update as sa_update

                session.execute(
                    sa_update(RunRow)
                    .where(RunRow.id.in_(evaluated))
                    .values(
                        metadata_json=func.json_set(
                            func.ifnull(RunRow.metadata_json, "{}"),
                            "$.decisions_evaluated",
                            1,
                        )
                    )
                    .execution_options(synchronize_session=False)
                )
            # block the scope of OPEN decisions (only blocking_scope)
            for d in repo.list_open(None):
                if d.status.value == "OPEN":
                    blocked_task_ids.update(d.blocking_scope)
            # unblock tasks whose decisions were resolved
            self._unblock_resolved(session, repo)
            if blocked_task_ids:
                from ..persistence.repositories import TaskRepo

                # map task_id -> decision that blocks it (for unblocking later)
                blockers: dict[str, str] = {}
                for d in repo.list_all_statuses(None):
                    if d.status.value == "OPEN":
                        for t in d.blocking_scope:
                            blockers[t] = d.decision_id
                for task in TaskRepo(session).list_by_status(None, [TS.READY.value, TS.RUNNING.value]):
                    if task.task_id in blocked_task_ids and task.status is TS.READY:
                        if blockers.get(task.task_id) not in task.blocked_by:
                            task.block(decision_id=blockers.get(task.task_id))
                        TaskRepo(session).save(task)
            session.commit()
        return opened

    def _unblock_resolved(self, session, repo) -> None:  # noqa: ANN001
        """When a blocking decision is ANSWERED/APPLIED, remove it from the
        blocked_by of paused tasks and requeue them (IMPLEMENTATION.md §8:
        only blocking_scope pauses, and only while OPEN)."""
        from ..domain.enums import TaskStatus as TS
        from ..persistence.repositories import TaskRepo

        resolved = [
            d.decision_id
            for d in repo.list_all_statuses(None)
            if d.status.value in ("ANSWERED", "APPLIED", "CLOSED", "WITHDRAWN")
        ]
        if not resolved:
            return
        for task in TaskRepo(session).list_by_status(None, [TS.BLOCKED.value]):
            remaining = [d for d in task.blocked_by if d not in resolved]
            if len(remaining) != len(task.blocked_by):
                task.blocked_by = remaining
                if not task.blocked_by:
                    task.requeue(reason="blocking decision resolved")
                TaskRepo(session).save(task)

    async def _report_projects(self) -> int:
        """Emit reports for projects with reportable state (no diff -> no send)."""
        from ..persistence.repositories import ProjectRepo
        from ..reporting.reporter import schedule_report

        emitted = 0
        with self.session_factory() as session:
            projects = ProjectRepo(session).list_all()
        for project in projects:
            if project.status.value != "ACTIVE":
                continue
            result = await schedule_report(self.session_factory, project_id=project.project_id)
            if result.sent:
                emitted += 1
        return emitted

    async def _project_projection(self) -> int:
        """Enqueue doc_block outbox rows for changed sections (incremental,
        PI Notes protected). Requires a configured doc platform: without one
        NOTHING is written and no projection state is claimed (the default
        FakeDocPlatform is never used in production)."""
        from ..persistence.repositories import ProjectRepo
        from ..projections.feishu_doc import sync_document

        platform = self._doc_platform()
        if platform is None:
            return 0  # no doc platform configured; projection disabled
        self.sender.doc_platform = platform  # share the instance with the sender
        updated = 0
        with self.session_factory() as session:
            projects = ProjectRepo(session).list_all()
        for project in projects:
            if project.status.value != "ACTIVE":
                continue
            document_id = (project.metadata or {}).get("feishu_document_id")
            if not document_id:
                continue  # no document configured
            try:
                with self.session_factory() as session:
                    result = await sync_document(
                        session, platform, project_id=project.project_id, document_id=document_id
                    )
                    updated += len(result.updated)
            except Exception:  # noqa: BLE001  one bad project must not starve the tick
                logger.exception("projection failed for project %s", project.project_id)
        return updated

    def _doc_platform(self):
        """Document platform: None unless a real platform is configured
        (feishu + credentials). FakeDocPlatform is ONLY injected by tests —
        production never projects into a fake and never claims state."""
        if self._doc_platform_instance is None:
            if getattr(self.settings, "doc_platform", "none") == "feishu":
                from ..projections.feishu_client import FeishuDocClient

                self._doc_platform_instance = FeishuDocClient()
            else:
                return None
        return self._doc_platform_instance

    async def _dispatch_ready(self) -> int:
        dispatched = 0
        with self.session_factory() as session:
            tasks = TaskRepo(session).list_by_status(None, [TaskStatus.READY.value])
            # cheap-diagnostic tasks are gate dependencies: dispatch them
            # FIRST so the gate is unblocked as soon as possible
            from ..scheduler.extensions import CHEAP_DIAGNOSTIC_MARKER

            tasks.sort(
                key=lambda t: (
                    0 if (t.contract.why_now or "").startswith(CHEAP_DIAGNOSTIC_MARKER) else 1,
                    t.created_at,
                )
            )
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
                decision = task_dispatch_decision(
                    task,
                    open_decisions,
                    blocked_task_ids=self._open_blocking_scope(session),
                )
                if decision.action != "dispatch":
                    continue
                dispatcher = RunDispatcher(session, self.executor)
                profile = self._resolve_profile(session, task)
                run = dispatcher.dispatch_task(task, profile=profile)
                if run is None:
                    continue
                session.commit()
                handle = asyncio.create_task(self._drive_run(run.run_id, run.task_id))
                self.active[run.task_id] = ActiveRun(run_id=run.run_id, task_id=run.task_id, task=task, task_handle=handle)
                dispatched += 1
                slots -= 1
        return dispatched

    async def _audit_review_tasks(self) -> int:
        """Dispatch an independent auditor run for every REVIEW task without a
        live audit run (the task-level lease deduplicates crashes/restarts)."""
        dispatched = 0
        with self.session_factory() as session:
            tasks = TaskRepo(session).list_by_status(None, [TaskStatus.REVIEW.value])
            for task in tasks:
                if task.task_id in self.active:
                    continue
                slots = self.max_parallel - len(self.active)
                if slots <= 0:
                    break
                dispatcher = RunDispatcher(session, self.executor)
                profile = self._resolve_profile(session, task, role="auditor")
                run = dispatcher.dispatch_audit_run(task, profile=profile)
                if run is None:
                    continue
                session.commit()
                handle = asyncio.create_task(self._drive_run(run.run_id, run.task_id, role="auditor"))
                self.active[task.task_id] = ActiveRun(run_id=run.run_id, task_id=run.task_id, task=task, task_handle=handle)
                dispatched += 1
        return dispatched

    def _open_blocking_scope(self, session) -> set:  # noqa: ANN001
        """Union of blocking_scope across OPEN decisions (IMPLEMENTATION.md §8:
        only blocking_scope pauses tasks)."""
        from ..persistence.repositories import DecisionRepo

        blocked: set = set()
        for d in DecisionRepo(session).list_open(None):
            if d.status.value == "OPEN":
                blocked.update(d.blocking_scope)
        return blocked

    def _resolve_profile(self, session, task, *, role: str | None = None) -> dict:  # noqa: ANN001
        """Resolve the executor profile for a task (IMPLEMENTATION.md §15.1):
        explicit contract profile > project role override > role default."""
        from ..persistence.repositories import ProjectRepo

        name = task.contract.executor_profile
        source = "contract"
        role = role or (task.contract.role.value if hasattr(task.contract.role, "value") else str(task.contract.role))
        if role == "auditor":
            # the auditor is NEVER the worker's own profile: a contract
            # profile belongs to the worker role. Independent reviewer by
            # construction: project auditor override > auditor default.
            name = None
        if not name:
            project = ProjectRepo(session).get_by_project_id(task.project_id)
            name = (project.policy.role_overrides or {}).get(role) if project else None
            source = "project_role_override" if name else "default"
        if not name:
            name = self._default_profile_name(role)
        return self._profile_dict(name, source)

    def _default_profile_name(self, role: str) -> str:
        """Default profile name for a role: <executor>_<role-suffix>.

        The planner schema leaves `role` a free string, so a real model may
        return any role label; unknown roles fall back to the worker profile
        (tasks are execution work) instead of crashing the dispatch loop."""
        prefix = {"reasonix": "reasonix", "codex": "codex", "fake": "fake"}.get(self.executor.name, "fake")
        suffix = ROLE_TO_PROFILE.get(role)
        if suffix is None:
            logger.warning(
                "unknown task role %r; defaulting to worker profile (task still runs)", role
            )
            suffix = "worker"
        return f"{prefix}_{suffix}"

    def _profile_dict(self, name: str, source: str) -> dict:
        profile_cfg = getattr(self.settings, "profiles", {}).get(name)
        if profile_cfg is None:
            raise ValueError(
                f"unknown executor profile {name!r} (source {source}); "
                "configure it in settings.profiles"
            )
        return {
            "name": name,
            "model": profile_cfg.model,
            "reasoning_effort": profile_cfg.reasoning_effort,
            "process_instance_id": profile_cfg.process_instance_id,
            "source": source,
        }

    def _planner_profile(self) -> dict:
        """Resolved profile for the planner turn (no task object exists yet)."""
        return self._profile_dict(self._default_profile_name("planner"), "default")

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
                RunRepo(session).save(run)  # domain objects need an explicit save
                if run.lease_token:
                    LeaseRepo(session).heartbeat(run.lease_token)
                session.commit()
            n += 1
        return n

    # ------------------------------------------------------------ drive
    async def _drive_run(self, run_id: str, task_id: str, *, role: str = "worker") -> None:
        """Execute one run with budget enforcement; heartbeat + lease renewal."""
        async with self._dispatch_sem:
            profile = await self._run_profile(run_id)
            budget = await self._run_budget(task_id)
            try:
                async with asyncio.timeout(budget):
                    await self._execute_with_heartbeat(run_id, profile, role=role)
            except TimeoutError:
                await self._collect_interrupt(run_id, "budget exceeded")
            except asyncio.CancelledError:
                await self._collect_interrupt(run_id, "cancelled")
            except Exception as exc:  # noqa: BLE001
                await self._collect_failure(run_id, str(exc))

    async def _run_profile(self, run_id: str) -> dict:
        """Read the profile frozen on the run at dispatch time."""
        with self.session_factory() as session:
            run = RunRepo(session).get_by_run_id(run_id)
            if run is None:
                return {}
            return {
                "name": run.executor_profile,
                "model": run.resolved_model,
                "reasoning_effort": run.reasoning_effort,
                "process_instance_id": run.process_instance_id,
                "source": run.configuration_source,
            }

    async def _run_budget(self, task_id: str) -> float:
        with self.session_factory() as session:
            task = TaskRepo(session).get_by_task_id(task_id)
            if task is None:
                return 900.0
            return float(task.contract.budget.max_wall_seconds or 900.0)

    async def _execute_with_heartbeat(self, run_id: str, profile: dict, *, role: str = "worker") -> None:
        """Run the executor turn, refreshing heartbeat every HEARTBEAT_SECONDS."""
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(run_id))
        try:
            with self.session_factory() as session:
                run = RunRepo(session).get_by_run_id(run_id)
                task = TaskRepo(session).get_by_task_id(run.task_id)
                # bounded, traceable context: build + persist the package in
                # the SAME transaction as the dispatch state (IMPLEMENTATION.md
                # §13): the run is recoverable and the exact text the model
                # saw is recorded before any model call happens.
                from ..application.context_package import ContextPackageBuilder

                builder = ContextPackageBuilder(session, data_dir=self.settings.data_dir)
                if role == "auditor":
                    # the audit package is built from the WORKER run under
                    # review (its structured result + real artifacts), never
                    # from the audit run itself (which has no result yet)
                    worker_run_id = (run.metadata or {}).get("worker_run_id") or task.current_run_id
                    worker_run = RunRepo(session).get_by_run_id(worker_run_id) if worker_run_id else None
                    if worker_run is None:
                        raise RuntimeError(
                            f"audit run {run.run_id}: worker run {worker_run_id!r} not found; cannot audit"
                        )
                    pkg = builder.persist(builder.auditor(task, worker_run, audit_run_id=run.run_id))
                else:
                    pkg = builder.persist(builder.worker(task, run=run))
                context = builder.to_context_dict(pkg, objective=task.contract.objective)
                # freeze the context id on the run for traceability
                run.metadata = dict(run.metadata or {})
                run.metadata["context_id"] = pkg.context_id
                RunRepo(session).save(run)
                session.commit()
            # record the executor session id as soon as it exists (recovery)
            def _on_session(sid: str) -> None:
                with self.session_factory() as session:
                    run = RunRepo(session).get_by_run_id(run_id)
                    if run is not None:
                        run.session_id = sid
                        RunRepo(session).save(run)
                        session.commit()

            hook = getattr(self.executor, "on_session_started", None)
            if hook is not None:
                hook(_on_session)
            # model-call invocation ledger: EVERY worker/auditor turn is
            # recorded durably (RUNNING -> SUCCEEDED/FAILED), matching the
            # planner turns recorded in plan_projects
            invocation = self._record_invocation(run_id, role, context, profile)
            try:
                if role == "auditor":
                    result, session_info = await self.executor.run_auditor(context, profile=profile)
                else:
                    result, session_info = await self.executor.run_worker(context, profile=profile)
            except Exception:
                self._finish_invocation(invocation, status="FAILED", error="executor turn failed")
                raise
            with self.session_factory() as session:
                run = RunRepo(session).get_by_run_id(run_id)
                dispatcher = RunDispatcher(session, self.executor)
                if role == "auditor":
                    dispatcher.collect_audit(run, result, session_info)
                else:
                    dispatcher.collect_success(run, result, session_info)
                session.commit()
            self._finish_invocation(
                invocation,
                status="SUCCEEDED",
                usage=getattr(session_info, "usage", None),
            )
        finally:
            heartbeat_task.cancel()

    def _record_invocation(self, run_id: str, role: str, context: dict, profile: dict):  # noqa: ANN001
        """Create a RUNNING invocation row for a worker/auditor turn (durable
        before the model call; the run row itself is not enough because the
        planner has no run)."""
        from ..domain.invocation import Invocation
        from ..persistence.repositories import InvocationRepo

        with self.session_factory() as session:
            run = RunRepo(session).get_by_run_id(run_id)
            if run is None:
                return None
            inv = Invocation(
                role=role,
                project_id=run.project_id,
                task_id=run.task_id,
                run_id=run.run_id,
                context_id=context.get("context_id"),
                profile_name=profile.get("name"),
                resolved_model=profile.get("model"),
                reasoning_effort=profile.get("reasoning_effort"),
                skills=list(getattr(self.executor, "installed_skills", []) or []),
                budget=dict(getattr(run, "metadata", {}) or {}).get("budget", {}) or {},
                status="RUNNING",
                created_by="scheduler",
            )
            InvocationRepo(session).save(inv)
            session.commit()
            return inv.invocation_id

    def _finish_invocation(self, invocation_id, *, status: str, usage=None, error: str = "") -> None:  # noqa: ANN001
        from ..domain.base import utcnow
        from ..persistence.repositories import InvocationRepo

        if not invocation_id:
            return
        with self.session_factory() as session:
            inv = InvocationRepo(session).get_by_invocation_id(invocation_id)
            if inv is None:
                return
            inv.status = status
            inv.finished_at = utcnow()
            if error:
                inv.error_message = error[:500]
            if usage is not None:
                inv.usage = usage
            elif status == "SUCCEEDED":
                inv.usage = {"available": False, "reason": "executor does not report usage"}
            InvocationRepo(session).save(inv)
            session.commit()

    async def _heartbeat_loop(self, run_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                with self.session_factory() as session:
                    run = RunRepo(session).get_by_run_id(run_id)
                    if run is None:
                        return
                    run.heartbeat()
                    RunRepo(session).save(run)
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
