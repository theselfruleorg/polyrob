"""The daily workspace cleanup must never delete a SHARED project-root workspace.

Prod ran POLYROB_PROJECT_DIR=/var/lib/polyrob/project, so
pm().get_workspace_dir(<any session>) returned that one shared folder
(path.py: the workspace_is_project_root shortcut). cleanup_old_workspaces then
called shutil.rmtree() on it once per old session:

    Aug 16 14:49:00  Workspace cleanup: removed 198 old workspaces
    Aug 17 14:49:00  Workspace cleanup: removed 202 old workspaces

That destroyed every artifact the agent had produced. A per-session workspace is
scratch and stays collectable; the project root is the agent's HOME and is not.
"""
from datetime import datetime, timedelta

import pytest


@pytest.fixture(autouse=True)
def _reset_pm():
    from agents.task.path import reset_path_manager
    reset_path_manager()
    yield
    reset_path_manager()


def _old_session(session_id, user_id="rob"):
    stale = (datetime.now() - timedelta(days=30)).isoformat()
    return {"session_id": session_id, "user_id": user_id,
            "status": "completed", "updated_at": stale}


def _manager(tmp_path):
    from agents.task.agent.session import SessionManager
    return SessionManager(base_dir=str(tmp_path / "sessions"))


def test_cleanup_never_deletes_the_shared_project_root(tmp_path):
    from agents.task.path import get_path_manager, set_path_manager

    project = tmp_path / "project"
    project.mkdir()
    (project / "x402-ecosystem-map.html").write_text("<html>a week of work</html>")

    set_path_manager(get_path_manager(
        data_root=str(tmp_path / "sessions"),
        workspace_is_project_root=True,
        project_root=str(project),
    ))

    mgr = _manager(tmp_path)
    mgr._sessions = {"sess-a": _old_session("sess-a"), "sess-b": _old_session("sess-b")}

    cleaned = mgr.cleanup_old_workspaces(max_age_days=7)

    assert project.exists(), "cleanup deleted the shared project root"
    assert (project / "x402-ecosystem-map.html").read_text() == "<html>a week of work</html>"
    assert cleaned == 0, "a project-root workspace must not count as cleaned"


def test_cleanup_still_removes_a_per_session_workspace(tmp_path):
    """The control: legacy per-session mode keeps collecting its own scratch."""
    from agents.task.path import get_path_manager, set_path_manager

    set_path_manager(get_path_manager(data_root=str(tmp_path / "sessions")))

    mgr = _manager(tmp_path)
    mgr._sessions = {"sess-a": _old_session("sess-a")}

    from agents.task.path import pm
    ws = pm().get_workspace_dir("sess-a", "rob")
    (ws / "scratch.txt").write_text("throwaway")

    cleaned = mgr.cleanup_old_workspaces(max_age_days=7)

    assert cleaned == 1
    assert not ws.exists()
