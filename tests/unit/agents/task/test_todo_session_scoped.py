"""Phase 6-A (path-concerns upgrade): session-scope todo.md in project-root mode.

B2/C3: project-root mode returned <CWD>/.polyrob/todo.md for EVERY session, so parallel
sessions/sub-agents shared one todo.md. Nest it under the per-session tree instead.
CLI_TODO_SESSION_SCOPED (default on) controls this; CLI_TODO_DOT_ROB=off still
restores the bare workspace todo.md. Server (non-project-root pm) is unchanged.
"""
from agents.task.path import PathManager


def _project_pm(tmp_path):
    return PathManager(
        data_root=str(tmp_path / ".polyrob" / "sessions"),
        workspace_is_project_root=True,
        project_root=str(tmp_path),
    )


def test_todo_is_session_scoped_in_project_root_mode(tmp_path):
    pm = _project_pm(tmp_path)
    a = pm.get_todo_file_path("sessionaaa", user_id="local")
    b = pm.get_todo_file_path("sessionbbb", user_id="local")
    assert a != b
    assert "sessionaaa" in str(a) and "sessionbbb" in str(b)
    # Not the single shared <CWD>/.polyrob/todo.md.
    assert a != tmp_path / ".polyrob" / "todo.md"


def test_todo_dot_rob_off_restores_bare(tmp_path, monkeypatch):
    monkeypatch.setenv("CLI_TODO_DOT_ROB", "off")
    pm = _project_pm(tmp_path)
    p = pm.get_todo_file_path("sessionccc", user_id="local")
    assert p == tmp_path / "todo.md"


def test_session_scoped_off_restores_dot_rob_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("CLI_TODO_SESSION_SCOPED", "off")
    pm = _project_pm(tmp_path)
    a = pm.get_todo_file_path("sessionaaa", user_id="local")
    b = pm.get_todo_file_path("sessionbbb", user_id="local")
    # Legacy behavior: shared <project>/.polyrob/todo.md for all sessions.
    assert a == b == tmp_path / ".polyrob" / "todo.md"


def test_server_mode_todo_unchanged(tmp_path):
    pm = PathManager(data_root=str(tmp_path / "data" / "task"))
    p = pm.get_todo_file_path("sessionxyz", user_id="alice")
    assert p.name == "todo.md"
    assert "workspace" in str(p)  # server keeps todo under the session workspace
