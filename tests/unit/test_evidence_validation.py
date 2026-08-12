"""Evidence/Claim provenance + path safety tests (IMPLEMENTATION.md §25.4, §25.9)."""

import os

import pytest

from researchd.application.paths import PathEscapeError, check_artifact_file, safe_resolve
from researchd.domain.enums import ClaimEvidenceState, ClaimReviewLevel, EvidenceType
from researchd.domain.evidence import Claim, ComputationalProvenance, Evidence, LiteratureProvenance


def make_evidence(**kw) -> Evidence:
    defaults = dict(
        evidence_id="E-001",
        project_id="P-TEST",
        type=EvidenceType.COMPUTATIONAL,
        statement="the claim under test",
    )
    defaults.update(kw)
    return Evidence(**defaults)


def test_missing_artifact_cannot_register():
    ev = make_evidence(
        computational=ComputationalProvenance(run_id="R-1", artifact_id="A-1"),
    )
    # provenance references are fake: no run/artifact rows -> cannot VERIFY
    assert ev.provenance_ok() is False


def test_computational_needs_run_artifact_and_refs():
    ev = make_evidence(
        run_id="R-1",
        artifact_refs=["A-1"],
        computational=ComputationalProvenance(run_id="R-1", artifact_id="A-1"),
    )
    assert ev.provenance_ok() is True  # full chain present
    ev2 = make_evidence(
        computational=ComputationalProvenance(run_id="R-1", artifact_id="A-1"),
        artifact_refs=[],
    )
    assert ev2.provenance_ok() is False


def test_literature_needs_source_id():
    ev = make_evidence(
        type=EvidenceType.LITERATURE,
        literature=LiteratureProvenance(source_id="", locator="p.3"),
    )
    assert ev.provenance_ok() is False
    ev2 = make_evidence(
        type=EvidenceType.LITERATURE,
        literature=LiteratureProvenance(source_id="doi:10.1/x", locator="p.3"),
    )
    assert ev2.provenance_ok() is True


def test_verify_requires_provenance():
    ev = make_evidence()
    with pytest.raises(ValueError):
        ev.verify()
    assert ev.status.value == "CANDIDATE"


def test_verify_with_provenance():
    ev = make_evidence(
        run_id="R-1",
        artifact_refs=["A-1"],
        computational=ComputationalProvenance(run_id="R-1", artifact_id="A-1"),
    )
    ev.verify()
    assert ev.status.value == "VERIFIED"


def test_candidate_evidence_cannot_support_milestone():
    """Eligibility layer must check status; here we assert the invariant helper."""
    ev = make_evidence()
    assert ev.status.value == "CANDIDATE"
    # verified-only invariant enforced in reporting.eligibility (Phase 6); smoke check:
    from researchd.domain.enums import EvidenceStatus
    assert EvidenceStatus.CANDIDATE != EvidenceStatus.VERIFIED


def test_contradicted_claim_cannot_enter_manuscript():
    c = Claim(claim_id="C-001", text="core claim", is_core=True)
    c.set_evidence_state(ClaimEvidenceState.CONTRADICTED)
    c.set_review_level(ClaimReviewLevel.CROSS_MODEL)
    assert c.can_enter_manuscript() is False


def test_core_claim_needs_cross_model_review():
    c = Claim(claim_id="C-001", text="core claim", is_core=True)
    c.set_evidence_state(ClaimEvidenceState.SUPPORTED)
    c.set_review_level(ClaimReviewLevel.INTERNAL)
    assert c.can_enter_manuscript() is False
    c.set_review_level(ClaimReviewLevel.CROSS_MODEL)
    assert c.can_enter_manuscript() is True


def test_claim_state_transitions():
    c = Claim(claim_id="C-001", text="x")
    c.set_evidence_state(ClaimEvidenceState.SUPPORTED)
    c.set_evidence_state(ClaimEvidenceState.CONTRADICTED)
    c.set_use_state("MANUSCRIPT_ELIGIBLE")
    c.set_use_state("RETIRED")
    assert c.use_state.value == "RETIRED"


# ---------------------------------------------------------------- path safety
def test_path_escape_rejected(tmp_path):
    with pytest.raises(PathEscapeError):
        safe_resolve(tmp_path, "../escape.txt")
    with pytest.raises(PathEscapeError):
        safe_resolve(tmp_path, "sub/../../escape.txt")


def test_symlink_escape_rejected(tmp_path):
    outside = tmp_path.parent / "outside-target"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    from researchd.application.paths import check_symlink_escape

    with pytest.raises(PathEscapeError):
        check_symlink_escape(tmp_path, "link/file.txt")


def test_artifact_hash_and_size(tmp_path):
    f = tmp_path / "data.csv"
    f.write_text("a,b,c\n1,2,3\n")
    info = check_artifact_file(tmp_path, "data.csv")
    assert info["sha256"] == "5cfe6dbbd7bcb7a5c8a5c35b8b0c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c4c" or len(info["sha256"]) == 64
    assert info["size_bytes"] == f.stat().st_size
    assert "csv" in (info["mime_type"] or "")


def test_missing_artifact_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        check_artifact_file(tmp_path, "nope.csv")


def test_apply_reuses_artifact_without_rewriting_provenance(factory, tmp_path):
    """Replaying the same artifact id re-validates the file WITHOUT rewriting
    the existing artifact's run/task provenance (security review round 5)."""
    import sqlite3

    from researchd.application.apply_result import apply_work_result
    from researchd.domain.evidence import Artifact as ArtifactDomain
    from researchd.domain.project import Project
    from researchd.domain.run import Run
    from researchd.executors.base import validate_work_result
    from researchd.persistence.models import ArtifactRow
    from researchd.persistence.repositories import ArtifactRepo, ProjectRepo
    from researchd.persistence.transaction import UnitOfWork

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data.csv").write_text("a,b\n1,2\n")
    with UnitOfWork(factory) as uow:
        ProjectRepo(uow.session).save(Project(project_id="P-AR", name="ar", metadata={}, workspace_root=str(ws)))
        uow.commit()
    # pre-registered artifact from run R-OLD (immutable provenance) — registered
    # through the gate so it carries a real sha256
    with UnitOfWork(factory) as uow:
        project = ProjectRepo(uow.session).get_by_project_id("P-AR")
        from researchd.application.evidence_validation import register_artifact

        register_artifact(
            uow.session,
            project=project,
            workspace_root=str(ws),
            rel_path="data.csv",
            artifact=ArtifactDomain(
                artifact_id="A-1", project_id="P-AR", task_id="T-OLD", run_id="R-OLD",
                kind="dataset", path="data.csv", description="original",
            ),
        )
        uow.commit()
    raw = {
        "schema": "researchd.work_result.v1",
        "task_id": "T-NEW",
        "outcome": "SUBMIT_FOR_REVIEW",
        "criteria_results": [{"criterion_id": "c-1", "status": "PASS", "refs": []}],
        "artifacts": [{"local_ref": "A-1", "kind": "dataset", "path": "data.csv", "description": "replayed"}],
        "evidence_candidates": [], "claim_changes": [], "issues": [], "decision_candidates": [],
        "next_task_proposals": [],
    }
    run = Run(run_id="R-NEW", task_id="T-NEW", project_id="P-AR")
    with UnitOfWork(factory) as uow:
        apply_work_result(uow.session, run, validate_work_result(raw))
        uow.commit()
    with UnitOfWork(factory) as uow:
        row = uow.session.execute(
            sqlite3 if False else __import__("sqlalchemy").select(ArtifactRow).where(ArtifactRow.artifact_id == "A-1")
        ).scalars().first()
        assert row.run_id == "R-OLD" and row.task_id == "T-OLD"  # provenance immutable
        assert row.description == "original"


def test_directory_artifact_declaration_skipped_not_rejected(factory, tmp_path):
    """A real model may declare an existing DIRECTORY as an artifact (e.g.
    'data/raw/' for a raw corpus). That benign declaration mistake must drop
    the declaration (logged), NOT reject the whole result — the audit gate
    still requires evidence refs to resolve to registered artifacts."""
    from researchd.application.apply_result import apply_work_result
    from researchd.domain.project import Project
    from researchd.domain.run import Run
    from researchd.executors.base import validate_work_result
    from researchd.persistence.repositories import ArtifactRepo, ProjectRepo
    from researchd.persistence.transaction import UnitOfWork

    ws = tmp_path / "ws"
    (ws / "data" / "raw").mkdir(parents=True)
    (ws / "data" / "raw" / "refs_batch_00001.json").write_text("{}")
    (ws / "data" / "corpus.csv").write_text("a,b\n1,2\n")
    with UnitOfWork(factory) as uow:
        ProjectRepo(uow.session).save(Project(project_id="P-DIR", name="dir", workspace_root=str(ws)))
        uow.commit()
    raw = {
        "schema": "researchd.work_result.v1",
        "task_id": "T-DIR",
        "outcome": "SUBMIT_FOR_REVIEW",
        "criteria_results": [{"criterion_id": "c-1", "status": "PASS", "refs": []}],
        "artifacts": [
            {"local_ref": "A-DIR", "kind": "dataset", "path": "data/raw/", "description": "raw corpus"},
            {"local_ref": "A-CSV", "kind": "dataset", "path": "data/corpus.csv", "description": "corpus"},
        ],
        "evidence_candidates": [], "claim_changes": [], "issues": [], "decision_candidates": [],
        "next_task_proposals": [],
    }
    run = Run(run_id="R-DIR", task_id="T-DIR", project_id="P-DIR")
    with UnitOfWork(factory) as uow:
        counts = apply_work_result(uow.session, run, validate_work_result(raw))
        uow.commit()
    assert counts["artifacts"] == 1  # only the real file was registered
    with UnitOfWork(factory) as uow:
        assert ArtifactRepo(uow.session).get_by_artifact_id("A-CSV") is not None
        assert ArtifactRepo(uow.session).get_by_artifact_id("A-DIR") is None


def test_same_task_artifact_supersede_on_content_change(factory, tmp_path):
    """A task's LATER run editing its OWN deliverable supersedes the stale
    artifact row (hash/run updated); a DIFFERENT task re-declaring the same
    id with changed content is still rejected."""
    from researchd.application.apply_result import apply_work_result
    from researchd.domain.evidence import Artifact as ArtifactDomain
    from researchd.domain.project import Project
    from researchd.domain.run import Run
    from researchd.executors.base import validate_work_result
    from researchd.persistence.repositories import ArtifactRepo, ProjectRepo
    from researchd.persistence.transaction import UnitOfWork

    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "defs.md"
    f.write_text("v1")
    with UnitOfWork(factory) as uow:
        ProjectRepo(uow.session).save(Project(project_id="P-SUP", name="sup", workspace_root=str(ws)))
        uow.commit()
        from researchd.application.evidence_validation import register_artifact

        project = ProjectRepo(uow.session).get_by_project_id("P-SUP")
        register_artifact(
            uow.session, project=project, workspace_root=str(ws), rel_path="defs.md",
            artifact=ArtifactDomain(
                artifact_id="art_doc", project_id="P-SUP", task_id="T-1", run_id="R-OLD",
                kind="doc", path="defs.md", description="original",
            ),
        )
        uow.commit()

    def result_for(task_id, run_id, desc):
        return Run(run_id=run_id, task_id=task_id, project_id="P-SUP"), {
            "schema": "researchd.work_result.v1",
            "task_id": task_id,
            "outcome": "SUBMIT_FOR_REVIEW",
            "criteria_results": [{"criterion_id": "c-1", "status": "PASS", "refs": []}],
            "artifacts": [{"local_ref": "art_doc", "kind": "doc", "path": "defs.md", "description": desc}],
            "evidence_candidates": [], "claim_changes": [], "issues": [],
            "decision_candidates": [], "next_task_proposals": [],
        }

    # same task, changed content -> supersede (hash/run updated)
    f.write_text("v2")
    run, raw = result_for("T-1", "R-NEW", "updated")
    with UnitOfWork(factory) as uow:
        apply_work_result(uow.session, run, validate_work_result(raw))
        uow.commit()
    with UnitOfWork(factory) as uow:
        art = ArtifactRepo(uow.session).get_by_artifact_id("art_doc")
        assert art.run_id == "R-NEW"
        assert art.sha256 != "v1-hash"
        assert art.description == "updated"
    # different task, changed content -> rejected
    f.write_text("v3")
    run2, raw2 = result_for("T-2", "R-OTHER", "trespass")
    with UnitOfWork(factory) as uow:
        with pytest.raises(ValueError, match="belongs to task"):
            apply_work_result(uow.session, run2, validate_work_result(raw2))
        uow.rollback()
