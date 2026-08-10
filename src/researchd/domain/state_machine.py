"""State machine definitions and transition guards.

Every state machine in IMPLEMENTATION.md §7 is encoded here as an explicit
transition table plus guards. Guards encode the "forced invariants":

- RUNNING may never transition directly to COMPLETED (not even in the table).
- A Task may only COMPLETE when all success criteria pass (guard).
- Completed Tasks never transition back (terminal state).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .enums import (
    AuditVerdict,
    ClaimEvidenceState,
    ClaimReviewLevel,
    ClaimUseState,
    DecisionStatus,
    EvidenceStatus,
    IssueStatus,
    RunStatus,
    TaskStatus,
)


class InvalidTransition(Exception):
    """Raised when a state transition is not allowed."""

    def __init__(self, obj: str, current: object, target: object, reason: str = ""):
        self.obj = obj
        self.current = current
        self.target = target
        msg = f"{obj}: invalid transition {current} -> {target}"
        if reason:
            msg += f" ({reason})"
        super().__init__(msg)


class StateMachine:
    """Generic explicit transition table with guards."""

    transitions: dict[object, set[object]] = {}

    def __init__(self, name: str, state: object):
        self.name = name
        self.state = state

    def can(self, target: object, *, guard: bool = True) -> bool:
        return guard and target in self.transitions.get(self.state, set())

    def transition(self, target: object, *, guard: bool = True, reason: str = "") -> object:
        if not self.can(target, guard=guard):
            raise InvalidTransition(self.name, self.state, target, reason)
        self.state = target
        return self.state


class TaskStateMachine(StateMachine):
    transitions: dict[TaskStatus, set[TaskStatus]] = {
        TaskStatus.PROPOSED: {TaskStatus.READY, TaskStatus.CANCELLED},
        TaskStatus.READY: {TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
        TaskStatus.RUNNING: {TaskStatus.REVIEW, TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.CANCELLED},
        TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.REVIEW, TaskStatus.CANCELLED},
        TaskStatus.REVIEW: {TaskStatus.COMPLETED, TaskStatus.READY, TaskStatus.FAILED, TaskStatus.CANCELLED},
        TaskStatus.COMPLETED: set(),
        TaskStatus.FAILED: set(),
        TaskStatus.CANCELLED: set(),
    }

    # RUNNING -> COMPLETED is intentionally absent from the table.

    @staticmethod
    def complete_guard(all_criteria_pass: bool) -> bool:
        return all_criteria_pass


class RunStateMachine(StateMachine):
    transitions: dict[RunStatus, set[RunStatus]] = {
        RunStatus.QUEUED: {RunStatus.STARTING, RunStatus.FAILED, RunStatus.INTERRUPTED},
        RunStatus.STARTING: {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.INTERRUPTED},
        RunStatus.RUNNING: {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.INTERRUPTED, RunStatus.ORPHANED},
        RunStatus.SUCCEEDED: set(),
        RunStatus.FAILED: set(),
        RunStatus.INTERRUPTED: set(),
        RunStatus.ORPHANED: set(),
    }


class EvidenceStateMachine(StateMachine):
    transitions: dict[EvidenceStatus, set[EvidenceStatus]] = {
        EvidenceStatus.CANDIDATE: {EvidenceStatus.VERIFIED, EvidenceStatus.CONTESTED, EvidenceStatus.INVALID, EvidenceStatus.SUPERSEDED},
        EvidenceStatus.VERIFIED: {EvidenceStatus.CONTESTED, EvidenceStatus.INVALID, EvidenceStatus.SUPERSEDED},
        EvidenceStatus.CONTESTED: {EvidenceStatus.VERIFIED, EvidenceStatus.INVALID, EvidenceStatus.SUPERSEDED},
        EvidenceStatus.INVALID: set(),
        EvidenceStatus.SUPERSEDED: set(),
    }


class IssueStateMachine(StateMachine):
    transitions: dict[IssueStatus, set[IssueStatus]] = {
        IssueStatus.OPEN: {IssueStatus.INVESTIGATING, IssueStatus.SUPERSEDED},
        IssueStatus.INVESTIGATING: {IssueStatus.RESOLVED, IssueStatus.ACCEPTED_RISK, IssueStatus.SUPERSEDED},
        IssueStatus.RESOLVED: set(),
        IssueStatus.ACCEPTED_RISK: set(),
        IssueStatus.SUPERSEDED: set(),
    }


class DecisionStateMachine(StateMachine):
    transitions: dict[DecisionStatus, set[DecisionStatus]] = {
        DecisionStatus.CANDIDATE: {DecisionStatus.OPEN},
        DecisionStatus.OPEN: {DecisionStatus.ANSWERED, DecisionStatus.WITHDRAWN},
        DecisionStatus.ANSWERED: {DecisionStatus.APPLIED, DecisionStatus.WITHDRAWN, DecisionStatus.CLOSED},
        DecisionStatus.APPLIED: {DecisionStatus.CLOSED},
        DecisionStatus.CLOSED: set(),
        DecisionStatus.WITHDRAWN: set(),
    }


class ClaimEvidenceStateMachine(StateMachine):
    transitions: dict[ClaimEvidenceState, set[ClaimEvidenceState]] = {
        ClaimEvidenceState.UNTESTED: {ClaimEvidenceState.SUPPORTED, ClaimEvidenceState.MIXED, ClaimEvidenceState.UNSUPPORTED, ClaimEvidenceState.CONTRADICTED},
        ClaimEvidenceState.SUPPORTED: {ClaimEvidenceState.MIXED, ClaimEvidenceState.UNSUPPORTED, ClaimEvidenceState.CONTRADICTED},
        ClaimEvidenceState.MIXED: {ClaimEvidenceState.SUPPORTED, ClaimEvidenceState.UNSUPPORTED, ClaimEvidenceState.CONTRADICTED},
        ClaimEvidenceState.UNSUPPORTED: {ClaimEvidenceState.SUPPORTED, ClaimEvidenceState.MIXED, ClaimEvidenceState.CONTRADICTED},
        ClaimEvidenceState.CONTRADICTED: {ClaimEvidenceState.SUPPORTED, ClaimEvidenceState.MIXED, ClaimEvidenceState.UNSUPPORTED},
    }


class ClaimReviewStateMachine(StateMachine):
    transitions: dict[ClaimReviewLevel, set[ClaimReviewLevel]] = {
        ClaimReviewLevel.NONE: {ClaimReviewLevel.INTERNAL, ClaimReviewLevel.CROSS_MODEL, ClaimReviewLevel.PI},
        ClaimReviewLevel.INTERNAL: {ClaimReviewLevel.CROSS_MODEL, ClaimReviewLevel.PI},
        ClaimReviewLevel.CROSS_MODEL: {ClaimReviewLevel.PI},
        ClaimReviewLevel.PI: set(),
    }


class ClaimUseStateMachine(StateMachine):
    transitions: dict[ClaimUseState, set[ClaimUseState]] = {
        ClaimUseState.DRAFT: {ClaimUseState.MANUSCRIPT_ELIGIBLE, ClaimUseState.RETIRED},
        ClaimUseState.MANUSCRIPT_ELIGIBLE: {ClaimUseState.INCLUDED, ClaimUseState.RETIRED},
        ClaimUseState.INCLUDED: {ClaimUseState.RETIRED},
        ClaimUseState.RETIRED: set(),
    }


@dataclass
class AuditTrail:
    """Human-readable audit of a WorkResult verdict path (used by review_policy)."""

    verdict: AuditVerdict
    reasons: list[str] = field(default_factory=list)
