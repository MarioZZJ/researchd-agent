"""Event model: append-only audit events (IMPLEMENTATION.md §10).

Unified event format `researchd.event.v1`. Every scientifically meaningful
state change must, in ONE SQLite transaction: update the aggregate, append
the event, and (if notification is needed) insert an outbox row.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .base import Actor, AggregateRef, utcnow

EVENT_SCHEMA = "researchd.event.v1"

# Event type vocabulary (extend as phases land).
EVENT_TYPES = {
    "project.created",
    "project.paused",
    "project.resumed",
    "project.cancelled",
    "project.executor_policy_changed",
    "project.interaction_changed",
    "question.created",
    "task.proposed",
    "task.validated",
    "task.ready",
    "task.started",
    "task.blocked",
    "task.unblocked",
    "task.review_submitted",
    "task.completed",
    "task.failed",
    "task.cancelled",
    "task.contract_updated",
    "run.queued",
    "run.starting",
    "run.running",
    "run.succeeded",
    "run.failed",
    "run.interrupted",
    "run.orphaned",
    "artifact.registered",
    "artifact.invalidated",
    "evidence.candidate",
    "evidence.verified",
    "evidence.contested",
    "evidence.invalidated",
    "evidence.superseded",
    "claim.created",
    "claim.updated",
    "claim.evidence_recomputed",
    "claim.review_level_changed",
    "claim.use_state_changed",
    "issue.opened",
    "issue.investigating",
    "issue.resolved",
    "issue.accepted_risk",
    "issue.superseded",
    "decision.candidate",
    "decision.opened",
    "decision.answered",
    "decision.applied",
    "decision.closed",
    "decision.withdrawn",
    "decision.duplicate_rejected",
    "report.compiled",
    "report.sent",
    "outbox.delivered",
    "outbox.dead",
    "inbound.message_received",
    "inbound.message_duplicate",
    "projection.updated",
    "projection.human_patch",
    "document.created",
    "planner.ran",
    "plan_revision.proposed",
    "plan_revision.applied",
    "taste_rule.proposed",
    "taste_rule.applied",
    "milestone.reached",
    "result.applied",
    "audit.accepted",
    "audit.revised",
    "audit.blocked",
    "audit.rejected",
    "diagnostic.giveup",
}


class Event(BaseModel):
    model_config = {"extra": "forbid"}

    schema: str = EVENT_SCHEMA
    event_id: str
    event_type: str
    occurred_at: datetime = Field(default_factory=utcnow)
    project_id: str | None = None
    aggregate: AggregateRef
    actor: Actor = Field(default_factory=Actor)
    correlation_id: str | None = None
    causation_id: str | None = None
    idempotency_key: str
    payload: dict[str, Any] = Field(default_factory=dict)


def make_event(
    *,
    event_type: str,
    aggregate: AggregateRef,
    idempotency_key: str,
    project_id: str | None = None,
    actor: Actor | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    payload: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> Event:
    assert event_type in EVENT_TYPES, f"unknown event type {event_type!r}"
    return Event(
        event_id=event_id or f"EVT-{uuid.uuid4().hex[:24].upper()}",
        event_type=event_type,
        project_id=project_id,
        aggregate=aggregate,
        actor=actor or Actor(),
        correlation_id=correlation_id,
        causation_id=causation_id,
        idempotency_key=idempotency_key,
        payload=payload or {},
    )
