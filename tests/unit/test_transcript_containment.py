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


def test_refuses_intermediate_directory_symlink(tmp_path):
    """O_NOFOLLOW on EVERY component: swapping an intermediate dir for a
    symlink out of the sessions dir must be refused (a sandbox process
    could otherwise smuggle a host file into the resolved path)."""
    import os

    sessions = tmp_path / "overlay" / "sessions"
    sessions.mkdir(parents=True)
    victim = tmp_path / "host-secret.txt"
    victim.write_text("TOP-SECRET")
    sub = sessions / "sub"
    sub.symlink_to(victim.parent)  # intermediate component -> host dir
    tp = str(sub / "t.jsonl")
    out = StdioReasonixTransport._last_assistant_text(tp, overlay_root=tmp_path / "overlay")
    assert out == ""
    assert "TOP-SECRET" not in out


def test_repeated_missing_nested_path_does_not_leak_fds(tmp_path):
    """A missing component deep in the walk must release every opened
    dirfd — repeated hostile paths cannot exhaust the service's fds."""
    import os

    sessions = tmp_path / "overlay" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "sub").mkdir()  # intermediate dir opens, THEN the walk fails

    def _open_fds():
        return len(os.listdir("/proc/self/fd"))

    before = _open_fds()
    for _ in range(200):
        out = StdioReasonixTransport._last_assistant_text(
            str(sessions / "sub" / "missing.jsonl"), overlay_root=tmp_path / "overlay"
        )
        assert out == ""
    after = _open_fds()
    assert after <= before + 1, f"fd leak on failed nested walk: {before} -> {after}"


def test_fifo_does_not_block_and_fds_do_not_leak(tmp_path):
    """A sandbox-created FIFO must not block the scheduler (O_NONBLOCK) and
    refused opens must not leak descriptors."""
    import os

    sessions = tmp_path / "overlay" / "sessions"
    sessions.mkdir(parents=True)
    fifo = sessions / "t.jsonl"
    os.mkfifo(fifo)

    def _open_fds():
        return len(os.listdir("/proc/self/fd"))

    before = _open_fds()
    out = StdioReasonixTransport._last_assistant_text(str(fifo), overlay_root=tmp_path / "overlay")
    after = _open_fds()
    assert out == ""
    assert after <= before + 1, f"fd leak: {before} -> {after}"
    # a directory at the final component is also refused without blocking
    d = sessions / "adir"
    d.mkdir()
    out2 = StdioReasonixTransport._last_assistant_text(str(d), overlay_root=tmp_path / "overlay")
    assert out2 == ""
    assert _open_fds() <= before + 1
