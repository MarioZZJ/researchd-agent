"""Reasonix adapter conformance tests (IMPLEMENTATION.md §23 Phase 4).

Fake-transport conformance pins the exact protocol interaction. Real-process
conformance is gated behind RESEARCHD_RUN_REAL_CONFORMANCE=1 (paid model calls
require authorization, see docs/blockers.md B-03).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from researchd.executors.base import ValidationFailure
from researchd.executors.reasonix.adapter import ReasonixAdapter, extract_json
from researchd.executors.reasonix.overlay import ensure_overlay, overlay_env
from researchd.executors.reasonix.transport import FakeReasonixTransport, TransportError


@pytest.fixture()
def fake():
    t = FakeReasonixTransport()
    yield t
    asyncio.run(t.close_all()) if hasattr(t, "close_all") else None


def valid_work_json(task_id="T-001") -> str:
    return json.dumps(
        {
            "schema": "researchd.work_result.v1",
            "task_id": task_id,
            "outcome": "SUBMIT_FOR_REVIEW",
            "criteria_results": [{"criterion_id": "SC-1", "status": "PASS"}],
            "artifacts": [],
            "evidence_candidates": [],
            "claim_changes": [],
            "issues": [],
            "decision_candidates": [],
            "next_task_proposals": [],
        }
    )


def test_extract_json_variants():
    assert extract_json(f"```json\n{valid_work_json()}\n```")["task_id"] == "T-001"
    assert extract_json(valid_work_json())["task_id"] == "T-001"
    # trailing prose after the JSON document is rejected (no guessing)
    with pytest.raises(ValueError):
        extract_json(f"{valid_work_json()} suffix")
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_worker_conformance_protocol_sequence(fake):
    """Adapter must: initialize -> session/new -> prompt -> validate."""
    fake.script_prompt(f"```json\n{valid_work_json()}\n```")
    adapter = ReasonixAdapter(transport=fake)
    result, info = asyncio.run(
        adapter.run_worker(
            {"task": {"task_id": "T-001"}, "objective": "analyze", "package": {"charter": "x"}},
            profile={"model": "gateway/deepseek-v4-flash", "reasoning_effort": "high"},
        )
    )
    assert result.task_id == "T-001"
    assert info.session_id == "SES-FAKE-1"
    methods = [c["method"] for c in fake.calls]
    assert methods == ["initialize", "session/new", "session/prompt", "session/close"]
    # session config carries model + effort overrides
    cfg = fake.calls[1]["params"]["sessionConfig"]
    assert cfg["model"] == "gateway/deepseek-v4-flash"
    assert cfg["reasoningEffort"] == "high"
    # prompt demands the JSON schema
    assert "researchd.work_result.v1" in fake.calls[2]["params"]["prompt"]


def test_repair_loop_on_invalid_schema(fake):
    """Schema failure -> targeted repair prompt -> success. No silent pass."""
    bad = json.dumps({"schema": "researchd.work_result.v1", "task_id": "T-001", "outcome": "NOPE"})
    fake.script_prompt(f"```json\n{bad}\n```", f"```json\n{valid_work_json()}\n```")
    adapter = ReasonixAdapter(transport=fake)
    result, _ = asyncio.run(adapter.run_worker({"objective": "x"}, profile={}))
    assert result.task_id == "T-001"
    # the repair prompt (initialize, new, prompt, repair) references the
    # validation error and demands a corrected JSON document
    repair_prompt = fake.calls[3]["params"]["prompt"]
    assert "无法解析" in repair_prompt and "work_result.json" in repair_prompt


def test_repair_exhaustion_fails_run(fake):
    """Two failed repairs -> TransportError (run fails; never a fake pass)."""
    bad = json.dumps({"schema": "researchd.work_result.v1", "task_id": "T-001", "outcome": "NOPE"})
    fake.script_prompt(f"x{bad}x", f"y{bad}y", f"z{bad}z")
    adapter = ReasonixAdapter(transport=fake)
    with pytest.raises((TransportError, ValidationFailure)):
        asyncio.run(adapter.run_worker({"objective": "x"}, profile={}))
    # exactly 1 + 2 repair prompts
    prompts = [c for c in fake.calls if c["method"] == "session/prompt"]
    assert len(prompts) == 3
    # the session was closed on the failure path too
    assert fake.calls[-1]["method"] == "session/close"


def test_steering_and_cancel(fake):
    fake.script_prompt(f"```json\n{valid_work_json()}\n```")
    adapter = ReasonixAdapter(transport=fake)
    r = asyncio.run(adapter.steer("SES-FAKE-1", "focus on limitations"))
    assert r["steered"] is True
    r = asyncio.run(adapter.cancel("SES-FAKE-1"))
    assert r["cancelled"] is True
    methods = [c["method"] for c in fake.calls]
    assert "_reasonix.io/session/steer" in methods
    assert methods.count("session/close") == 1


def test_planner_and_auditor_routes(fake):
    fake.script_prompt(json.dumps({"schema": "researchd.planner_result.v1", "proposed_tasks": [], "risks": [], "plan_revisions": []}))
    adapter = ReasonixAdapter(transport=fake)
    result, _ = asyncio.run(adapter.run_planner({"objective": "plan"}, profile={}))
    assert result.schema == "researchd.planner_result.v1"
    fake.script_prompt(json.dumps({"schema": "researchd.audit_result.v1", "task_id": "T-1", "verdict": "ACCEPT", "checks": []}))
    result, _ = asyncio.run(adapter.run_auditor({"task_id": "T-1"}, profile={}))
    assert result.verdict.value == "ACCEPT"


# ---------------------------------------------------------------- overlay
def test_overlay_isolation(tmp_path, monkeypatch):
    """Overlay copies ONLY the provider blocks into the run dir with 0600; the
    global config is untouched. Uses a synthetic HOME so no real api keys are
    ever copied in tests."""
    fake_home = tmp_path / "fake-home"
    (fake_home / ".reasonix").mkdir(parents=True)
    (fake_home / ".reasonix" / "config.toml").write_text(
        'default_model = "gateway/m" \n'
        '[[providers]]\n'
        'name = "gateway"\n'
        'kind = "openai"\n'
        'base_url = "http://127.0.0.1:1/v1"\n'
        'api_key_env = "TEST_KEY"\n'
        'models = ["m"]\n'
        '[[providers]]\n'
        'name = "other"\n'
        'api_key_env = "OTHER_KEY"\n'
        '[bot]\n'
        'enabled = true\n'
        'app_secret_env = "SOMETHING"\n'
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    overlay = ensure_overlay(tmp_path / "data")
    cfg = (overlay / "config.toml").read_text()
    assert "api_key_env = \"TEST_KEY\"" in cfg
    assert "api_key_env = \"OTHER_KEY\"" in cfg  # env refs kept (needed to run)
    assert "sk-" not in cfg  # inline secrets NEVER copied
    assert "SOMETHING" not in cfg  # bot secrets NOT copied
    mode = (overlay / "config.toml").stat().st_mode & 0o777
    assert mode == 0o600
    assert (overlay / "sessions").is_dir()
    # restricted env: whitelist only, HOME redirected into the overlay
    env = overlay_env(overlay)
    assert env["REASONIX_HOME"] == str(overlay)
    assert env["HOME"] == str(overlay)
    assert "SOMETHING" not in env


# ---------------------------------------------------------------- real (gated)
REAL = os.environ.get("RESEARCHD_RUN_REAL_CONFORMANCE") == "1"


@pytest.mark.skipif(not REAL, reason="gated: real reasonix conformance requires authorization (B-03)")
def test_real_process_session_lifecycle(tmp_path):
    """Real `reasonix acp` with isolated overlay: initialize + session/new +
    prompt + close. Requires gateway access (paid model)."""
    import asyncio

    from researchd.executors.reasonix.transport import StdioReasonixTransport

    if shutil.which("reasonix") is None:
        pytest.skip("reasonix binary not on PATH")
    overlay = ensure_overlay(tmp_path)
    transport = StdioReasonixTransport(overlay)
    async def run():
        caps = await transport.initialize()
        assert caps.get("loadSession") is True
        sid = await transport.new_session({"model": os.environ.get("REASONIX_CONFORMANCE_MODEL", "gateway/deepseek-v4-flash")})
        assert sid
        text = await transport.prompt(sid, "Reply with exactly: PONG")
        assert "PONG" in text
        await transport.close(sid)
        await transport.close_all()
    asyncio.run(run())


def test_overlay_excludes_following_tables(tmp_path, monkeypatch):
    """[[mcp...]] / [bot] tables after providers must NOT be copied."""
    fake_home = tmp_path / "fake-home2"
    (fake_home / ".reasonix").mkdir(parents=True)
    (fake_home / ".reasonix" / "config.toml").write_text(
        '[[providers]]\n'
        'name = "gw"\n'
        'api_key_env = "GW_KEY"\n'
        '[[mcp.servers]]\n'
        'name = "secret-mcp"\n'
        'token = "mcp-secret-token"\n'
        '[bot]\n'
        'enabled = true\n'
        'app_secret_env = "BOT_SECRET"\n'
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    overlay = ensure_overlay(tmp_path / "data2")
    cfg = (overlay / "config.toml").read_text()
    assert 'api_key_env = "GW_KEY"' in cfg
    assert "sk-gw" not in cfg  # inline secrets never copied
    assert "mcp-secret-token" not in cfg
    assert "BOT_SECRET" not in cfg


def test_sanitized_parse_errors_never_include_raw_reply():
    """Trailing-content and no-JSON errors must not carry reply fragments."""
    from researchd.executors.reasonix.adapter import extract_json

    with pytest.raises(ValueError) as exc1:
        extract_json('{"schema": "x"} trailing secret-data-here')
    assert "secret-data-here" not in str(exc1.value)
    with pytest.raises(ValueError) as exc2:
        extract_json("no json here at all")
    assert "no json here" not in str(exc2.value)


def test_profile_resolution_rejects_unknown(tmp_path, monkeypatch):
    """Unknown contract profile raises instead of silently falling back."""
    from researchd.config import DEFAULT_PROFILES
    from researchd.scheduler.loop import SchedulerLoop

    class FakeExecutorStub:
        name = "reasonix"

    settings = type("S", (), {"profiles": dict(DEFAULT_PROFILES)})()
    loop = SchedulerLoop.__new__(SchedulerLoop)
    loop.executor = FakeExecutorStub()
    loop.settings = settings
    loop.session_factory = None
    from researchd.domain.task import Task, TaskContract, SuccessCriterion

    t = Task(
        task_id="T-1", project_id="P-1",
        contract=TaskContract(
            task_id="T-1", role="analysis_worker", objective="o",
            success_criteria=[SuccessCriterion(id="SC-1", text="c")],
            executor_profile="no-such-profile",
        ),
    )
    import pytest as _pytest

    with _pytest.raises(ValueError):
        loop._resolve_profile(None, t)
