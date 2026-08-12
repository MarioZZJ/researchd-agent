"""normalize_artifact_path: model outputs are free-form; only rewrite when
the target file really exists — never widen a boundary."""

from __future__ import annotations

from researchd.application.paths import normalize_artifact_path


def test_strips_workspace_basename_prefix_when_file_exists(tmp_path):
    ws = tmp_path / "ws"
    out = ws / "out"
    out.mkdir(parents=True)
    (out / "result.json").write_text("{}")
    assert normalize_artifact_path(ws, "ws/out/result.json") == "out/result.json"


def test_keeps_basename_prefix_when_file_does_not_exist(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # no out/result.json on disk: the gate must decide, not the normalizer
    assert normalize_artifact_path(ws, "ws/out/result.json") == "ws/out/result.json"


def test_absolute_inside_root_becomes_relative(tmp_path):
    ws = tmp_path / "ws"
    (ws / "out").mkdir(parents=True)
    (ws / "out" / "r.json").write_text("{}")
    assert normalize_artifact_path(ws, str(ws / "out" / "r.json")) == "out/r.json"


def test_absolute_outside_root_left_untouched(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "x.json"
    outside.write_text("{}")
    assert normalize_artifact_path(ws, str(outside)) == str(outside)


def test_plain_relative_unchanged(tmp_path):
    ws = tmp_path / "ws"
    (ws / "out").mkdir(parents=True)
    (ws / "out" / "r.json").write_text("{}")
    assert normalize_artifact_path(ws, "out/r.json") == "out/r.json"
