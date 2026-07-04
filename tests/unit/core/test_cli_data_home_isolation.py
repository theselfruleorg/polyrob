"""Doc 06 / 01 coordination: POLYROB_DATA_DIR is the headless isolation switch.

On the headless server the CLI/local container runs from /opt/polyrob with
POLYROB_LOCAL=1. Without this, `rob_dir` was hardcoded to `cwd/.polyrob` =
INSIDE the code tree, so goals.db + agent workspaces lived next to the agent's
own source + config/.env.* (an isolation breach). Setting POLYROB_DATA_DIR must
move the data home OUTSIDE the code tree AND drop the workspace==cwd exception.
"""

from pathlib import Path

from core.bootstrap import _resolve_cli_data_home
from core.path_safety import is_within_root


def test_data_dir_set_isolates_outside_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "polyrob-data"))
    data_home, ws_is_project_root, project_root = _resolve_cli_data_home()
    assert data_home == (tmp_path / "polyrob-data").resolve()
    # Isolated mode: workspace lives UNDER the data home, NOT == cwd (the code tree).
    assert ws_is_project_root is False
    assert project_root is None
    # The workspace root (data_home/sessions/...) is NOT under cwd (= the code tree on the server).
    assert not is_within_root(str(data_home), str(Path.cwd().resolve()))


def test_data_dir_unset_is_legacy_local(monkeypatch):
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("POLYROB_PROJECT_DIR", raising=False)
    data_home, ws_is_project_root, project_root = _resolve_cli_data_home()
    # Local dev: byte-identical to legacy — cwd/.polyrob, workspace == cwd (consented).
    assert data_home == Path.cwd() / ".polyrob"
    assert ws_is_project_root is True
    assert project_root == str(Path.cwd())


def test_project_dir_enables_project_root_independent_of_data_dir(monkeypatch, tmp_path):
    # Explicit project dir + data dir elsewhere: persistent shared workspace at the
    # project, data still outside the code tree. This is the headless-persistent case.
    proj = tmp_path / "myproject"
    data = tmp_path / "polyrob-data"
    monkeypatch.setenv("POLYROB_PROJECT_DIR", str(proj))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(data))
    data_home, ws_is_project_root, project_root = _resolve_cli_data_home()
    assert ws_is_project_root is True
    assert project_root == str(proj.resolve())
    assert data_home == data.resolve()  # data home stays where POLYROB_DATA_DIR points


def test_project_dir_without_data_dir_uses_dot_polyrob_home(monkeypatch, tmp_path):
    proj = tmp_path / "myproject"
    monkeypatch.setenv("POLYROB_PROJECT_DIR", str(proj))
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    data_home, ws_is_project_root, project_root = _resolve_cli_data_home()
    assert ws_is_project_root is True
    assert project_root == str(proj.resolve())
    assert data_home == Path.cwd() / ".polyrob"
