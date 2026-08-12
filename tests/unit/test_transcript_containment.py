"""Transcript containment: only regular files under the overlay sessions dir
are readable; symlinks, devices and oversized files are refused."""

from __future__ import annotations

import json

import pytest

from researchd.executors.reasonix.transport import StdioReasonixTransport


def _transcript(sessions_dir, lines):
    path = sessions_dir / "t.jsonl"
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return str(path)


def test_reads_last_assistant_text_block_from_sessions_dir(tmp_path):
    sessions = tmp_path / "overlay" / "sessions"
    sessions.mkdir(parents=True)
    tp = _transcript(
        sessions,
        [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": [{"type": "text", "text": "second"}]},
            {"role": "assistant", "content": ""},
        ],
    )
    out = StdioReasonixTransport._last_assistant_text(tp, overlay_root=tmp_path / "overlay")
    assert out == "second"


def test_refuses_path_outside_sessions_dir(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    tp = _transcript(outside, [{"role": "assistant", "content": "x"}])
    out = StdioReasonixTransport._last_assistant_text(tp, overlay_root=tmp_path / "overlay")
    assert out == ""


def test_refuses_symlink_final_component(tmp_path):
    """O_NOFOLLOW: a sandbox process cannot swap the transcript for a
    symlink to a readable host file after the containment check."""
    sessions = tmp_path / "overlay" / "sessions"
    sessions.mkdir(parents=True)
    victim = tmp_path / "host-secret.txt"
    victim.write_text("TOP-SECRET")
    link = sessions / "t.jsonl"
    link.symlink_to(victim)
    out = StdioReasonixTransport._last_assistant_text(str(link), overlay_root=tmp_path / "overlay")
    assert out == ""
    assert "TOP-SECRET" not in out


def test_refuses_oversized_transcript(tmp_path):
    sessions = tmp_path / "overlay" / "sessions"
    sessions.mkdir(parents=True)
    tp = _transcript(sessions, [{"role": "assistant", "content": "x" * 100}])
    out = StdioReasonixTransport._last_assistant_text(tp, overlay_root=tmp_path / "overlay", max_bytes=50)
    assert out == ""
