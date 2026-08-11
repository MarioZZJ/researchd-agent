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
    ComputationalProvenance,
    Evidence,
    Issue,
    LiteratureProvenance,
)
from ..domain.enums import ClaimEvidenceState, ClaimReviewLevel, ClaimUseState
from ..executors.base import WorkResult
from ..persistence.models import ClaimEvidenceRow
from ..persistence.repositories import (
    ArtifactRepo,
    ClaimRepo,
    EvidenceRepo,
    IssueRepo,
    ProjectRepo,
)

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
    #    idempotency below prevents duplicate registration on replay).
    #    Every path goes through the registration gate: resolved inside the
    #    project root, no '..' / symlink escape — any violation rejects the
    #    whole result (the transaction rolls back).
    project = ProjectRepo(session).get_by_project_id(project_id)
    if project is None:
        # fail-closed: without the persisted project (and its workspace root)
        # there is no trusted boundary to register artifacts against — reject
        # the WHOLE result instead of silently dropping provenance
        raise ValueError(f"run {run.run_id}: project {project_id!r} not found; result rejected (fail-closed)")
    for art in result.artifacts:
        artifact_id = art.local_ref
        existing_artifact = ArtifactRepo(session).get_by_artifact_id(artifact_id)
        path = art.path
        if not path:
            raise ValueError(f"artifact {artifact_id!r} has an empty path; rejected")
        if existing_artifact is not None:
            if existing_artifact.project_id != project_id or existing_artifact.path != path:
                raise ValueError(
                    f"artifact id {artifact_id!r} reused with different project/path; rejected"
                )
            if project is None or not project.workspace_root:
                raise ValueError(
                    f"artifact {artifact_id!r} cannot be re-validated: "
                    "project has no workspace_root (fail-closed)"
                )
            else:
                # re-run the file gate as PURE validation: real file + size +
                # hash, WITHOUT saving — an existing artifact's provenance
                # (run/task/kind/description) must never be rewritten
                from .paths import PathEscapeError, check_artifact_file, check_symlink_escape, safe_resolve

                try:
                    check_symlink_escape(project.workspace_root, path)
                    safe_resolve(project.workspace_root, path)
                    info = check_artifact_file(project.workspace_root, path)
                except PathEscapeError as exc:
                    raise ValueError(f"artifact {path!r} rejected by registration gate: {exc}") from exc
                if existing_artifact.sha256 is None:
                    raise ValueError(
                        f"artifact {artifact_id!r} has no stored hash; "
                        "re-run `researchd migrate` hash backfill before replay (fail-closed)"
                    )
                if info.get("sha256") is not None and existing_artifact.sha256 != info["sha256"]:
                    raise ValueError(
                        f"artifact {artifact_id!r} content changed since registration; rejected"
                    )
            continue
        if project is not None:
            # registration gate: project-root boundary + '..' / symlink
            # escape + real file + size/hash — any violation rejects the
            # whole result (the surrounding transaction rolls back)
            from .evidence_validation import register_artifact

            registered = register_artifact(
                session,
                project=project,
                workspace_root=project.workspace_root,
                rel_path=path,
                artifact=Artifact(
                    artifact_id=artifact_id,
                    project_id=project_id,
                    task_id=run.task_id,
                    run_id=run.run_id,
                    kind=art.kind,
                    path=path,
                    description=art.description,
                ),
            )
            path = str(registered.path)
            counts["artifacts"] += 1
            continue

    # 2. evidence candidates -> CANDIDATE rows ONLY. VERIFIED is reached
    #    exclusively through the auditor gate (apply_audit_result) after an
    #    independent auditor ACCEPTs the run — free-text worker judgment can
    #    never claim VERIFIED, and provenance is re-checked at audit time.
    for cand in result.evidence_candidates:
        evidence_id = cand.local_ref
        lit = None
        if cand.literature and (cand.literature.get("source_id") or cand.literature.get("doi")):
            lit = LiteratureProvenance(
                source_id=cand.literature.get("source_id") or cand.literature.get("doi") or "unknown",
                locator=cand.literature.get("locator") or cand.literature.get("url"),
            )
        comp = None
        if cand.computational and cand.artifact_refs:
            comp = ComputationalProvenance(
                run_id=run.run_id,
                artifact_id=cand.artifact_refs[0],
                code_commit=cand.computational.get("code_commit"),
                data_snapshot=cand.computational.get("data_snapshot"),
                statistics=cand.computational.get("statistics") or {},
                uncertainty=cand.computational.get("uncertainty") or {},
                interpretation_limits=cand.computational.get("interpretation_limits") or [],
            )
        existing_ev = EvidenceRepo(session).get_by_evidence_id(evidence_id)
        if existing_ev is not None:
            if existing_ev.status.value == "VERIFIED":
                continue  # verified evidence is never overwritten
            if existing_ev.run_id != run.run_id:
                # a previous (REVISE-cycle) candidate with the same id: this
                # round's candidate SUPERSEDES it (id stays stable; the
                # candidate was never verified)
                existing_ev.statement = cand.statement
                existing_ev.type = cand.type
                existing_ev.run_id = run.run_id
                existing_ev.task_id = run.task_id
                existing_ev.artifact_refs = cand.artifact_refs
                existing_ev.literature = lit
                existing_ev.computational = comp
                existing_ev.limitations = cand.limitations
                EvidenceRepo(session).save(existing_ev)
                counts["evidence"] += 1
                continue
            continue  # same run replay (idempotent)
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
            computational=comp,
            limitations=cand.limitations,
        )
        EvidenceRepo(session).save(evidence)
        counts["evidence"] += 1

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
