"""Acceptance definitions cannot widen authority or silently weaken verification."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from agents.task.runtime.acceptance_checks import run_acceptance_checks


def run(checks, **context):
    return asyncio.run(run_acceptance_checks(checks, **context))


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a/../../secret", "C:/secret", "a\\..\\secret"])
@pytest.mark.parametrize("kind,key", [("file_contains", "path"), ("artifact_glob", "pattern")])
def test_paths_cannot_escape_the_run(tmp_path, path, kind, key):
    check = {"type": kind, key: path}
    if kind == "file_contains":
        check["contains"] = ["secret"]
    assert not run([check], workspace_dir=str(tmp_path))[0]["ok"]


def test_no_probe_runs_if_any_declaration_is_invalid(monkeypatch, tmp_path):
    from agents.task.runtime import acceptance_checks as ac
    probe = AsyncMock(return_value=(True, "ok"))
    monkeypatch.setitem(ac._CHECK_TYPES, "fixture", probe)
    for bad in ([{"type": "fixture"}] * 11,
                [{"type": "fixture"}, "malformed"],
                [{"type": "fixture", "workspace_dir": str(tmp_path)}]):
        results = run(bad, workspace_dir=str(tmp_path))
        assert results and not results[0]["ok"]
    probe.assert_not_called()


@pytest.mark.parametrize("directory", [False, True])
def test_symlinks_never_satisfy_file_checks(tmp_path, directory):
    outside, workspace = tmp_path / "outside", tmp_path / "workspace"
    outside.mkdir()
    workspace.mkdir()
    (outside / "secret.txt").write_text("needle")
    if directory:
        (workspace / "link").symlink_to(outside, target_is_directory=True)
        path = "link/secret.txt"
    else:
        (workspace / "secret.txt").symlink_to(outside / "secret.txt")
        path = "secret.txt"
    assert not run([{"type": "file_contains", "path": path, "contains": "needle"}],
                   workspace_dir=str(workspace))[0]["ok"]
    assert not run([{"type": "artifact_glob", "pattern": "*.txt"}],
                   workspace_dir=str(workspace))[0]["ok"]


def test_artifact_id_is_scoped_to_goal_and_session(tmp_path):
    from core.artifacts import get_artifact_ledger
    path = tmp_path / "report.md"
    path.write_text("verified content")
    row = get_artifact_ledger().record("u1", str(path), session_id="s1", goal_id="g1")
    check = [{"type": "artifact", "id": row.id, "contains": "verified"}]
    assert run(check, user_id="u1", goal_id="g1", session_id="s1")[0]["ok"]
    for context in ({"goal_id": "g2"}, {"session_id": "s2"}, {}):
        assert not run(check, user_id="u1", **context)[0]["ok"]


def test_eleventh_goal_check_is_rejected_without_persisting(tmp_path):
    from agents.task.goals.board import GoalBoard
    from tools.goal_tools import GoalTool, GoalCreateAction
    board = GoalBoard(str(tmp_path / "goals.db"))
    tool = GoalTool.__new__(GoalTool)
    tool._resolve_board = lambda: board
    tool._user = lambda _: "u1"
    params = GoalCreateAction(title="fixture", acceptance_checks=[
        {"type": "artifact_glob", "pattern": "*.md"}] * 11)
    result = asyncio.run(tool.goal_create(params))
    assert result.error and not board.list(user_id="u1")
