"""Task domain object: contract, state machine, success criteria (IMPLEMENTATION.md §7.1, §9)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .base import DomainModel, new_id, utcnow
from .enums import TaskRole, TaskStatus
from .state_machine import InvalidTransition, TaskStateMachine


class SuccessCriterion(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    text: str


class Budget(BaseModel):
    model_config = {"extra": "forbid"}

    max_wall_seconds: int | None = None
    max_executor_turns: int | None = None
    max_model_calls: int | None = None
    max_parallel_workers: int | None = None


class TaskContract(BaseModel):
    """Research-oriented contract. 'Write a script' engineering tasks are forbidden."""

    model_config = {"extra": "forbid"}

    task_id: str
    role: TaskRole | str
    objective: str
    why_now: str = ""
    inputs: list[str] = Field(default_factory=list)  # evidence/artifact/decision ids
    deliverables: list[str] = Field(default_factory=list)
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    escalation_conditions: list[str] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    executor_profile: str | None = None

    def validate_contract(self) -> list[str]:
        """Contract validation errors; empty list == valid."""
        errors: list[str] = []
        if not self.objective.strip():
            errors.append("objective is empty")
        if not self.success_criteria:
            errors.append("success_criteria is empty")
        for sc in self.success_criteria:
            if not sc.id.strip() or not sc.text.strip():
                errors.append(f"success_criterion {sc.id!r} is incomplete")
        return errors


class Task(DomainModel):
    task_id: str = Field(default_factory=lambda: new_id("task"))
    status: TaskStatus = TaskStatus.PROPOSED
    contract: TaskContract
    parent_task_id: str | None = None
    blocked_by: list[str] = Field(default_factory=list)  # decision ids
    depends_on: list[str] = Field(default_factory=list)  # task ids
    current_run_id: str | None = None
    lease_token: str | None = None
    error_message: str | None = None

    def __init__(self, **data):
        tid = data.get("task_id")
        if not tid:
            data["task_id"] = new_id("task")
        # keep task_id stable as the public id
        data.setdefault("id", data["task_id"])
        super().__init__(**data)

    @field_validator("status", mode="before")
    @classmethod
    def _coerce(cls, v):
        return TaskStatus(v) if v and not isinstance(v, TaskStatus) else v

    def _sm(self) -> TaskStateMachine:
        return TaskStateMachine("task", self.status)

    def transition(self, target: TaskStatus | str, *, guard: bool = True, reason: str = "") -> TaskStatus:
        target = TaskStatus(target) if isinstance(target, str) else target
        # COMPLETED is only reachable through complete() with all criteria PASS
        if target is TaskStatus.COMPLETED:
            raise InvalidTransition(
                "task", self.status, target,
                "COMPLETED only via complete() when every success criterion is PASS",
            )
        self.status = self._sm().transition(target, guard=guard, reason=reason)
        self.updated_at = utcnow()
        return self.status

    def propose_ready(self, contract_errors: list[str] | None = None) -> TaskStatus:
        errors = contract_errors if contract_errors is not None else self.contract.validate_contract()
        if errors:
            raise InvalidTransition("task", self.status, TaskStatus.READY, f"contract invalid: {errors}")
        return self.transition(TaskStatus.READY)

    def start(self, run_id: str, lease_token: str) -> TaskStatus:
        if self.status is not TaskStatus.READY:
            raise InvalidTransition("task", self.status, TaskStatus.RUNNING, "task not READY")
        self.current_run_id = run_id
        self.lease_token = lease_token
        self.status = TaskStatus.RUNNING
        self.updated_at = utcnow()
        return self.status

    def block(self, decision_id: str | None = None) -> TaskStatus:
        if decision_id and decision_id not in self.blocked_by:
            self.blocked_by.append(decision_id)
        return self.transition(TaskStatus.BLOCKED)

    def submit_review(self) -> TaskStatus:
        return self.transition(TaskStatus.REVIEW)

    def complete(self, criteria_results: list[dict]) -> TaskStatus:
        """Complete only when ALL success criteria explicitly PASS (guard).

        criteria_results is required: completing without evidence of criteria
        verification is forbidden (IMPLEMENTATION.md §7.1).
        """
        if criteria_results is None:
            raise InvalidTransition("task", self.status, TaskStatus.COMPLETED, "criteria_results required")
        results = {c.get("criterion_id"): c.get("status") for c in criteria_results}
        ok = all(results.get(sc.id) == "PASS" for sc in self.contract.success_criteria)
        if not ok:
            raise InvalidTransition(
                "task", self.status, TaskStatus.COMPLETED, "every success criterion must be PASS"
            )
        if not self._sm().can(TaskStatus.COMPLETED):
            raise InvalidTransition("task", self.status, TaskStatus.COMPLETED)
        self.status = TaskStatus.COMPLETED
        self.updated_at = utcnow()
        return self.status

    def fail(self, message: str) -> TaskStatus:
        self.error_message = message
        return self.transition(TaskStatus.FAILED)

    def requeue(self, reason: str = "") -> TaskStatus:
        """REVIEW -> READY (modify or rerun)."""
        self.error_message = reason or None
        return self.transition(TaskStatus.READY)

    def cancel(self, reason: str = "") -> TaskStatus:
        self.error_message = reason or None
        return self.transition(TaskStatus.CANCELLED)


class TaskDependency(BaseModel):
    model_config = {"extra": "forbid"}

    task_id: str
    depends_on: str
