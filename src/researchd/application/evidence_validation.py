"""Application-level validation: the single gate for VERIFIED evidence and
artifact registration (IMPLEMENTATION.md §7.3, §12.2, §25.4).

Domain-level `Evidence.verify()` enforces structure; these functions enforce
EXISTENCE against the database — a computational evidence can only be VERIFIED
when its Run and Artifact rows exist and are consistent.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from ..domain.enums import EvidenceType
from ..domain.evidence import Artifact, Evidence
from ..persistence.repositories import ArtifactRepo, EvidenceRepo, RunRepo
from .paths import PathEscapeError, check_artifact_file, check_symlink_escape, safe_resolve


class EvidenceValidationError(ValueError):
    pass


def verify_evidence(session: Session, evidence: Evidence) -> Evidence:
    """Verify evidence against real persisted provenance. Raises on failure."""
    if not evidence.project_id:
        raise EvidenceValidationError(f"evidence {evidence.evidence_id}: project_id is required for VERIFIED")
    if not evidence.provenance_ok():
        raise EvidenceValidationError(
            f"evidence {evidence.evidence_id} lacks required provenance for type {evidence.type}"
        )
    if evidence.type == EvidenceType.COMPUTATIONAL:
        run = RunRepo(session).get_by_run_id(evidence.run_id) if evidence.run_id else None
        if run is None:
            raise EvidenceValidationError(f"evidence {evidence.evidence_id}: run {evidence.run_id!r} does not exist")
        if run.project_id != evidence.project_id:
            raise EvidenceValidationError(
                f"evidence {evidence.evidence_id}: run {run.run_id} belongs to project "
                f"{run.project_id}, not {evidence.project_id}"
            )
        artifact = ArtifactRepo(session).get_by_artifact_id(evidence.computational.artifact_id)
        if artifact is None:
            raise EvidenceValidationError(
                f"evidence {evidence.evidence_id}: artifact {evidence.computational.artifact_id!r} does not exist"
            )
        if artifact.project_id != evidence.project_id:
            raise EvidenceValidationError(
                f"evidence {evidence.evidence_id}: artifact {artifact.artifact_id} belongs to project "
                f"{artifact.project_id}, not {evidence.project_id}"
            )
        if artifact.run_id != run.run_id:
            raise EvidenceValidationError(
                f"evidence {evidence.evidence_id}: artifact {artifact.artifact_id} was produced by "
                f"run {artifact.run_id}, not {run.run_id}"
            )
        for ref in evidence.artifact_refs:
            # refs may be artifact ids or local_refs; at least one must resolve
            if ArtifactRepo(session).get_by_artifact_id(ref) is not None:
                break
        else:
            raise EvidenceValidationError(
                f"evidence {evidence.evidence_id}: no artifact_ref resolves to a real artifact: {evidence.artifact_refs}"
            )
    if evidence.type == EvidenceType.LITERATURE and evidence.literature and evidence.literature.snapshot_artifact_id:
        snap = ArtifactRepo(session).get_by_artifact_id(evidence.literature.snapshot_artifact_id)
        if snap is None:
            raise EvidenceValidationError(
                f"evidence {evidence.evidence_id}: snapshot artifact "
                f"{evidence.literature.snapshot_artifact_id!r} does not exist"
            )
        if snap.project_id != evidence.project_id:
            raise EvidenceValidationError(
                f"evidence {evidence.evidence_id}: snapshot artifact belongs to project "
                f"{snap.project_id}, not {evidence.project_id}"
            )
    return evidence.verify()


def register_artifact(
    session: Session,
    *,
    project: object,
    workspace_root: str | None,
    rel_path: str,
    artifact: Artifact,
    max_bytes: int = 2 * 1024**3,
) -> Artifact:
    """Single registration entry point: path boundary + symlink + real file + hash.

    The workspace root is DERIVED from the persisted Project (never trusted from
    the caller alone); the caller's workspace_root, when given, is only checked
    for consistency against the project.
    """
    if project is None:
        raise EvidenceValidationError("project is required for artifact registration")
    project_id = getattr(project, "project_id", None)
    if project_id and artifact.project_id and artifact.project_id != project_id:
        raise EvidenceValidationError(
            f"artifact {artifact.artifact_id} belongs to project {artifact.project_id}, not {project_id}"
        )
    if project_id and not artifact.project_id:
        artifact.project_id = project_id
    project_root = getattr(project, "workspace_root", None)
    if not project_root:
        raise EvidenceValidationError(f"project {project_id} has no workspace_root configured")
    if workspace_root is not None:
        if str(Path(workspace_root).resolve()) != str(Path(project_root).resolve()):
            raise EvidenceValidationError(
                f"workspace_root mismatch: caller passed {workspace_root}, project says {project_root}"
            )
    root = project_root
    # absolute paths are rejected: everything is relative to the workspace root
    if rel_path.startswith("/") or "\\" in rel_path:
        raise EvidenceValidationError(f"artifact path must be relative: {rel_path!r}")
    try:
        check_symlink_escape(root, rel_path)
        safe_resolve(root, rel_path)  # boundary check
    except PathEscapeError as exc:
        raise EvidenceValidationError(str(exc)) from exc
    info = check_artifact_file(root, rel_path, max_bytes=max_bytes)
    artifact.path = info["path"]
    artifact.sha256 = info["sha256"]
    artifact.size_bytes = info["size_bytes"]
    artifact.mime_type = info["mime_type"]
    return ArtifactRepo(session).save(artifact)
