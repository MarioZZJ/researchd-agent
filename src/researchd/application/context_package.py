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
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _section(title: str, lines: list[str]) -> str:
    return f"## {title}\n" + ("\n".join(lines) if lines else "（无）")


class ContextPackageBuilder:
    """Builds + persists role-specific context packages."""

    def __init__(self, session: Session):
        self.session = session

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
        """Paths the model must never read/write: researchd data dir (db,
        socket, other projects' workspaces) and the global reasonix home."""
        forbidden = []
        root = getattr(project, "workspace_root", None)
        if root:
            from pathlib import Path

            data_dir = Path(root).resolve().parent.parent  # <data>/workspaces/<p> -> <data>
            forbidden.append(str(data_dir))
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
        return self._finalize(pkg, objects, content)

    # ------------------------------------------------------------ worker
    def worker(self, task, run=None) -> ContextPackage:  # noqa: ANN001
        project_id = task.project_id
        contract = task.contract
        objects = [
            ContextObjectRef(kind="task", id=task.task_id, summary=_clip(contract.objective)),
            *self._questions(project_id),
            *self._claims(project_id),
            *self._verified_evidence(project_id),
            *self._open_decisions(project_id),
            *self._artifacts(project_id),
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
                    "OPEN_DECISIONS",
                    [f"- {o.id}: {o.summary}" for o in self._open_decisions(project_id)],
                ),
                _section(
                    "RELATED_ARTIFACTS",
                    [f"- {o.id}: {o.summary}" for o in self._artifacts(project_id)],
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
        return self._finalize(pkg, objects, content)

    # ------------------------------------------------------------ auditor
    def auditor(self, task, run) -> ContextPackage:  # noqa: ANN001
        """INDEPENDENT audit package: built from persisted run result
        (structured evidence candidates + real artifacts), NOT from the
        worker's free-text self-assessment."""
        project_id = task.project_id
        result = run.result or {}
        candidates = result.get("evidence_candidates", [])
        artifacts = result.get("artifacts", [])
        objects = [
            ContextObjectRef(kind="task", id=task.task_id, summary=_clip(task.contract.objective)),
            ContextObjectRef(kind="run", id=run.run_id, summary=f"executor={run.executor} outcome={run.outcome}"),
            *[ContextObjectRef(kind="evidence_candidate", id=c.get("local_ref", "?"), summary=_clip(c.get("statement", ""))) for c in candidates],
            *[ContextObjectRef(kind="artifact", id=a.get("local_ref", "?"), summary=_clip(a.get("path", ""))) for a in artifacts],
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
        artifact_lines = []
        for a in artifacts:
            artifact_lines.append(f"- {a.get('local_ref')} path={a.get('path')} kind={a.get('kind')} {_clip(a.get('description', ''))}")
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
            run_id=run.run_id,
            created_by="auditor",
        )
        return self._finalize(pkg, objects, content)

    # ------------------------------------------------------------ persist
    def persist(self, pkg: ContextPackage) -> ContextPackage:
        ContextPackageRepo(self.session).save(pkg)
        self.session.flush()
        return pkg

    # ------------------------------------------------------------ wire
    def to_context_dict(self, pkg: ContextPackage, *, objective: str) -> dict:
        """Executor-facing context: objective + structured package."""
        return {
            "objective": objective,
            "context_id": pkg.context_id,
            "role": pkg.role,
            "task_id": pkg.task_id,
            "run_id": pkg.run_id,
            "package": {
                "objects": [o.model_dump() for o in pkg.objects],
                "content": pkg.content,
                "content_hash": pkg.content_hash,
                "token_estimate": pkg.token_estimate,
            },
        }
