"""The injected PathManager root is honored: artifacts live under .rob, never data/task (R7)."""
import os

import pytest

from agents.task.path import get_path_manager


def test_injected_root_keeps_artifacts_under_rob(tmp_path):
    proj = tmp_path / "proj"; proj.mkdir()
    pm = get_path_manager(
        data_root=str(proj / ".rob" / "sessions"),
        workspace_is_project_root=True,
        project_root=str(proj),
    )
    workspace = pm.get_workspace_dir("sess1", "local")
    feed = pm.get_subdir("sess1", "feed", "local")
    assert workspace == proj.resolve()                       # workspace IS the project folder
    rob_root = str((proj / ".rob" / "sessions").resolve())
    assert str(feed).startswith(rob_root)                    # session artifacts under .rob
    assert "data/task" not in str(feed)
    assert "data/auto" not in str(feed)


def test_normalize_path_allows_in_workspace_absolute(tmp_path):
    """Project-root workspaces have no session_id segment in the path; an absolute
    path inside the workspace must be allowed, not rejected as 'external'."""
    proj = tmp_path / "proj"; proj.mkdir()
    pm = get_path_manager(
        data_root=str(proj / ".rob" / "sessions"),
        workspace_is_project_root=True,
        project_root=str(proj),
    )
    abs_in_ws = str(proj / "hello.txt")
    out = pm.normalize_path(abs_in_ws, session_id="sess1")
    assert out == os.path.abspath(abs_in_ws)  # allowed as-is, not rejected


def test_normalize_path_still_rejects_external_absolute(tmp_path):
    """The security guard still rejects absolute paths OUTSIDE the workspace."""
    proj = tmp_path / "proj"; proj.mkdir()
    pm = get_path_manager(
        data_root=str(proj / ".rob" / "sessions"),
        workspace_is_project_root=True,
        project_root=str(proj),
    )
    with pytest.raises(ValueError):
        pm.normalize_path("/etc/passwd", session_id="sess1")
