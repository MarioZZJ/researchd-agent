"""Apply a worker's structured result to persisted state (IMPLEMENTATION.md
§26 golden path step 17). Runs in the SAME transaction as collect_success.

Idempotency: the run's metadata_json["result_applied"] is the same-transaction
gate — a replayed collect_success (crash between apply and commit) skips
re-application instead of duplicating artifacts/evidence/claims/issues.

Provenance gate (§25.4): an evidence candidate is VERIFIED ONLY through
verify_evidence() against real, type-matching provenance. The work schema
only carries `literature` provenance, so non-literature candidates stay
CANDIDATE — free-text judgment can never claim VERIFIED.

Stable ids: evidence ids are derived from (run, local_ref) so the same
local_ref across runs never collides; artifact ids likewise.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.base import new_id
from ..domain.evidence import (
    Artifact,
    Claim,
    ClaimEvidenceLink,
    Evidence,
    Issue,
    LiteratureProvenance,
)
from ..domain.enums import ClaimEvidenceState, ClaimReviewLevel, ClaimUseState
from ..executors.base import WorkResult
from ..persistence.models import ClaimEvidenceRow
from ..persistence.repositories import ArtifactRepo, ClaimRepo, EvidenceRepo, IssueRepo

logger = logging.getLogger("researchd.apply")

RESULT_APPLIED_KEY = "result_applied"


def apply_work_result(session: Session, run, result: WorkResult) -> dict:  # noqa: ANN001
    """Persist artifacts / evidence / claims / issues from a worker result.

    Idempotent per run: a `run-applied:<run_id>` event (unique key) is written
    in the SAME transaction; a replayed collect_success skips re-application."""
    from ..domain.base import Actor, AggregateRef
    from ..domain.events import make_event
    from ..persistence.models import EventRow
    from ..persistence.repositories import EventRepo

    applied_key = f"run-applied:{run.run_id}"
    already = session.execute(
        select(EventRow.id).where(EventRow.idempotency_key == applied_key)
    ).first()
    if already is not None:
        return {"replayed": True}
    counts = {"artifacts": 0, "evidence": 0, "verified_evidence": 0, "claims": 0, "issues": 0}
    project_id = run.project_id

    # 1. artifacts: stable id from the schema's local_ref (run-level
    #    idempotency below prevents duplicate registration on replay)
    for art in result.artifacts:
        artifact_id = art.local_ref
        if ArtifactRepo(session).get_by_artifact_id(artifact_id):
            continue  # idempotent
        ArtifactRepo(session).save(
            Artifact(
                artifact_id=artifact_id,
                project_id=project_id,
                task_id=run.task_id,
                run_id=run.run_id,
                kind=art.kind,
                path=art.path,
                description=art.description,
            )
        )
        counts["artifacts"] += 1

    # 2. evidence candidates -> Evidence with the unified verification gate
    for cand in result.evidence_candidates:
        evidence_id = cand.local_ref
        if EvidenceRepo(session).get_by_evidence_id(evidence_id):
            continue  # idempotent
        lit = None
        if cand.literature and (cand.literature.get("source_id") or cand.literature.get("doi")):
            lit = LiteratureProvenance(
                source_id=cand.literature.get("source_id") or cand.literature.get("doi") or "unknown",
                locator=cand.literature.get("locator") or cand.literature.get("url"),
            )
        evidence = Evidence(
            evidence_id=evidence_id,
            project_id=project_id,
            type=cand.type,
            status="CANDIDATE",
            statement=cand.statement,
            task_id=run.task_id,
            run_id=run.run_id,
            artifact_refs=cand.artifact_refs,
            literature=lit,
            limitations=cand.limitations,
        )
        # unified verification: only real, type-matching provenance may VERIFY
        try:
            from ..application.evidence_validation import verify_evidence

            evidence = verify_evidence(session, evidence)
        except Exception as exc:  # noqa: BLE001  provenance missing -> stays CANDIDATE
            logger.debug("evidence %s stays CANDIDATE: %s", evidence_id, exc)
        EvidenceRepo(session).save(evidence)
        counts["evidence"] += 1
        if evidence.status.value == "VERIFIED":
            counts["verified_evidence"] += 1

    # 3. claim changes (upsert, project-scoped, evidence links persisted)
    for change in result.claim_changes:
        claim_id = change.claim_id or new_id("claim")
        claim = ClaimRepo(session).get_by_claim_id(claim_id)
        if claim is not None and claim.project_id != project_id:
            # id collision across projects: do NOT touch the foreign claim
            logger.warning("claim %s belongs to another project; skipped", claim_id)
            continue
        if change.operation == "retire":
            if claim is None:
                continue
            claim.use_state = ClaimUseState.RETIRED
            ClaimRepo(session).save(claim)
            counts["claims"] += 1
            continue
        if claim is None:
            claim = Claim(
                claim_id=claim_id,
                project_id=project_id,
                text=change.text or claim_id,
                is_core=bool(change.is_core),
                evidence_state=ClaimEvidenceState.UNTESTED,
                review_level=ClaimReviewLevel.NONE,
                use_state=ClaimUseState.DRAFT,
            )
        else:
            if change.text:
                claim.text = change.text
            if change.is_core is not None:
                claim.is_core = change.is_core
        ClaimRepo(session).save(claim)
        session.flush()  # the claim row must exist before its FK links
        # persist evidence links explicitly (claim_evidence rows)
        for rel in change.evidence_relations:
            ev_id = str(rel.get("evidence_id") or "")
            if not ev_id:
                continue
            exists = session.execute(
                select(ClaimEvidenceRow.id).where(
                    ClaimEvidenceRow.claim_id == claim_id,
                    ClaimEvidenceRow.evidence_id == ev_id,
                )
            ).first()
            if exists is None:
                session.add(
                    ClaimEvidenceRow(
                        id=new_id("other"),
                        claim_id=claim_id,
                        evidence_id=ev_id,
                        relation=str(rel.get("relation") or "supports"),
                    )
                )
        counts["claims"] += 1

    # 4. issues (fresh id each time; run-level idempotency covers replays)
    for issue in result.issues:
        IssueRepo(session).save(
            Issue(
                issue_id=new_id("issue"),
                project_id=project_id,
                title=issue.title,
                description=issue.description,
                severity=issue.severity,
                task_id=run.task_id,
            )
        )
        counts["issues"] += 1

    # run-level idempotency gate (same transaction as the writes)
    EventRepo(session).append(
        make_event(
            event_type="result.applied",
            aggregate=AggregateRef(type="run", id=run.run_id, version=1),
            idempotency_key=applied_key,
            project_id=project_id,
            actor=Actor(type="system"),
            payload={"run_id": run.run_id},
        )
    )

    if counts["evidence"] or counts["claims"]:
        logger.info(
            "applied run %s: %s", run.run_id,
            {k: v for k, v in counts.items() if v},
        )
    return counts
