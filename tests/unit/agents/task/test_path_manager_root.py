"""PathManager honors an injected root + project-as-workspace (R1)."""
from pathlib import Path
from agents.task.path import PathManager, get_path_manager


def test_default_construction_unchanged(tmp_path):
    pm = PathManager(data_root=str(tmp_path / "data" / "task"))
    ws = pm.get_workspace_dir("sess1", "u1")
    assert ws == (tmp_path / "data" / "task" / "u1" / "sess1" / "workspace").resolve()


def test_project_root_is_workspace(tmp_path):
    proj = tmp_path / "myproj"
    proj.mkdir()
    pm = PathManager(
        data_root=str(proj / ".rob" / "sessions"),
        workspace_is_project_root=True,
        project_root=str(proj),
    )
    # workspace == the project folder, NOT a nested .rob/.../workspace
    assert pm.get_workspace_dir("s1", "local") == proj.resolve()
    # session artifacts still live under the .rob data root
    sub = pm.get_subdir("s1", "feed", "local")
    assert str(sub).startswith(str((proj / ".rob" / "sessions").resolve()))


def test_factory_returns_configured_manager(tmp_path):
    pm = get_path_manager(data_root=str(tmp_path), workspace_is_project_root=True,
                          project_root=str(tmp_path))
    assert pm.get_workspace_dir("s1") == Path(tmp_path).resolve()


def test_is_project_root_workspace_true_when_configured(tmp_path):
    pm = PathManager(data_root=str(tmp_path / "d"),
                     workspace_is_project_root=True,
                     project_root=str(tmp_path / "proj"))
    assert pm.is_project_root_workspace is True


def test_is_project_root_workspace_false_for_default(tmp_path):
    pm = PathManager(data_root=str(tmp_path / "d"))
    assert pm.is_project_root_workspace is False


def test_is_project_root_workspace_false_when_flag_without_root(tmp_path):
    # Flag True but no project_root => get_workspace_dir would NOT use project mode,
    # so the accessor must report False (honest effective signal).
    pm = PathManager(data_root=str(tmp_path / "d"), workspace_is_project_root=True)
    assert pm.is_project_root_workspace is False


def test_workspace_subdir_follows_the_project_root(tmp_path):
    """A file made for the "workspace" lands where the file tools look.

    Prod 2026-10-09: a run with the project folder as its workspace offloaded a
    472k-char tool result via create_file_path(sid, "workspace", ...) into the
    session dir, then filesystem_read_file (rooted at get_workspace_dir = the
    project folder) could not find it -- 50 "File not found: content_*" in 7 days.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    pm = PathManager(data_root=str(tmp_path / "data"),
                     workspace_is_project_root=True, project_root=str(proj))
    assert pm.get_subdir("s1", "workspace", "rob") == pm.get_workspace_dir("s1", "rob")
    f = pm.create_file_path("s1", "workspace", "content_1_ab.json", user_id="rob")
    assert f.parent == proj.resolve()
    # other session subdirs still live under the data root
    assert str(pm.get_subdir("s1", "feed", "rob")).startswith(str((tmp_path / "data").resolve()))
