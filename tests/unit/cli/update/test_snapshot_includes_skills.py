"""Task 10: `polyrob update` snapshot must cover <data_home>/skills.

Task 8/9 moved authored/installed user skills into <data_home>/skills. The update
snapshot (which protects rollback) previously captured only identity/, so a rollback
would not restore skills. This pins that <data_home>/skills is included when present
and omitted when absent (mirroring the identity/ `is_dir()` guard).
"""
from pathlib import Path

from cli.update.context import resolve_update_context


def test_snapshot_includes_skills_dir_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    (tmp_path / "identity").mkdir()
    (tmp_path / "skills").mkdir()
    ctx = resolve_update_context()
    assert (tmp_path / "skills") in ctx.dir_paths, "skills dir must be snapshotted"
    assert (tmp_path / "identity") in ctx.dir_paths, "identity dir still snapshotted"


def test_snapshot_omits_skills_dir_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    (tmp_path / "identity").mkdir()
    # no skills dir created
    ctx = resolve_update_context()
    assert (tmp_path / "skills") not in ctx.dir_paths
