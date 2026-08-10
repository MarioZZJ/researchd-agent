"""Domain enums: all state machines and fixed vocabularies."""

from __future__ import annotations

import enum


class StrEnum(str, enum.Enum):
    def __str__(self) -> str:  # pragma: no cover
        return self.value


class TaskStatus(StrEnum):
    PROPOSED = "PROPOSED"
    READY = "READY"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    REVIEW = "REVIEW"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    ORPHANED = "ORPHANED"


class EvidenceStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    VERIFIED = "VERIFIED"
    CONTESTED = "CONTESTED"
    INVALID = "INVALID"
    SUPERSEDED = "SUPERSEDED"


class EvidenceType(StrEnum):
    LITERATURE = "literature"
    COMPUTATIONAL = "computational"
    MODEL_ANNOTATION = "model_annotation"
    HUMAN = "human"
    OTHER = "other"


class ClaimEvidenceState(StrEnum):
    UNTESTED = "UNTESTED"
    SUPPORTED = "SUPPORTED"
    MIXED = "MIXED"
    UNSUPPORTED = "UNSUPPORTED"
    CONTRADICTED = "CONTRADICTED"


class ClaimReviewLevel(StrEnum):
    NONE = "NONE"
    INTERNAL = "INTERNAL"
    CROSS_MODEL = "CROSS_MODEL"
    PI = "PI"


class ClaimUseState(StrEnum):
    DRAFT = "DRAFT"
    MANUSCRIPT_ELIGIBLE = "MANUSCRIPT_ELIGIBLE"
    INCLUDED = "INCLUDED"
    RETIRED = "RETIRED"


class ClaimEvidenceRelation(StrEnum):
    SUPPORTS = "supports"
    CHALLENGES = "challenges"
    QUALIFIES = "qualifies"
    DEFINES_SCOPE = "defines_scope"
    PROVIDES_CONTEXT = "provides_context"
    INVALIDATES = "invalidates"


class IssueStatus(StrEnum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    RESOLVED = "RESOLVED"
    ACCEPTED_RISK = "ACCEPTED_RISK"
    SUPERSEDED = "SUPERSEDED"


class DecisionStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    APPLIED = "APPLIED"
    CLOSED = "CLOSED"
    WITHDRAWN = "WITHDRAWN"


class DecisionCategory(StrEnum):
    """Decision categories used for the decision fingerprint."""

    CHARTER_CHANGE = "charter_change"
    CORE_QUESTION = "core_question"
    INCLUSION_CRITERIA = "inclusion_criteria"
    ANALYSIS_STRATEGY = "analysis_strategy"
    NARRATIVE = "narrative"
    TITLE_ABSTRACT = "title_abstract"
    PUBLICATION = "publication"
    BUDGET_PERMISSION = "budget_permission"
    DESTRUCTIVE = "destructive"
    OTHER = "other"


class WorkOutcome(StrEnum):
    SUBMIT_FOR_REVIEW = "SUBMIT_FOR_REVIEW"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class AuditVerdict(StrEnum):
    ACCEPT = "ACCEPT"
    REVISE = "REVISE"
    BLOCK = "BLOCK"
    REJECT = "REJECT"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"
    SENT = "SENT"
    DEAD = "DEAD"


class OutboxDestination(StrEnum):
    DELIVERY = "delivery"  # cc-connect Delivery API / fake port


class ReportType(StrEnum):
    EVIDENCE = "EVIDENCE"
    CLAIM = "CLAIM"
    ISSUE = "ISSUE"
    DECISION = "DECISION"
    MILESTONE = "MILESTONE"
    EXCEPTION = "EXCEPTION"
    DIGEST = "DIGEST"


class ProjectStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    ARCHIVED = "ARCHIVED"


class TaskRole(StrEnum):
    INTERACTION = "interaction"
    PLANNER = "planner"
    WORKER_DEFAULT = "worker_default"
    LITERATURE_WORKER = "literature_worker"
    ANALYSIS_WORKER = "analysis_worker"
    AUDITOR = "auditor"
    CROSS_MODEL_REVIEWER = "cross_model_reviewer"
    REPORT_COMPRESSOR = "report_compressor"
    MANUSCRIPT_WRITER = "manuscript_writer"


class ActorType(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class InboundPlatform(StrEnum):
    FEISHU = "feishu"
    OTHER = "other"
