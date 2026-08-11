"""FakeExecutor + FakeDeliveryPort: deterministic, script-driven test doubles.

Scripts describe per-call behavior, so tests (including the golden path) are
fully deterministic and can inject schema failures, conflicts, and crashes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .base import (
    AuditResult,
    ExecutorAdapter,
    ExecutorCapabilities,
    ExecutorSessionInfo,
    PlannerResult,
    WorkResult,
    validate_audit_result,
    validate_planner_result,
    validate_work_result,
)

RoleKind = str  # "planner" | "worker" | "auditor"


class ScriptStep:
    """One scripted behavior for a (role, task_id, call_index) triple.
    task_id None matches any task; otherwise the step is only used for the
    matching task (deterministic per-task scripts)."""

    def __init__(
        self,
        *,
        action: str = "return",  # return | raise | hang
        payload: dict | None = None,
        error: str | None = None,
        delay: float = 0.0,
        task_id: str | None = None,
    ):
        self.action = action
        self.payload = payload or {}
        self.error = error
        self.delay = delay
        self.task_id = task_id


class FakeExecutor(ExecutorAdapter):
    """Scripted executor. Call `script()` to queue behaviors per role."""

    name = "fake"
    capabilities = ExecutorCapabilities(
        supports_session_new=True,
        supports_session_load=True,
        supports_session_resume=True,
        supports_steering=True,
        supports_cancel=True,
        supports_structured_output=True,
        supports_model_override=True,
        supports_reasoning_override=True,
        supports_tool_approval=True,
    )

    def __init__(self, *, workspace_root: Path | None = None):
        self.workspace_root = workspace_root
        self._scripts: dict[str, list[ScriptStep]] = {}
        self._calls: dict[str, int] = {}
        self.sessions: list[ExecutorSessionInfo] = []
        self.raw_outputs: list[dict] = []

    def script(self, role: RoleKind, step: ScriptStep | dict) -> None:
        if isinstance(step, dict):
            step = ScriptStep(**step)
        self._scripts.setdefault(role, []).append(step)

    def _next(self, role: RoleKind, task_id: str | None = None) -> ScriptStep | None:
        """Next unused step for this role: prefer a step bound to task_id,
        else the next unbound step. Each call bumps the per-role call count
        (used for stable session/turn ids)."""
        self._calls[role] = self._calls.get(role, 0) + 1
        steps = self._scripts.get(role, [])
        if task_id is not None:
            for i, step in enumerate(steps):
                if step.task_id == task_id and not getattr(step, "_used", False):
                    step._used = True
                    return step
        for i, step in enumerate(steps):
            if step.task_id is None and not getattr(step, "_used", False):
                step._used = True
                return step
        return None

    async def _run(self, role: RoleKind, context: dict, payload_default: dict) -> tuple[Any, ExecutorSessionInfo]:
        """Scripted execution with a structured-output repair loop: a schema
        validation failure is retried with the next scripted step (mimics the
        reasonix adapter's repair loop) before surfacing as a failure."""
        session = ExecutorSessionInfo(
            executor="fake",
            process_instance_id=f"fake-{role}-{self._calls.get(role, 0)}",
            session_id=f"SES-{role}-{self._calls.get(role, 0)}",
            turn_id=f"TURN-{role}-{self._calls.get(role, 0)}",
        )
        self.sessions.append(session)

        def _take_step(*, allow_default: bool) -> ScriptStep | None:
            task_id = (context.get("task") or {}).get("task_id") or context.get("task_id")
            step = self._next(role, task_id=task_id)
            if step is not None:
                return step
            # only the FIRST attempt may fall back to the default payload
            # (unscripted executor); a repair retry with no scripted step
            # must surface the original validation failure instead of
            # silently substituting a synthetic result
            return ScriptStep(payload=payload_default) if allow_default else None

        def _validate(raw: dict):
            if role == "planner":
                return validate_planner_result(raw)
            if role == "worker":
                return validate_work_result(raw)
            return validate_audit_result(raw)

        first = True
        first_error: Exception | None = None
        for attempt in range(4):
            step = _take_step(allow_default=first)
            first = False
            if step is None:
                # repair retry with no scripted step: surface the ORIGINAL
                # validation failure (never a synthetic substitute)
                if first_error is not None:
                    raise first_error
                raise RuntimeError(f"fake {role} repair loop exhausted (no scripted step)")
            if step.delay:
                await asyncio.sleep(step.delay)
            if step.action == "raise":
                raise RuntimeError(step.error or f"fake {role} failure")
            if step.action == "hang":
                await asyncio.sleep(3600)
            raw = dict(step.payload)
            self.raw_outputs.append({"role": role, "raw": raw})
            try:
                return _validate(raw), session
            except Exception as exc:
                first_error = first_error or exc
                if attempt >= 3:
                    raise  # repair loop exhausted -> real failure
                # repair: retry with the next scripted step
        if first_error is not None:
            raise first_error
        raise RuntimeError(f"fake {role} repair loop exhausted")

    async def run_planner(self, context: dict, *, profile: dict) -> tuple[PlannerResult, ExecutorSessionInfo]:
        default = {
            "schema": "researchd.planner_result.v1",
            "proposed_tasks": [],
            "risks": [],
            "plan_revisions": [],
        }
        return await self._run("planner", context, default)

    async def run_worker(self, context: dict, *, profile: dict) -> tuple[WorkResult, ExecutorSessionInfo]:
        default = {
            "schema": "researchd.work_result.v1",
            "task_id": context.get("task_id") or context.get("task", {}).get("task_id", "T-UNKNOWN"),
            "outcome": "SUBMIT_FOR_REVIEW",
            "criteria_results": [],
            "artifacts": [],
            "evidence_candidates": [],
            "claim_changes": [],
            "issues": [],
            "decision_candidates": [],
            "next_task_proposals": [],
        }
        return await self._run("worker", context, default)

    async def run_auditor(self, context: dict, *, profile: dict) -> tuple[AuditResult, ExecutorSessionInfo]:
        default = {
            "schema": "researchd.audit_result.v1",
            "task_id": context.get("task_id") or context.get("task", {}).get("task_id", "T-UNKNOWN"),
            "verdict": "ACCEPT",
            "checks": [],
        }
        return await self._run("auditor", context, default)


class FakeDeliveryPort:
    """In-memory delivery port: records deliveries, can fail or crash on demand.

    Mirrors the cc-connect Delivery API surface (IMPLEMENTATION.md §19.2):
    deliver(kind, payload, attachments, idempotency_key) -> platform_message_id
    update(platform_message_id, payload)

    Idempotency contract: the same idempotency_key is delivered exactly once
    (the key -> message id mapping is cached), so outbox replays after a crash
    never double-deliver.
    """

    def __init__(self):
        self.deliveries: list[dict] = []
        self.updates: list[dict] = []
        self.fail_next: int = 0
        self.crash_after_commit: bool = False
        self.delivery_id_counter: int = 1000
        self._by_key: dict[str, str] = {}

    async def deliver(self, *, idempotency_key: str, kind: str, payload: dict, attachments: list | None = None, project_id: str | None = None) -> str:
        if idempotency_key in self._by_key:
            # replay: same key -> same platform message id, no new message
            return self._by_key[idempotency_key]
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("fake delivery failure")
        if self.crash_after_commit:
            # simulate: message "sent" but the receipt never reaches the sender
            # (recorded BEFORE raising so a replay returns the same id)
            self.delivery_id_counter += 1
            msg_id = f"MSG-{self.delivery_id_counter}"
            self._by_key[idempotency_key] = msg_id
            raise RuntimeError("fake crash after send, receipt lost")
        self.delivery_id_counter += 1
        msg_id = f"MSG-{self.delivery_id_counter}"
        self._by_key[idempotency_key] = msg_id
        self.deliveries.append(
            {
                "idempotency_key": idempotency_key,
                "kind": kind,
                "payload": payload,
                "attachments": attachments or [],
                "project_id": project_id,
                "platform_message_id": msg_id,
            }
        )
        return msg_id

    async def update(self, platform_message_id: str, payload: dict) -> None:
        self.updates.append({"platform_message_id": platform_message_id, "payload": payload})

    def find(self, idempotency_key: str) -> dict | None:
        for d in self.deliveries:
            if d["idempotency_key"] == idempotency_key:
                return d
        return None
