"""Runtime isolation: workspace/data must live in a DIFFERENT tree than code+secrets.

Covers doc 01 (launch-finalization) T1 (one path resolver) + T2 (stop anchoring
server runtime data inside the code tree). The structural floor the agent's
filesystem confinement leans on: realpath(workspace_root) is NOT under
realpath(code_root) on the server path.
"""
from pathlib import Path

from core.path_safety import is_within_root


# ---------------------------------------------------------------------------
# T1 — resolve_runtime_paths
# ---------------------------------------------------------------------------

def test_server_data_home_outside_code_root(tmp_path, monkeypatch):
    """local=False with POLYROB_DATA_DIR set → workspace_root NOT under code_root."""
    from core.runtime_paths import resolve_runtime_paths

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "pdata"))
    paths = resolve_runtime_paths(local=False)

    assert not is_within_root(str(paths.workspace_root), str(paths.code_root)), (
        f"workspace_root {paths.workspace_root} must not be under code_root {paths.code_root}"
    )
    # data_home is what we set; workspace_root is data_home/task.
    assert is_within_root(str(paths.workspace_root), str(paths.data_home))


def test_server_data_home_default_outside_code_root(monkeypatch):
    """No POLYROB_DATA_DIR → the server default data_home is still outside code_root."""
    from core.runtime_paths import resolve_runtime_paths

    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    paths = resolve_runtime_paths(local=False)

    assert not is_within_root(str(paths.data_home), str(paths.code_root)), (
        f"default server data_home {paths.data_home} must not be under code_root {paths.code_root}"
    )
    assert not is_within_root(str(paths.workspace_root), str(paths.code_root))


def test_local_workspace_is_cwd(monkeypatch, tmp_path):
    """local=True → workspace_root == cwd (project-root mode preserved)."""
    from core.runtime_paths import resolve_runtime_paths

    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    paths = resolve_runtime_paths(local=True)

    assert paths.workspace_root == Path.cwd()


# ---------------------------------------------------------------------------
# T2 — config does not anchor server runtime data inside the code tree
# ---------------------------------------------------------------------------

def test_ensure_directories_no_cwd_data_tree(tmp_path, monkeypatch):
    """A1 regression lock: constructing BotConfig() must not create ./data in CWD."""
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    from core.config import BotConfig

    BotConfig()

    assert not (tmp_path / "data").exists(), "BotConfig polluted CWD with ./data"


def test_server_data_dir_under_data_home(tmp_path, monkeypatch):
    """With POLYROB_DATA_DIR set, data_dir resolves under data_home, not code_root."""
    from core.runtime_paths import resolve_runtime_paths

    data_home = tmp_path / "pdata"
    monkeypatch.setenv("POLYROB_DATA_DIR", str(data_home))
    monkeypatch.chdir(tmp_path)

    from core.config import BotConfig

    cfg = BotConfig()
    paths = resolve_runtime_paths(local=False)

    assert is_within_root(str(cfg.data_dir), str(paths.data_home)), (
        f"data_dir {cfg.data_dir} must resolve under data_home {paths.data_home}"
    )
    assert not is_within_root(str(cfg.data_dir), str(paths.code_root))
