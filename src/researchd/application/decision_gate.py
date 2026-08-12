"""Decision Gate (IMPLEMENTATION.md §8, §25.5).

Ask PI = Material AND Unresolved AND (TasteSensitive OR HardGate)

- Material: the choice affects the core question, method, conclusions,
  publication, budget, or permissions.
- Unresolved: the existing evidence cannot decide it (numerical-only
  differences and cheap-parallel options do NOT qualify).
- TasteSensitive: style/narrative/trade-off judgment the PI owns.
- HardGate: publication, budget/permission, destructive, or external
  release (always goes to the PI, §22).

Engineering errors are auto-resolved; cheap parallel options run in parallel;
only genuine scientific forks produce OPEN decisions. The fingerprint
deduplicates the same question regardless of wording.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from ..domain.decision import Decision, DecisionOption
from ..domain.enums import DecisionCategory

HARD_GATE_CATEGORIES = {
    DecisionCategory.CHARTER_CHANGE,
    DecisionCategory.CORE_QUESTION,
    DecisionCategory.INCLUSION_CRITERIA,
    DecisionCategory.TITLE_ABSTRACT,
    DecisionCategory.PUBLICATION,
    DecisionCategory.BUDGET_PERMISSION,
    DecisionCategory.DESTRUCTIVE,
}

MATERIAL_CATEGORIES = {
    DecisionCategory.CHARTER_CHANGE,
    DecisionCategory.CORE_QUESTION,
    DecisionCategory.INCLUSION_CRITERIA,
    DecisionCategory.ANALYSIS_STRATEGY,
    DecisionCategory.NARRATIVE,
    DecisionCategory.TITLE_ABSTRACT,
    DecisionCategory.PUBLICATION,
    DecisionCategory.BUDGET_PERMISSION,
    DecisionCategory.DESTRUCTIVE,
}


@dataclass
class GateVerdict:
    action: str  # ask_pi | resolve_automatically | run_parallel | resolve_numerically | duplicate
    decision_id: str | None = None
    reason: str = ""
    fingerprint: str | None = None
    blocking_scope: list[str] = field(default_factory=list)
    continue_scope: list[str] = field(default_factory=list)


def decision_fingerprint(
    *,
    project_id: str,
    category: DecisionCategory | str,
    affected_object: str | None,
    question: str,
    options: list[DecisionOption],
) -> str:
    """Stable fingerprint: same question asked twice (any wording) dedupes on
    (project, category, affected_object, normalized question, option ids)."""
    normalized_q = " ".join(question.strip().lower().split())
    option_ids = sorted(o.option_id for o in options)
    payload = json.dumps(
        [project_id, str(category), affected_object or "", normalized_q, option_ids],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


class DecisionGate:
    """Pure evaluation: candidate -> verdict. The scheduler persists the
    resulting OPEN decisions and blocks only the dependent scope."""

    def __init__(self, *, existing_fingerprints: set[str] | None = None):
        self.existing_fingerprints = existing_fingerprints or set()

    # ------------------------------------------------------------ predicates
    @staticmethod
    def is_material(category: DecisionCategory | str, why_material: str, *, has_option_conflict: bool) -> bool:
        try:
            cat = DecisionCategory(category)
        except ValueError:
            cat = DecisionCategory.OTHER
        if cat in MATERIAL_CATEGORIES:
            return True
        # OTHER categories are only material with an explicit reason
        return bool(why_material.strip() and has_option_conflict)

    @staticmethod
    def is_unresolved(*, evidence_refs: list[str], unresolved_uncertainty: str | None, option_conflict: bool) -> bool:
        """Unresolved = real scientific uncertainty (not numerical noise)."""
        if not option_conflict:
            return False  # only one viable option or no conflict -> not a fork
        if not evidence_refs and not unresolved_uncertainty:
            return False  # no basis to ask: nothing to weigh
        return True

    @staticmethod
    def is_taste_sensitive(category: DecisionCategory | str) -> bool:
        try:
            cat = DecisionCategory(category)
        except ValueError:
            return False
        return cat in (DecisionCategory.NARRATIVE, DecisionCategory.TITLE_ABSTRACT, DecisionCategory.OTHER)

    @staticmethod
    def is_hard_gate(category: DecisionCategory | str) -> bool:
        try:
            cat = DecisionCategory(category)
        except ValueError:
            return False
        return cat in HARD_GATE_CATEGORIES

    # ------------------------------------------------------------ evaluation
    def evaluate(
        self,
        *,
        project_id: str,
        category: DecisionCategory | str,
        question: str,
        why_material: str,
        options: list[DecisionOption],
        affected_object: str | None = None,
        trigger: str = "",
        recommendation: str | None = None,
        recommendation_basis: str | None = None,
        evidence_refs: list[str] | None = None,
        unresolved_uncertainty: str | None = None,
        reversibility: str | None = None,
        blocking_scope: list[str] | None = None,
        continue_scope: list[str] | None = None,
        has_option_conflict: bool = True,
        numerical_only: bool = False,
        hard_gate_override: bool = False,
    ) -> GateVerdict:
        fp = decision_fingerprint(
            project_id=project_id,
            category=category,
            affected_object=affected_object,
            question=question,
            options=options,
        )
        if fp in self.existing_fingerprints:
            return GateVerdict(action="duplicate", fingerprint=fp, reason="same question already asked")
        self.existing_fingerprints.add(fp)

        try:
            category_enum = DecisionCategory(category) if isinstance(category, str) else category
        except ValueError:
            # unknown categories are treated as OTHER (never crash a tick)
            category_enum = DecisionCategory.OTHER

        # numerical-only differences never ask, whatever the evidence says
        if numerical_only:
            return GateVerdict(
                action="resolve_numerically",
                fingerprint=fp,
                reason="numerical-only difference",
            )

        # HardGate always asks the PI (publication/budget/destructive/external)
        if self.is_hard_gate(category_enum) or hard_gate_override:
            return GateVerdict(
                action="ask_pi",
                fingerprint=fp,
                reason="hard gate (publication/budget/destructive/external)",
                blocking_scope=blocking_scope or [],
                continue_scope=continue_scope or [],
            )

        # engineering problems resolve automatically
        if not self.is_material(category_enum, why_material, has_option_conflict=has_option_conflict):
            return GateVerdict(
                action="resolve_automatically",
                fingerprint=fp,
                reason="engineering problem, not a scientific fork",
            )

        # cheap parallel options run in parallel instead of asking
        if not has_option_conflict:
            return GateVerdict(
                action="run_parallel",
                fingerprint=fp,
                reason="cheap parallel options; no conflict",
            )

        # numerical-only differences do not ask
        if not self.is_unresolved(
            evidence_refs=evidence_refs or [],
            unresolved_uncertainty=unresolved_uncertainty,
            option_conflict=has_option_conflict,
        ):
            return GateVerdict(
                action="resolve_numerically",
                fingerprint=fp,
                reason="numerical-only difference or no scientific uncertainty",
            )

        material = self.is_material(category_enum, why_material, has_option_conflict=has_option_conflict)
        unresolved = self.is_unresolved(
            evidence_refs=evidence_refs or [],
            unresolved_uncertainty=unresolved_uncertainty,
            option_conflict=has_option_conflict,
        )
        taste = self.is_taste_sensitive(category_enum)
        if material and unresolved and (taste or self.is_hard_gate(category_enum)):
            return GateVerdict(
                action="ask_pi",
                fingerprint=fp,
                reason="material AND unresolved AND (taste-sensitive OR hard gate)",
                blocking_scope=blocking_scope or [],
                continue_scope=continue_scope or [],
            )

        # strict predicate: NOT (TasteSensitive OR HardGate) -> no ask
        return GateVerdict(
            action="resolve_automatically",
            fingerprint=fp,
            reason="not (material AND unresolved AND (taste OR hard gate))",
        )


def build_decision(verdict: GateVerdict, *, project_id: str, question: str, options: list[DecisionOption], **kw) -> Decision:
    """Materialize an OPEN decision from a verdict (only for action=ask_pi).

    Optional list fields are normalized here (the single choke point): a real
    model's candidate may carry None for optional scopes/refs, and a
    ValidationError here would crash the whole scheduler tick.
    """
    assert verdict.action == "ask_pi"
    d = Decision(
        project_id=project_id,
        status="OPEN",
        question=question,
        options=options,
        fingerprint=verdict.fingerprint,
        blocking_scope=verdict.blocking_scope or [],
        continue_scope=verdict.continue_scope or [],
        **kw,
    )
    return d
