"""Regression tests for review findings: completion bypass, provenance
consistency, outbox claim concurrency, artifact registration gate."""

import asyncio

import pytest

from researchd.application.evidence_validation import (
    EvidenceValidationError,
    register_artifact,
    verify_evidence,
)
from researchd.domain.enums import EvidenceType, OutboxStatus
from researchd.domain.evidence import Artifact, ComputationalProvenance, Evidence
from researchd.domain.run import Run
from researchd.domain.task import SuccessCriterion, Task, TaskContract
from researchd.persistence.outbox import OutboxRepo
from researchd.persistence.repositories import ArtifactRepo, EventRepo, RunRepo, TaskRepo
from researchd.persistence.transaction import UnitOfWork, init_db, make_engine, make_session_factory


@pytest.fixture()
def factory(tmp_path):
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    yield make_session_factory(engine)
    engine.dispose()


def make_task() -> Task:
    return Task(
        task_id="T-001",
        project_id="P-TEST",
        contract=TaskContract(
            task_id="T-001",
            role="analysis_worker",
            objective="o",
            success_criteria=[SuccessCriterion(id="SC-1", text="c")],
        ),
    )


def test_complete_requires_explicit_criteria(factory):
    from researchd.domain.state_machine import InvalidTransition

    with UnitOfWork(factory) as uow:
        t = make_task()
        t.propose_ready()
        t.start(run_id="R-1", lease_token="L-1")
        t.submit_review()
        with pytest.raises(InvalidTransition):
            t.complete(None)  # completing without criteria evidence is refused
        uow.rollback()


def test_evidence_verify_requires_real_run_and_artifact(factory):
    with UnitOfWork(factory) as uow:
        ev = Evidence(
            evidence_id="E-001",
            project_id="P-TEST",
            type=EvidenceType.COMPUTATIONAL,
            statement="s",
            run_id="R-GHOST",
            artifact_refs=["A-GHOST"],
            computational=ComputationalProvenance(run_id="R-GHOST", artifact_id="A-GHOST"),
        )
        with pytest.raises(EvidenceValidationError):
            verify_evidence(uow.session, ev)
        uow.rollback()

    # with real run + artifact rows it passes
    with UnitOfWork(factory) as uow:
        RunRepo(uow.session).save(Run(run_id="R-1", task_id="T-001", project_id="P-TEST"))
        ArtifactRepo(uow.session).save(
            Artifact(
                artifact_id="A-1", project_id="P-TEST", run_id="R-1",
                path="data/x.csv", sha256="x" * 64, size_bytes=1,
            )
        )
        uow.session.flush()  # rows must be visible to the same-session verification queries
        ev = Evidence(
            evidence_id="E-001",
            project_id="P-TEST",
            type=EvidenceType.COMPUTATIONAL,
            statement="s",
            run_id="R-1",
            artifact_refs=["A-1"],
            computational=ComputationalProvenance(run_id="R-1", artifact_id="A-1"),
        )
        verify_evidence(uow.session, ev)
        assert ev.status.value == "VERIFIED"
        uow.commit()


def test_evidence_inconsistent_refs_rejected(factory):
    with UnitOfWork(factory) as uow:
        ev = Evidence(
            evidence_id="E-002",
            project_id="P-TEST",
            type=EvidenceType.COMPUTATIONAL,
            statement="s",
            run_id="R-1",
            artifact_refs=["A-1"],
            computational=ComputationalProvenance(run_id="R-OTHER", artifact_id="A-1"),
        )
        assert ev.provenance_ok() is False  # run_id mismatch between rows
        with pytest.raises(EvidenceValidationError):
            verify_evidence(uow.session, ev)
        uow.rollback()


def test_artifact_registration_gate(factory, tmp_path):
    from researchd.domain.project import Project

    p = Project(project_id="P-1", name="p", workspace_root=str(tmp_path))
    real = tmp_path / "real.csv"
    real.write_text("a,b\n1,2\n")
    with UnitOfWork(factory) as uow:
        art = Artifact(artifact_id="A-1", project_id="P-1", path="real.csv")
        register_artifact(uow.session, project=p, workspace_root=str(tmp_path), rel_path="real.csv", artifact=art)
        assert art.sha256 and len(art.sha256) == 64
        assert art.size_bytes == real.stat().st_size
        # absolute path rejected
        from researchd.application.evidence_validation import EvidenceValidationError as EVE

        with pytest.raises(EVE):
            register_artifact(uow.session, project=p, workspace_root=str(tmp_path), rel_path=str(real), artifact=art)
        # escape rejected
        with pytest.raises(EVE):
            register_artifact(uow.session, project=p, workspace_root=str(tmp_path), rel_path="../evil.txt", artifact=art)
        # no project -> rejected
        with pytest.raises(EVE):
            register_artifact(uow.session, project=None, workspace_root=str(tmp_path), rel_path="real.csv", artifact=art)
        uow.rollback()


def test_outbox_claim_is_exclusive(factory):
    with UnitOfWork(factory) as uow:
        OutboxRepo(uow.session).enqueue(destination="delivery", idempotency_key="k-1", payload={})
        uow.commit()
    with UnitOfWork(factory) as uow:
        repo = OutboxRepo(uow.session)
        row = repo.pending()[0]
        assert repo.claim(row.id, attempts=0) is True
        uow.commit()
    # a second sender cannot see or claim the in-flight row
    with UnitOfWork(factory) as uow:
        repo = OutboxRepo(uow.session)
        assert repo.pending() == []
        row = repo.get_by_idempotency_key("k-1")
        assert row.status == OutboxStatus.IN_FLIGHT.value
        assert repo.claim(row.id, attempts=1) is False
        uow.rollback()
    # after release it is retryable
    with UnitOfWork(factory) as uow:
        repo = OutboxRepo(uow.session)
        row = repo.get_by_idempotency_key("k-1")
        assert repo.release(row.id, attempts=1) is True
        uow.commit()
    with UnitOfWork(factory) as uow:
        assert len(OutboxRepo(uow.session).pending()) == 1


def test_version_increment_after_review_fix(factory):
    """Optimistic update path keeps version monotonic through a normal cycle."""
    with UnitOfWork(factory) as uow:
        TaskRepo(uow.session).save(make_task())
        uow.commit()
    with UnitOfWork(factory) as uow:
        repo = TaskRepo(uow.session)
        t = repo.get("T-001")
        assert t.version == 1
        t.status = "READY"
        repo.save(t)
        uow.commit()
    with UnitOfWork(factory) as uow:
        assert TaskRepo(uow.session).get("T-001").version == 2


# ---------------------------------------------------------------- bypass paths
def test_raw_transition_to_completed_rejected():
    """REVIEW -> COMPLETED via raw transition() is impossible; only complete() works."""
    from researchd.domain.enums import TaskStatus
    from researchd.domain.state_machine import InvalidTransition

    t = make_task()
    t.propose_ready()
    t.start(run_id="R-1", lease_token="L-1")
    t.submit_review()
    with pytest.raises(InvalidTransition):
        t.transition(TaskStatus.COMPLETED)
    assert t.status.value == "REVIEW"
    t.complete(criteria_results=[{"criterion_id": "SC-1", "status": "PASS"}])
    assert t.status.value == "COMPLETED"


def test_raw_transition_to_verified_rejected():
    """Evidence.transition(VERIFIED) is impossible; only verify() works."""
    from researchd.domain.enums import EvidenceType
    from researchd.domain.evidence import Evidence

    ev = Evidence(
        evidence_id="E-X", project_id="P-TEST", type=EvidenceType.LITERATURE,
        statement="s", literature={"source_id": "doi:1"},
    )
    with pytest.raises(ValueError):
        ev.transition("VERIFIED")
    assert ev.status.value == "CANDIDATE"


def test_human_evidence_requires_real_human():
    from researchd.domain.enums import EvidenceType
    from researchd.domain.evidence import Evidence

    ev = Evidence(evidence_id="E-H", project_id="P-TEST", type=EvidenceType.HUMAN, statement="s")
    assert ev.created_by == "system"  # default
    assert ev.provenance_ok() is False
    with pytest.raises(ValueError):
        ev.verify()
    ev.created_by = "ou_123"
    assert ev.provenance_ok() is True


def test_cross_project_provenance_rejected(factory):
    """Evidence of project P1 cannot cite runs/artifacts of project P2."""
    with UnitOfWork(factory) as uow:
        RunRepo(uow.session).save(Run(run_id="R-P2", task_id="T-001", project_id="P-OTHER"))
        ArtifactRepo(uow.session).save(
            Artifact(artifact_id="A-P2", project_id="P-OTHER", run_id="R-P2", path="x.csv", sha256="x" * 64, size_bytes=1)
        )
        uow.session.flush()
        ev = Evidence(
            evidence_id="E-1", project_id="P-TEST", type=EvidenceType.COMPUTATIONAL,
            statement="s", run_id="R-P2", artifact_refs=["A-P2"],
            computational={"run_id": "R-P2", "artifact_id": "A-P2"},
        )
        with pytest.raises(EvidenceValidationError):
            verify_evidence(uow.session, ev)
        uow.rollback()


# ---------------------------------------------------------------- outbox lease
def test_expired_in_flight_is_reclaimed(factory):
    from datetime import timedelta

    with UnitOfWork(factory) as uow:
        OutboxRepo(uow.session).enqueue(destination="delivery", idempotency_key="k-lease", payload={})
        uow.commit()
    with UnitOfWork(factory) as uow:
        repo = OutboxRepo(uow.session)
        row = repo.pending()[0]
        assert repo.claim(row.id, attempts=0, lease_seconds=1) is True
        uow.commit()
    # lease expires -> a new sender may reclaim
    with UnitOfWork(factory) as uow:
        repo = OutboxRepo(uow.session)
        assert repo.pending() == []  # lease not yet expired
        row = repo.get_by_idempotency_key("k-lease")
        row.next_attempt_at = row.next_attempt_at - timedelta(seconds=5)  # force expiry
        uow.commit()
    with UnitOfWork(factory) as uow:
        repo = OutboxRepo(uow.session)
        rows = repo.pending()
        assert len(rows) == 1
        assert repo.claim(rows[0].id, attempts=1) is True
        uow.commit()


def test_late_release_after_reclaim_is_noop(factory):
    """A worker that lost its claim cannot release/mark another claimant's row."""
    with UnitOfWork(factory) as uow:
        OutboxRepo(uow.session).enqueue(destination="delivery", idempotency_key="k-race", payload={})
        uow.commit()
    with UnitOfWork(factory) as uow:
        repo = OutboxRepo(uow.session)
        row = repo.pending()[0]
        repo.claim(row.id, attempts=0)
        uow.commit()
    # second claimant takes over after the first "died" (attempts now 1)
    with UnitOfWork(factory) as uow:
        repo = OutboxRepo(uow.session)
        row = repo.get_by_idempotency_key("k-race")
        assert repo.claim(row.id, attempts=1) is False  # not yet expired -> cannot reclaim
        uow.rollback()
    # the original claimant completes with attempts=1
    with UnitOfWork(factory) as uow:
        repo = OutboxRepo(uow.session)
        row = repo.get_by_idempotency_key("k-race")
        assert repo.mark_sent(row.id, attempts=1) is True
        uow.commit()
    # a stale release (attempts=1) after SENT is a no-op
    with UnitOfWork(factory) as uow:
        repo = OutboxRepo(uow.session)
        row = repo.get_by_idempotency_key("k-race")
        assert row.status == "SENT"
        assert repo.release(row.id, attempts=1) is False
        uow.rollback()


def test_workspace_root_mismatch_rejected(factory, tmp_path):
    from researchd.domain.project import Project

    p = Project(project_id="P-1", name="p", workspace_root=str(tmp_path))
    other = tmp_path.parent / "other-root"
    other.mkdir(exist_ok=True)
    (tmp_path / "f.txt").write_text("data")
    art = Artifact(artifact_id="A-1", project_id="P-1", path="f.txt")
    with UnitOfWork(factory) as uow:
        with pytest.raises(EvidenceValidationError):
            register_artifact(
                uow.session, project=p, workspace_root=str(other), rel_path="f.txt", artifact=art
            )
        uow.rollback()
