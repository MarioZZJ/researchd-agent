"""Reasonix overlay whitelist tests (IMPLEMENTATION.md §15.2): config keys,
skills mounting, secret exclusion, per-workspace cwd routing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from researchd.executors.reasonix.overlay import (
    ALLOWED_SKILLS,
    TOP_LEVEL_KEYS,
    _minimal_config,
    ensure_overlay,
    installed_skills,
    overlay_env,
    overlay_workdir,
)

GLOBAL_CONFIG = """\
config_version = 5
default_model = "gateway/deepseek-v4-flash"
planner_model = "gateway/gpt-5.6-sol"
subagent_model = "gateway/deepseek-v4-flash"
subagent_effort = "max"
subagent_models = { review = "gateway/gpt-5.6-sol", research = "gateway/deepseek-v4-pro" }
max_subagent_depth = 2
max_subagent_concurrency = 32
max_parallel_writers = 16
theme = "auto"
telemetry = true

[bot]
token = "BOT-SECRET"

[[mcp.servers]]
name = "github"
token = "MCP-SECRET"

[[providers]]
name = "gateway"
api_key = "GATEWAY-KEY"

[[providers]]
name = "direct"
api_key = "DIRECT-KEY"
"""


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".reasonix" / "skills").mkdir(parents=True)
    (home / ".reasonix" / "config.toml").write_text(GLOBAL_CONFIG)
    for skill in ALLOWED_SKILLS:
        d = home / ".reasonix" / "skills" / skill
        d.mkdir()
        (d / "SKILL.md").write_text(f"# {skill}\n")
        (d / "secret.toml").write_text("token=SECRET")
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def test_minimal_config_whitelists_keys_and_providers(fake_home):
    out = _minimal_config(fake_home / ".reasonix" / "config.toml")
    for key in TOP_LEVEL_KEYS:
        assert key in out, f"whitelisted key {key} missing"
    assert 'api_key = "GATEWAY-KEY"' in out
    assert 'api_key = "DIRECT-KEY"' in out
    # secrets and unrelated config are NOT copied
    for banned in ("BOT-SECRET", "MCP-SECRET", "[bot]", "[[mcp", "telemetry = true", "theme = "):
        assert banned not in out, f"banned content leaked: {banned}"


def test_overlay_installs_whitelisted_skills_only(fake_home, tmp_path):
    overlay = ensure_overlay(tmp_path / "data")
    skills = installed_skills(overlay)
    assert sorted(skills) == sorted(ALLOWED_SKILLS)
    cfg = overlay / "config.toml"
    assert (cfg.stat().st_mode & 0o777) == 0o600
    assert (overlay.stat().st_mode & 0o777) == 0o700
    assert (overlay / "sessions").is_dir()
    # skill files are 0600
    for skill in ALLOWED_SKILLS:
        assert (overlay / "skills" / skill / "SKILL.md").stat().st_mode & 0o777 == 0o600


def test_overlay_env_whitelist(fake_home, tmp_path, monkeypatch):
    overlay = ensure_overlay(tmp_path / "data")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/real-home")
    monkeypatch.setenv("RESEARCHD_API__TOKEN", "DB-TOKEN")
    monkeypatch.setenv("LARK_APP_SECRET", "FEISHU-SECRET")
    monkeypatch.setenv("AWS_CREDS", "SHOULD-NOT-PASS")
    env = overlay_env(overlay)
    assert env["REASONIX_HOME"] == str(overlay)
    assert env["HOME"] == str(overlay)
    assert "RESEARCHD_API__TOKEN" not in env
    assert "LARK_APP_SECRET" not in env
    assert "AWS_CREDS" not in env


def test_overlay_workdir_fallback(fake_home, tmp_path):
    overlay = ensure_overlay(tmp_path / "data")
    work = overlay_workdir(overlay)
    assert work == overlay / "work"
    assert work.is_dir()


def test_adapter_routes_transports_per_workspace(tmp_path, monkeypatch):
    """The adapter gives each project workspace its own subprocess cwd; the
    fallback (no workspace) uses the overlay work dir."""
    from researchd.executors.reasonix.adapter import ReasonixAdapter
    from researchd.executors.reasonix.transport import StdioReasonixTransport

    monkeypatch.setattr("researchd.executors.reasonix.transport.resolve_native_binary", lambda: "/bin/true")
    adapter = ReasonixAdapter(settings=type("S", (), {"data_dir": str(tmp_path / "data")})())
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    t1a = adapter._transport_for(str(ws1))
    t1b = adapter._transport_for(str(ws1))
    t2 = adapter._transport_for(str(ws2))
    fallback = adapter._transport_for(None)
    assert t1a is t1b  # same workspace -> shared process
    assert t1a is not t2 and t1a is not fallback
    assert isinstance(t1a, StdioReasonixTransport)
    assert str(t1a.cwd.resolve()) == str(ws1.resolve())
    assert str(t2.cwd.resolve()) == str(ws2.resolve())
    assert str(fallback.cwd.resolve()) == str((tmp_path / "data" / "rx-overlay" / "work").resolve())
