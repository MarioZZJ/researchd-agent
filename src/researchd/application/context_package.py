"""ContextPackageBuilder (IMPLEMENTATION.md §13): bounded, traceable context
for Planner / Worker / Auditor turns.

Every package is built from PERSISTED state only, rendered to the exact text
the model will see, hashed (content_hash), and persisted (context_packages
row) so any model judgment can be traced back to the object refs + text it
actually received.

Role separation:
- planner package: initial brief, project charter, questions, approved
  decisions, claims/evidence summary, unresolved issues, workspace, budgets.
- worker package: full task contract (why_now/inputs/deliverables/success
  criteria/stop+escalation), related questions/claims/evidence/decisions,
  real artifact paths + hashes, workspace root, allowed tools, forbidden
  paths.
- auditor package: INDEPENDENT of the worker's self-assessment — the auditor
  sees the structured evidence candidates and real artifacts, never the
  worker's free-text criteria self-report.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ..domain.base import new_id, utcnow
from ..domain.context import ContextObjectRef, ContextPackage
from ..persistence.repositories import (
    ArtifactRepo,
    ClaimRepo,
    ContextPackageRepo,
    DecisionRepo,
    EvidenceRepo,
    IssueRepo,
    ProjectRepo,
    QuestionRepo,
    RunRepo,
    TaskRepo,
)

# object summary length caps keep packages bounded (token budget)
_SUMMARY_CAP = 300
_ARTIFACT_PATH_CAP = 200
MAX_OBJECTS = 200


def _clip(text: str, cap: int = _SUMMARY_CAP) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= cap else text[:cap] + "…"


def _token_estimate(text: str) -> int:
    """Rough token estimate (mixed zh/en): ~1 token per 3 characters."""
    return max(1, len(text) // 3)


def _hash(text: str) -> str:
    """Full SHA-256 (64 hex chars) of the exact rendered content."""
    return hashlib.sha256(text.encode()).hexdigest()


def _section(title: str, lines: list[str]) -> str:
    return f"## {title}\n" + ("\n".join(lines) if lines else "（无）")


class ContextPackageBuilder:
    """Builds + persists role-specific context packages.

    `data_dir` (from Settings, resolved) provides the canonical boundary for
    forbidden paths — never guessed from workspace_root parents.
    """

    def __init__(self, session: Session, *, data_dir: str | Path | None = None):
        self.session = session
        self.data_dir = Path(data_dir).resolve() if data_dir else None

    # ------------------------------------------------------------ helpers
    def _questions(self, project_id: str) -> list[ContextObjectRef]:
        refs = []
        for q in QuestionRepo(self.session).list_by_project(project_id)[:10]:
            refs.append(ContextObjectRef(kind="question", id=q.question_id, summary=_clip(q.text)))
        return refs

    def _approved_decisions(self, project_id: str) -> list[ContextObjectRef]:
        refs = []
        for d in DecisionRepo(self.session).list_all_statuses(project_id)[:10]:
            if d.status.value in ("ANSWERED", "APPLIED"):
                refs.append(
                    ContextObjectRef(
                        kind="decision",
                        id=d.decision_id,
                        summary=_clip(f"{d.question} -> {d.answer or ''}"),
                    )
                )
        return refs

    def _open_decisions(self, project_id: str) -> list[ContextObjectRef]:
        refs = []
        for d in DecisionRepo(self.session).list_all_statuses(project_id)[:10]:
            if d.status.value == "OPEN":
                refs.append(
                    ContextObjectRef(
                        kind="decision",
                        id=d.decision_id,
                        summary=_clip(f"{d.question}（等待 PI 决策 v{d.decision_version}）"),
                    )
                )
        return refs

    def _claims(self, project_id: str) -> list[ContextObjectRef]:
        refs = []
        for c in ClaimRepo(self.session).list_by_project(project_id)[:10]:
            refs.append(
                ContextObjectRef(
                    kind="claim",
                    id=c.claim_id,
                    summary=_clip(f"[{c.evidence_state.value}] {c.text}"),
                )
            )
        return refs

    def _verified_evidence(self, project_id: str) -> list[ContextObjectRef]:
        refs = []
        for e in EvidenceRepo(self.session).list_verified(project_id)[:20]:
            refs.append(
                ContextObjectRef(
                    kind="evidence",
                    id=e.evidence_id,
                    summary=_clip(f"[{e.type.value}] {e.statement}"),
                )
            )
        return refs

    def _issues(self, project_id: str) -> list[ContextObjectRef]:
        refs = []
        for i in IssueRepo(self.session).list_by_project(project_id)[:10]:
            if i.status.value in ("OPEN", "INVESTIGATING"):
                refs.append(
                    ContextObjectRef(kind="issue", id=i.issue_id, summary=_clip(f"[{i.severity}] {i.title}"))
                )
        return refs

    def _artifacts(self, project_id: str, task_id: str | None = None) -> list[ContextObjectRef]:
        refs = []
        for a in ArtifactRepo(self.session).list_by_project(project_id):
            if task_id and a.task_id != task_id:
                continue
            if len(refs) >= 20:
                break
            refs.append(
                ContextObjectRef(
                    kind="artifact",
                    id=a.artifact_id,
                    summary=_clip(
                        f"path={a.path} sha256={a.sha256 or '?'} size={a.size_bytes or '?'} "
                        f"task={a.task_id or '-'} {a.description}"
                    ),
                )
            )
        return refs

    def _forbidden_paths(self, project: Any) -> list[str]:
        """Paths the model must never read/write. The researchd data dir is
        taken from the injected Settings-derived boundary (never guessed via
        workspace_root.parent.parent); the global reasonix home is masked too.
        The project workspace root itself is allowed (it is the cwd)."""
        from pathlib import Path as _P

        forbidden: list[str] = []
        if self.data_dir is not None:
            forbidden.append(str(self.data_dir))
        forbidden.append(str(_P.home() / ".reasonix"))
        forbidden.append(str(_P.home() / ".cc-connect"))
        return forbidden

    # ------------------------------------------------------------ render
    @staticmethod
    def _finalize(
        pkg: ContextPackage,
        objects: list[ContextObjectRef],
        content: str,
    ) -> ContextPackage:
        pkg.objects = objects[:MAX_OBJECTS]
        pkg.content = content
        pkg.content_hash = _hash(content)
        pkg.token_estimate = _token_estimate(content)
        pkg.updated_at = utcnow()
        return pkg

    # ------------------------------------------------------------ planner
    def planner(self, project) -> ContextPackage:  # noqa: ANN001
        project_id = project.project_id
        objects = [
            ContextObjectRef(kind="project", id=project_id, summary=_clip(project.description)),
            *self._questions(project_id),
            *self._approved_decisions(project_id),
            *self._claims(project_id),
            *self._verified_evidence(project_id),
            *self._issues(project_id),
        ]
        charter = (
            f"project_id={project.project_id}\nname={project.name}\n"
            f"status={project.status.value}\nworkspace_root={project.workspace_root or '（未配置）'}"
        )
        budget = (
            f"default_budget={project.policy.default_budget or {}}\n"
            f"role_overrides={project.policy.role_overrides or {}}"
        )
        content = "\n\n".join(
            [
                _section("INITIAL_BRIEF", [_clip(project.description or "（无）")]),
                _section("PROJECT_CHARTER", charter.splitlines()),
                _section(
                    "QUESTIONS",
                    [f"- {o.id}: {o.summary}" for o in self._questions(project_id)],
                ),
                _section(
                    "APPROVED_DECISIONS",
                    [f"- {o.id}: {o.summary}" for o in self._approved_decisions(project_id)],
                ),
                _section("CLAIMS", [f"- {o.id}: {o.summary}" for o in self._claims(project_id)]),
                _section(
                    "VERIFIED_EVIDENCE",
                    [f"- {o.id}: {o.summary}" for o in self._verified_evidence(project_id)],
                ),
                _section("UNRESOLVED_ISSUES", [f"- {o.id}: {o.summary}" for o in self._issues(project_id)]),
                _section("WORKSPACE", [project.workspace_root or "（未配置）"]),
                _section("BUDGET_AND_PERMISSIONS", budget.splitlines()),
                _section(
                    "FORBIDDEN_PATHS",
                    self._forbidden_paths(project) or ["（无额外限制）"],
                ),
            ]
        )
        pkg = ContextPackage(
            context_id=new_id("context_package"),
            role="planner",
            project_id=project_id,
            created_by="planner",
        )
        if project.workspace_root:
            pkg.metadata["workspace_root"] = project.workspace_root
        return self._finalize(pkg, objects, content)

    # ------------------------------------------------------------ worker
    def worker(self, task, run=None) -> ContextPackage:  # noqa: ANN001
        project_id = task.project_id
        contract = task.contract
        # task-scoped artifacts (the task's OWN outputs) — never the whole
        # project's artifact set
        artifacts = self._artifacts(project_id, task_id=task.task_id)
        objects = [
            ContextObjectRef(kind="task", id=task.task_id, summary=_clip(contract.objective)),
            *self._questions(project_id),
            *self._claims(project_id),
            *self._verified_evidence(project_id),
            *self._approved_decisions(project_id),
            *self._open_decisions(project_id),
            *artifacts,
        ]
        project = ProjectRepo(self.session).get_by_project_id(project_id)
        contract_lines = [
            f"task_id={task.task_id}",
            f"status={task.status.value}",
            f"role={contract.role.value if hasattr(contract.role, 'value') else contract.role}",
            f"objective={contract.objective}",
            f"why_now={contract.why_now or '（无）'}",
            f"inputs={contract.inputs or []}",
            f"deliverables={contract.deliverables or []}",
            "success_criteria:",
            *[f"  - {c.id}: {c.text}" for c in contract.success_criteria],
            f"stop_conditions={contract.stop_conditions or []}",
            f"escalation_conditions={contract.escalation_conditions or []}",
            f"budget={contract.budget.model_dump() if hasattr(contract.budget, 'model_dump') else contract.budget}",
            f"executor_profile={contract.executor_profile or '（由调度器解析）'}",
        ]
        content = "\n\n".join(
            [
                _section("TASK_CONTRACT", contract_lines),
                _section("QUESTIONS", [f"- {o.id}: {o.summary}" for o in self._questions(project_id)]),
                _section("CLAIMS", [f"- {o.id}: {o.summary}" for o in self._claims(project_id)]),
                _section(
                    "VERIFIED_EVIDENCE",
                    [f"- {o.id}: {o.summary}" for o in self._verified_evidence(project_id)],
                ),
                _section(
                    "APPROVED_DECISIONS",
                    [f"- {o.id}: {o.summary}" for o in self._approved_decisions(project_id)],
                ),
                _section(
                    "OPEN_DECISIONS",
                    [f"- {o.id}: {o.summary}" for o in self._open_decisions(project_id)],
                ),
                _section(
                    "RELATED_ARTIFACTS",
                    [f"- {o.id}: {o.summary}" for o in artifacts],
                ),
                _section("WORKSPACE_ROOT", [project.workspace_root or "（未配置）"] if project else ["（未配置）"]),
                _section(
                    "TOOLS_AND_PERMISSIONS",
                    [
                        "你可以在项目 workspace 内创建/读取/修改文件并运行只读分析；",
                        "所有产出的路径必须以 workspace 相对路径声明；",
                        "不得读取或写入禁止路径（见下）。",
                    ],
                ),
                _section(
                    "FORBIDDEN_PATHS",
                    self._forbidden_paths(project) if project else ["（未配置 workspace）"],
                ),
            ]
        )
        pkg = ContextPackage(
            context_id=new_id("context_package"),
            role="worker",
            project_id=project_id,
            task_id=task.task_id,
            run_id=run.run_id if run else None,
            created_by="scheduler",
        )
        if project is not None and project.workspace_root:
            pkg.metadata["workspace_root"] = project.workspace_root
        pkg.metadata["budget"] = contract.budget.model_dump() if hasattr(contract.budget, "model_dump") else str(contract.budget)
        pkg.metadata["excluded_by_budget"] = list(pkg.excluded_by_budget or [])
        return self._finalize(pkg, objects, content)

    # ------------------------------------------------------------ auditor
    def auditor(self, task, worker_run, *, audit_run_id: str | None = None) -> ContextPackage:  # noqa: ANN001
        """INDEPENDENT audit package: built from the WORKER run's persisted
        structured result (evidence candidates + real artifacts), NOT from the
        worker's free-text self-assessment. `worker_run` is the run under
        review; `audit_run_id` (the audit run) is recorded for traceability."""
        project_id = task.project_id
        run = worker_run
        result = run.result or {}
        candidates = result.get("evidence_candidates", [])
        declared_artifacts = result.get("artifacts", [])
        # RE-QUERY the registry: the auditor sees the REAL persisted artifacts
        # of the worker run (full sha256, size, version, run) — the worker's
        # declared list is only used to flag missing registrations.
        registered = ArtifactRepo(self.session).list_by_run(run.run_id)
        declared_paths = {str(a.get("path", "")).lstrip("./") for a in declared_artifacts}
        registered_paths = {a.path.lstrip("./") for a in registered}
        missing = sorted(declared_paths - registered_paths)
        objects = [
            ContextObjectRef(kind="task", id=task.task_id, summary=_clip(task.contract.objective)),
            ContextObjectRef(kind="run", id=run.run_id, summary=f"executor={run.executor} outcome={run.outcome}"),
            *[ContextObjectRef(kind="evidence_candidate", id=c.get("local_ref", "?"), summary=_clip(c.get("statement", ""))) for c in candidates],
            *[
                ContextObjectRef(
                    kind="artifact",
                    id=a.artifact_id,
                    summary=_clip(
                        f"path={a.path} sha256={a.sha256 or '?'} size={a.size_bytes or '?'} "
                        f"v{a.version or 1} run={a.run_id or '-'}"
                    ),
                )
                for a in registered
            ],
            *self._verified_evidence(project_id),
            *self._claims(project_id),
        ]
        project = ProjectRepo(self.session).get_by_project_id(project_id)
        cand_lines = []
        for c in candidates:
            cand_lines.append(
                f"- {c.get('local_ref')} type={c.get('type')} refs={c.get('artifact_refs', [])} "
                f"limitations={c.get('limitations', [])} provenance={c.get('literature') or c.get('computational') or 'none'}"
            )
            cand_lines.append(f"    statement: {_clip(c.get('statement', ''))}")
        # real registry rows (not the worker's claim): relative path + FULL
        # sha256 + size + version + owning run
        artifact_lines = [
            f"- {a.artifact_id} path={a.path} sha256={a.sha256 or '?'} "
            f"size={a.size_bytes or '?'} v{a.version or 1} run={a.run_id or '-'} kind={a.kind}"
            for a in registered
        ]
        if missing:
            artifact_lines.append(f"MISSING_FROM_REGISTRY: {missing}")
        contract = task.contract
        content = "\n\n".join(
            [
                _section(
                    "TASK_CONTRACT",
                    [
                        f"task_id={task.task_id}",
                        f"objective={contract.objective}",
                        f"deliverables={contract.deliverables or []}",
                        "success_criteria:",
                        *[f"  - {c.id}: {c.text}" for c in contract.success_criteria],
                    ],
                ),
                _section("RUN_FACTS", [f"run_id={run.run_id}", f"executor={run.executor}", f"outcome={run.outcome or ''}"]),
                _section("EVIDENCE_CANDIDATES", cand_lines or ["（无候选）"]),
                _section("DECLARED_ARTIFACTS", artifact_lines or ["（无产物）"]),
                _section(
                    "BACKGROUND_VERIFIED_EVIDENCE",
                    [f"- {o.id}: {o.summary}" for o in self._verified_evidence(project_id)],
                ),
                _section("BACKGROUND_CLAIMS", [f"- {o.id}: {o.summary}" for o in self._claims(project_id)]),
                _section(
                    "AUDIT_RULES",
                    [
                        "独立审计：不要采信 worker 的自评；只依据产物与候选证据本身；",
                        "每个 evidence candidate 必须给出 PASS/FAIL 与理由（checks）；",
                        "verdict：ACCEPT（全部通过）| REVISE（可修复问题）| BLOCK（需决策）| REJECT（不可接受）；",
                        "artifact 路径必须是 workspace 相对路径且真实存在。",
                    ],
                ),
                _section(
                    "FORBIDDEN_PATHS",
                    self._forbidden_paths(project) if project else ["（未配置 workspace）"],
                ),
            ]
        )
        pkg = ContextPackage(
            context_id=new_id("context_package"),
            role="auditor",
            project_id=project_id,
            task_id=task.task_id,
            run_id=audit_run_id or run.run_id,
            created_by="auditor",
        )
        if project is not None and project.workspace_root:
            pkg.metadata["workspace_root"] = project.workspace_root
        return self._finalize(pkg, objects, content)

    # ------------------------------------------------------------ persist
    def persist(self, pkg: ContextPackage) -> ContextPackage:
        ContextPackageRepo(self.session).save(pkg)
        self.session.flush()
        return pkg

    # ------------------------------------------------------------ wire
    def to_context_dict(self, pkg: ContextPackage, *, objective: str) -> dict:
        """Executor-facing context: objective + structured package + the run's
        workspace root (the executor confines its subprocess cwd to it)."""
        return {
            "objective": objective,
            "context_id": pkg.context_id,
            "role": pkg.role,
            "task_id": pkg.task_id,
            "run_id": pkg.run_id,
            "workspace_root": pkg.metadata.get("workspace_root"),
            "package": {
                "objects": [o.model_dump() for o in pkg.objects],
                "content": pkg.content,
                "content_hash": pkg.content_hash,
                "token_estimate": pkg.token_estimate,
            },
        }
