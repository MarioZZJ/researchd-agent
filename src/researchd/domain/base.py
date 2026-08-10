"""Domain base types: ULID ids, timestamps, common fields."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

ID_PREFIXES = {
    "project": "P",
    "question": "Q",
    "task": "T",
    "run": "R",
    "artifact": "A",
    "evidence": "E",
    "claim": "C",
    "issue": "I",
    "decision": "D",
    "report": "RPT",
    "context_package": "CP",
    "plan_revision": "PR",
    "taste_rule": "TR",
}


def new_id(kind: str) -> str:
    """Human-friendly prefixed ULID-ish id, e.g. T-01K3..."""
    ulid = str(uuid.uuid4()).replace("-", "").upper()[:24]
    prefix = ID_PREFIXES.get(kind, "X")
    return f"{prefix}-{ulid}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class DomainModel(BaseModel):
    """Base for domain schemas; all timestamps UTC, metadata is free-form JSON."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(default_factory=lambda: new_id("other"))
    project_id: str | None = None
    version: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    created_by: str = "system"
    metadata: dict = Field(default_factory=dict)


class Actor(BaseModel):
    """Who caused an event."""

    model_config = ConfigDict(extra="forbid")

    type: str = "system"  # human | agent | system
    role: str | None = None
    executor: str | None = None
    model: str | None = None
    run_id: str | None = None
    platform_user_id: str | None = None
    display_name: str | None = None


class AggregateRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    id: str
    version: int
