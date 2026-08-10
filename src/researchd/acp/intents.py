"""Constrained intent classification for free-form input (IMPLEMENTATION.md §18).

First version: rule-based intent detection over a small vocabulary, with a
confidence score. No LLM is required for the deterministic paths; the
interaction profile only tunes how aggressive the fallback is. An LLM-based
classifier can be plugged in later behind this interface (the threshold comes
from settings.interaction.intent_confidence_threshold).

Negation guard: commands preceded by "不要/别/不/没" are NOT matched, so
"不要暂停" never pauses the project.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STATUS_WORDS = {"状态", "status", "进展", "进度"}
PAUSE_WORDS = {"暂停", "pause", "pausing"}
RESUME_WORDS = {"继续", "恢复", "resume"}
DIGEST_WORDS = {"摘要", "digest", "汇总"}
BIND_WORDS = {"绑定", "bind"}
DECISION_WORDS = re.compile(r"(选择|选|决定|decision)\s*([A-Za-z]+-\d+)\s*([A-Za-z])", re.IGNORECASE)
EXPLAIN_WORDS = re.compile(r"(解释|explain|为什么)\s*([A-Za-z]+-\d+)", re.IGNORECASE)

_NEGATION_WORDS = ("不要", "别", "不", "没", "无需", "不用", "no", "don't", "dont", "not")
_NEGATION_PREFIX = re.compile(rf"^({'|'.join(_NEGATION_WORDS)})\s*", re.IGNORECASE)


@dataclass
class Intent:
    name: str
    confidence: float
    command_text: str | None = None
    command_name: str | None = None
    explanation: str | None = None


def _negated(text: str) -> bool:
    return bool(_NEGATION_PREFIX.match(text.strip()))


def _negated_near(text: str, word: str) -> bool:
    """True when a negation word appears within 6 chars BEFORE the hit word,
    e.g. '请不要暂停项目' or 'please don't pause'."""
    idx = text.lower().find(word.lower())
    if idx < 0:
        return False
    window = text[max(0, idx - 6):idx]
    for neg in _NEGATION_WORDS:
        if neg in window.lower():
            return True
    return False


def classify_intent(text: str, *, profile: str) -> Intent | None:
    """Rule-based intent classification. Returns None when nothing matches
    confidently. `profile` tunes thresholds: deterministic only accepts very
    strong matches; deep may accept weaker ones."""
    if profile == "deterministic":
        # deterministic interaction never acts on free-form language
        return None
    t = text.strip()
    if _negated(t):
        return None

    if DECISION_WORDS.search(t) and not _negated_near(t, "decision"):
        m = DECISION_WORDS.search(t)
        decision_id, option = m.group(2), m.group(3)
        return Intent("answer_decision", 0.98, f"/decision {decision_id} {option}", "decision")
    if EXPLAIN_WORDS.search(t) and not _negated_near(t, "explain"):
        m = EXPLAIN_WORDS.search(t)
        return Intent("explain_object", 0.97, f"/explain {m.group(2)}", "explain")
    if any(w in t for w in STATUS_WORDS) and not _negated_near(t, next(w for w in STATUS_WORDS if w in t)):
        return Intent("status", 0.9, "/research status", "status")
    if any(w in t for w in PAUSE_WORDS) and not _negated_near(t, next(w for w in PAUSE_WORDS if w in t)):
        return Intent("pause", 0.92, "/research pause", "pause")
    if any(w in t for w in RESUME_WORDS) and not _negated_near(t, next(w for w in RESUME_WORDS if w in t)):
        return Intent("resume", 0.92, "/research resume", "resume")
    if any(w in t for w in DIGEST_WORDS) and not _negated_near(t, next(w for w in DIGEST_WORDS if w in t)):
        return Intent("digest", 0.9, "/research digest", "digest")
    if any(w in t for w in BIND_WORDS) and not _negated_near(t, next(w for w in BIND_WORDS if w in t)):
        return Intent("bind", 0.85, None, None, "请用 /research bind project <project-id> 指定项目")
    return None


def needs_llm_intent(text: str, *, profile: str) -> bool:
    """Whether the interaction profile would route this to an LLM classifier
    (used to gate such calls behind authorization)."""
    return classify_intent(text, profile=profile) is None
