"""Report eligibility: State Snapshot Diff (IMPLEMENTATION.md §21.1).

Only these changes trigger a report:
- new/moved Evidence (verified) or Claim state changes;
- Issue state changes that affect the project;
- OPEN/ANSWERED/APPLIED Decisions;
- Milestones reached;
- Exceptions (unrecoverable failures);
- scheduled digests.

A report is only emitted when the snapshot differs from the last emitted one
(no state diff -> no send).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class StateSnapshot:
    """Deterministic hashable summary of reportable state for one project."""

    project_id: str
    verified_evidence_ids: list[str] = field(default_factory=list)
    claim_state: dict[str, str] = field(default_factory=dict)  # claim_id -> evidence_state
    open_decisions: list[str] = field(default_factory=list)
    answered_decisions: list[str] = field(default_factory=list)
    milestones: list[str] = field(default_factory=list)
    unresolved_issues: list[str] = field(default_factory=list)
    ts: datetime | None = None

    def signature(self) -> str:
        import hashlib
        import json

        payload = {
            "verified_evidence": sorted(self.verified_evidence_ids),
            "claims": {k: self.claim_state[k] for k in sorted(self.claim_state)},
            "open_decisions": sorted(self.open_decisions),
            "answered_decisions": sorted(self.answered_decisions),
            "milestones": sorted(self.milestones),
            "unresolved_issues": sorted(self.unresolved_issues),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass
class DiffResult:
    changed: bool
    reasons: list[str] = field(default_factory=list)
    previous: StateSnapshot | None = None
    current: StateSnapshot | None = None


def diff_snapshots(previous: StateSnapshot | None, current: StateSnapshot) -> DiffResult:
    """True when reportable state changed (or no report was emitted yet)."""
    if previous is None:
        return DiffResult(changed=True, reasons=["first report"], current=current)
    reasons: list[str] = []
    if current.verified_evidence_ids != previous.verified_evidence_ids:
        reasons.append("evidence changed")
    if current.claim_state != previous.claim_state:
        reasons.append("claims changed")
    if current.open_decisions != previous.open_decisions:
        reasons.append("decisions changed")
    if current.answered_decisions != previous.answered_decisions:
        reasons.append("decisions answered")
    if current.milestones != previous.milestones:
        reasons.append("milestones changed")
    if current.unresolved_issues != previous.unresolved_issues:
        reasons.append("issues changed")
    return DiffResult(
        changed=bool(reasons),
        reasons=reasons,
        previous=previous,
        current=current,
    )
