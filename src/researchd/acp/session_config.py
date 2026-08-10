"""Interaction session state (IMPLEMENTATION.md §15.3).

A session's interaction profile (fast|deep|deterministic) is UI-level only and
NEVER changes the project execution policy. Policy changes go through
`/research config set role.<role> <profile>` and are recorded as
`project.executor_policy_changed` events.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InteractionSession:
    session_id: str
    interaction_profile: str = "fast"  # fast | deep | deterministic
    cc_project: str | None = None
    cc_session_key: str | None = None
    _requests: int = field(default=0, init=False)

    def request_counter(self) -> int:
        self._requests += 1
        return self._requests
