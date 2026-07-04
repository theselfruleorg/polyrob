"""Regression: a redundant leading "workspace/" must not nest into <ws>/workspace/.

The confinement root of the filesystem tool IS the session workspace dir (basename
'workspace'). Agents sometimes follow an instruction like "save to workspace/brief.md"
literally; joining that against the workspace root produced
``<ws>/workspace/brief.md`` (observed live for grok-4.3 / qwen during the 2026-06-20
multi-model run). Collapse a single redundant leading 'workspace/' segment so the file
lands at the workspace root as intended.
"""
import logging
import os

import pytest


def _fs_tool():
    from tools.filesystem import FileSystem
    t = object.__new__(FileSystem)
    t.logger = logging.getLogger("fs-wsprefix")
    t.session_id = "s1"
    t.user_id = "u1"
    t._current_session_id = None
    return t


@pytest.fixture
def _pm(tmp_path):
    from agents.task.path import PathManager, set_path_manager
    pm = PathManager(data_root=str(tmp_path / "data"))
    set_path_manager(pm)
    return pm


def test_redundant_workspace_prefix_collapses(_pm):
    tool = _fs_tool()
    ws = _pm.get_workspace_dir("s1", "u1")
    assert os.path.basename(os.path.normpath(str(ws))) == "workspace"

    resolved = tool._normalize_path("workspace/brief.md")

    # lands at the workspace root, NOT nested under a second workspace/
    assert os.path.normpath(resolved) == os.path.normpath(str(ws / "brief.md"))
    assert "workspace/workspace" not in resolved.replace(os.sep, "/")


def test_plain_relative_path_unaffected(_pm):
    tool = _fs_tool()
    ws = _pm.get_workspace_dir("s1", "u1")
    resolved = tool._normalize_path("brief.md")
    assert os.path.normpath(resolved) == os.path.normpath(str(ws / "brief.md"))


def test_nested_non_workspace_subdir_preserved(_pm):
    tool = _fs_tool()
    ws = _pm.get_workspace_dir("s1", "u1")
    resolved = tool._normalize_path("reports/q1.md")
    assert os.path.normpath(resolved) == os.path.normpath(str(ws / "reports" / "q1.md"))
