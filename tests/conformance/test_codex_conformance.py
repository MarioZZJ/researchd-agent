"""Codex App Server adapter conformance tests (IMPLEMENTATION.md §23 Phase 5).

Fake-transport conformance pins the exact protocol interaction. Real-process
conformance is gated behind RESEARCHD_RUN_REAL_CONFORMANCE=1 (paid model calls
require authorization, see docs/blockers.md B-02).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from researchd.executors.base import ValidationFailure
from researchd.executors.codex.adapter import CodexAdapter, assistant_text_from_turn
from researchd.executors.codex.transport import FakeCodexTransport, TransportError


@pytest.fixture()
def fake():
    return FakeCodexTransport()


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


def completed_turn(text: str) -> dict:
    """v2 wire shape: assistant messages are {"type":"agentMessage","text":...}."""
    return {
        "status": "completed",
        "error": None,
        "items": [
            {"type": "agentMessage", "id": "item-1", "text": text},
        ],
    }


def test_assistant_text_extraction():
    turn = completed_turn("hello")
    assert assistant_text_from_turn(turn) == "hello"
    assert assistant_text_from_turn({"status": "completed", "items": []}) == ""
    assert assistant_text_from_turn({"status": "completed", "items": [{"type": "other"}]}) == ""


def test_worker_protocol_sequence(fake):
    """Adapter must: initialize -> thread/start -> turn/start (with outputSchema)
    -> wait -> validate -> thread/delete."""
    fake.script_turn_completion(status="completed", items=[
        {"type": "agentMessage", "id": "item-x", "text": f"```json\n{valid_work_json()}\n```"}
    ])
    adapter = CodexAdapter(transport=fake)
    result, info = asyncio.run(
        adapter.run_worker(
            {"task": {"task_id": "T-001"}, "objective": "analyze", "package": {"charter": "x"}, "cwd": "/tmp/w"},
            profile={"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        )
    )
    assert result.task_id == "T-001"
    assert info.session_id == "THREAD-1"
    assert info.turn_id == "TURN-1"
    methods = [c["method"] for c in fake.calls]
    assert methods == ["initialize", "thread/start", "turn/start", "thread/archive"]
    thread_params = fake.calls[1]["params"]
    assert thread_params["approvalPolicy"] == "never"  # no interactive approvals
    assert thread_params["cwd"] == "/tmp/w"
    assert thread_params["model"] == "gpt-5.6-sol"
    turn_params = fake.calls[2]["params"]
    assert turn_params["input"] == [{"type": "text", "text": turn_params["input"][0]["text"]}]
    assert "outputSchema" in turn_params  # protocol-level constraint
    assert turn_params["outputSchema"]["$id"] == "researchd.work_result.v1"
    assert turn_params["model"] == "gpt-5.6-sol"
    assert turn_params["effort"] == "high"


def test_repair_loop_via_steer(fake):
    """Schema failure -> turn/steer with expectedTurnId -> success."""
    bad = json.dumps({"schema": "researchd.work_result.v1", "task_id": "T-1", "outcome": "NOPE"})
    fake.script_turn_completion(status="completed", items=[
        {"type": "agentMessage", "id": "item-x", "text": bad}
    ])
    fake.script_turn_completion(status="completed", items=[
        {"type": "agentMessage", "id": "item-x", "text": valid_work_json()}
    ])
    adapter = CodexAdapter(transport=fake)
    result, info = asyncio.run(adapter.run_worker({"objective": "x", "cwd": "/tmp"}, profile={}))
    assert result.task_id == "T-001"
    starts = [c for c in fake.calls if c["method"] == "turn/start"]
    assert len(starts) == 2  # original + repair turn on the same thread
    assert "无法解析" in starts[1]["params"]["input"][0]["text"]
    # thread still archived at the end
    assert fake.calls[-1]["method"] == "thread/archive"


def test_turn_failure_raises(fake):
    fake.script_turn_completion(status="failed", error="model exploded")
    adapter = CodexAdapter(transport=fake)
    with pytest.raises(TransportError):
        asyncio.run(adapter.run_worker({"objective": "x", "cwd": "/tmp"}, profile={}))
    # thread cleaned up on the failure path too
    assert fake.calls[-1]["method"] == "thread/archive"


def test_repair_exhaustion_raises(fake):
    bad = json.dumps({"schema": "researchd.work_result.v1", "task_id": "T-1", "outcome": "NOPE"})
    for _ in range(3):
        fake.script_turn_completion(status="completed", items=[
            {"type": "agentMessage", "id": "item-x", "text": bad}
        ])
    adapter = CodexAdapter(transport=fake)
    with pytest.raises((TransportError, ValidationFailure)):
        asyncio.run(adapter.run_worker({"objective": "x", "cwd": "/tmp"}, profile={}))
    starts = [c for c in fake.calls if c["method"] == "turn/start"]
    assert len(starts) == 3  # original + 2 repair turns


def test_planner_and_auditor(fake):
    fake.script_turn_completion(status="completed", items=[
        {"type": "agentMessage", "id": "x", "text": json.dumps(
            {"schema": "researchd.planner_result.v1", "proposed_tasks": [], "risks": [], "plan_revisions": []}
        )}
    ])
    adapter = CodexAdapter(transport=fake)
    result, _ = asyncio.run(adapter.run_planner({"objective": "plan", "cwd": "/tmp"}, profile={}))
    assert result.schema == "researchd.planner_result.v1"
    fake.script_turn_completion(status="completed", items=[
        {"type": "agentMessage", "id": "x", "text": json.dumps(
            {"schema": "researchd.audit_result.v1", "task_id": "T-1", "verdict": "ACCEPT", "checks": []}
        )}
    ])
    result, _ = asyncio.run(adapter.run_auditor({"task_id": "T-1", "cwd": "/tmp"}, profile={}))
    assert result.verdict.value == "ACCEPT"


# ---------------------------------------------------------------- real (gated)
REAL = os.environ.get("RESEARCHD_RUN_REAL_CONFORMANCE") == "1"


@pytest.mark.skipif(not REAL, reason="gated: real codex conformance requires authorization (B-02)")
def test_real_process_lifecycle(tmp_path):
    """Real `codex app-server` over stdio: initialize + thread/start + turn/start
    with outputSchema. Requires model access (paid)."""
    from researchd.executors.codex.transport import StdioCodexTransport

    if shutil.which("codex") is None:
        pytest.skip("codex binary not on PATH")
    transport = StdioCodexTransport(workdir=str(tmp_path))

    async def run():
        await transport.initialize()
        th = await transport.thread_start({"cwd": str(tmp_path), "approvalPolicy": "never"})
        thread_id = th.get("thread", {}).get("id")
        assert thread_id
        turn = await transport.turn_start(
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "Reply with exactly: PONG"}],
            }
        )
        turn_id = turn.get("turn", {}).get("id")
        completion = await transport.wait_for_turn(thread_id, turn_id, timeout=300.0)
        assert completion.get("status") in ("completed", "failed")
        await transport.thread_close(thread_id)
        await transport.close_all()

    asyncio.run(run())


def test_codex_home_overlay_isolation(tmp_path, monkeypatch):
    """codex overlay copies ONLY auth files (0600); config.toml and state dirs
    are excluded (config.toml breaks thread/start — verified on 0.146.0).
    Uses a synthetic HOME so no real credentials are copied in tests."""
    from researchd.executors.codex.overlay import ensure_codex_home

    fake_home = tmp_path / "fake-home"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "config.toml").write_text('model = "gpt-x"\napproval_policy = "never"\n')
    (fake_home / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "sk-test"}\n')
    (fake_home / ".codex" / "sessions").mkdir()
    (fake_home / ".codex" / "sessions" / "junk").write_text("junk")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    home = ensure_codex_home(tmp_path / "data")
    assert (home / "auth.json").exists()
    assert (home / "auth.json").stat().st_mode & 0o777 == 0o600
    assert not (home / "config.toml").exists()  # breaks thread/start on 0.146.0
    assert not (home / "sessions").exists()  # state never copied
