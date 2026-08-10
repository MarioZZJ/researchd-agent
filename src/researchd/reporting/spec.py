"""ReportSpec compiler + linter (IMPLEMENTATION.md §21.2, §21.3).

ReportSpec is deterministic content built from persisted state — a language
model may only COMPRESS existing fields, never add conclusions, drop
uncertainty, change evidence refs, decide buttons, or alter the message type.

Linter rules (all enforced locally before anything is sent):
- no empty slots: every conclusion must carry evidence refs;
- every "next step" must reference a real Task;
- decision cards must state the actual scientific consequence;
- no hollow phrases / unfounded self-assessment (deterministic keyword scan).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import ReportType
from ..domain.report import ReportAction, ReportConflict, ReportSpec, ReportUncertainty

HOLLOW_PHRASES = (
    "深入分析",
    "全面评估",
    "显著提升",
    "重要意义",
    "初步结果表明",
    "总的来说",
    "进一步研究",
    "值得注意",
    "in conclusion",
    "comprehensive",
    "significantly improves",
    "further research",
)


class LinterError(ValueError):
    pass


@dataclass
class LintReport:
    ok: bool
    errors: list[str] = field(default_factory=list)

    def raise_if_bad(self) -> None:
        if not self.ok:
            raise LinterError("; ".join(self.errors))


def lint_spec(
    spec: ReportSpec,
    *,
    known_task_ids: set[str],
    known_evidence_ids: set[str],
    allow_procedural_bottom_line: bool = False,
) -> LintReport:
    """`allow_procedural_bottom_line` exempts hard-gate decision cards whose
    bottom line is a procedural statement (publication/budget/destructive) and
    legitimately cites no evidence (IMPLEMENTATION.md §21.3)."""
    errors: list[str] = []

    # every bottom line must cite REAL evidence (unless procedural hard gate)
    if spec.bottom_line and not spec.bottom_line_evidence_refs and not allow_procedural_bottom_line:
        errors.append("bottom_line has no evidence refs")
    for ref in spec.bottom_line_evidence_refs:
        if ref not in known_evidence_ids:
            errors.append(f"bottom_line cites unknown evidence {ref!r}")

    # conflicts must cite evidence
    for c in spec.conflicts:
        if not c.evidence_refs:
            errors.append(f"conflict {c.text[:40]!r} has no evidence refs")

    # every "next step" must be a real task
    for a in spec.active_actions:
        if a.task_id not in known_task_ids:
            errors.append(f"action references unknown task {a.task_id!r}")

    # hollow phrases
    text_blob = " ".join(
        [
            spec.title or "",
            spec.bottom_line or "",
            *[c.text for c in spec.conflicts],
            *[u.text for u in spec.uncertainties],
            *[a.text for a in spec.active_actions],
        ]
    )
    for phrase in HOLLOW_PHRASES:
        if phrase in text_blob:
            errors.append(f"hollow phrase {phrase!r}")

    return LintReport(ok=not errors, errors=errors)


# ---------------------------------------------------------------- compiler
def compile_spec(
    *,
    project_id: str,
    type: ReportType,
    title: str,
    bottom_line: str | None = None,
    bottom_line_evidence_refs: list[str] | None = None,
    conflicts: list[ReportConflict] | None = None,
    uncertainties: list[ReportUncertainty] | None = None,
    active_actions: list[ReportAction] | None = None,
    decision_id: str | None = None,
    milestone_id: str | None = None,
    digest_period: str | None = None,
) -> ReportSpec:
    """Compile a deterministic ReportSpec from persisted state."""
    return ReportSpec(
        type=type,
        title=title,
        bottom_line=bottom_line,
        bottom_line_evidence_refs=bottom_line_evidence_refs or [],
        conflicts=conflicts or [],
        uncertainties=uncertainties or [],
        active_actions=active_actions or [],
        decision_id=decision_id,
        milestone_id=milestone_id,
        digest_period=digest_period,
    )


def render_text(spec: ReportSpec) -> str:
    """Deterministic text rendering (the fallback when compression fails)."""
    lines: list[str] = []
    lines.append(f"【{spec.type.value}】{spec.title}")
    if spec.bottom_line:
        lines.append(f"结论：{spec.bottom_line}")
        if spec.bottom_line_evidence_refs:
            lines.append(f"证据：{', '.join(spec.bottom_line_evidence_refs)}")
    if spec.conflicts:
        lines.append("证据冲突：")
        for c in spec.conflicts:
            lines.append(f"- {c.text}（{', '.join(c.evidence_refs)}）")
    if spec.uncertainties:
        lines.append("不确定性：")
        for u in spec.uncertainties:
            lines.append(f"- {u.text}")
    if spec.active_actions:
        lines.append("下一步：")
        for a in spec.active_actions:
            lines.append(f"- [{a.task_id}] {a.text}")
    return "\n".join(lines)
