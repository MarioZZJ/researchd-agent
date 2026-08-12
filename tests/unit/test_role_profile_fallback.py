"""Unknown planner role labels must not crash the dispatch loop."""

from __future__ import annotations

import pytest


def test_unknown_task_role_falls_back_to_worker_profile(tmp_path, monkeypatch):
    """A real planner may return any free-string role (the schema leaves
    `role` unconstrained); the loop must fall back to the worker profile
    instead of raising KeyError."""
    import logging

    from researchd.scheduler.loop import SchedulerLoop, ROLE_TO_PROFILE

    # exercise the pure mapping decision directly: every known role maps,
    # and an unknown role resolves to "worker" without raising
    class DummyExecutor:
        name = "fake"

    class DummySettings:
        profiles = {
            "fake_worker": {"model": "x"},
            "fake_planner": {"model": "x"},
            "fake_auditor": {"model": "x"},
        }
        scheduler = type("S", (), {"executor": "fake", "delivery": "fake"})()

    loop = object.__new__(SchedulerLoop)
    loop.executor = DummyExecutor()
    loop.settings = DummySettings()

    assert loop._default_profile_name("worker") == "fake_worker"
    assert loop._default_profile_name("analysis_worker") == "fake_worker"
    assert loop._default_profile_name("auditor") == "fake_auditor"
    # unknown free-string role -> worker fallback, never KeyError
    warnings: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record):
            warnings.append(record)

    cap = Capture()
    logging.getLogger("researchd.scheduler").addHandler(cap)
    try:
        name = loop._default_profile_name("executor")
    finally:
        logging.getLogger("researchd.scheduler").removeHandler(cap)
    assert name == "fake_worker"
    assert any("unknown task role" in w.getMessage() for w in warnings)
    # and every declared role key resolves (no accidental typos)
    for role in ROLE_TO_PROFILE:
        assert loop._default_profile_name(role) == f"fake_{ROLE_TO_PROFILE[role]}"
