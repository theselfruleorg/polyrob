"""G5 — confinement regression tests (the secret-exfil safety floor).

Locks the invariant the autonomous loop depends on: the `filesystem` and `coding`
tools cannot read/write `config/.env.production` (which holds MASTER_SEED, LLM keys,
the SSH key) via `..` traversal, an absolute path, or an in-workspace symlink.

These are pure regression tests — no behavior change. They pass on current code
(documenting the floor) and fail if someone weakens confinement.
"""
import logging
import os
from pathlib import Path

import pytest

from core.exceptions import ServiceError
from core.path_safety import is_within_root
from core.runtime_paths import resolve_runtime_paths


# ---------------------------------------------------------------------------
# The shared confinement gate (filesystem._normalize_path final check, :824-828)
# ---------------------------------------------------------------------------

def test_gate_rejects_secret_exfil_vectors(tmp_path):
    workspace = tmp_path / "data" / "auto" / "u" / "sessions" / "s" / "workspace"
    workspace.mkdir(parents=True)
    root = str(workspace)
    # relative traversal up to a sibling config/.env.production
    traversal = os.path.join(root, "../../../../../../config/.env.production")
    assert not is_within_root(traversal, root)
    # absolute path to the real secrets file
    assert not is_within_root("/opt/rob/config/.env.production", root)
    # in-workspace symlink escaping the root
    outside = tmp_path / "config"
    outside.mkdir()
    (outside / ".env.production").write_text("SECRET=1")
    (workspace / "link").symlink_to(outside)
    assert not is_within_root(os.path.join(root, "link/.env.production"), root)
    # a legit in-workspace file is still allowed
    assert is_within_root(os.path.join(root, "notes.txt"), root)


# ---------------------------------------------------------------------------
# filesystem tool — drive _normalize_path end-to-end against a tmp workspace
# ---------------------------------------------------------------------------

def _fs_tool():
    from tools.filesystem import FileSystem
    t = object.__new__(FileSystem)
    t.logger = logging.getLogger("fs-confine")
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


def test_filesystem_raises_on_traversal_to_secrets(_pm, monkeypatch):
    monkeypatch.setenv("FS_REALPATH_CONFINE", "on")
    tool = _fs_tool()
    with pytest.raises(ServiceError):
        tool._normalize_path("../../../../../../config/.env.production")


def test_filesystem_clamps_absolute_secret_path_inside_workspace(_pm, monkeypatch):
    monkeypatch.setenv("FS_REALPATH_CONFINE", "on")
    tool = _fs_tool()
    workspace_abs = os.path.abspath(str(_pm.get_workspace_dir("s1", "u1")))
    # absolute external path is either rejected or clamped INSIDE the workspace —
    # never resolves to the real /opt/rob/config secrets file.
    try:
        result = tool._normalize_path("/opt/rob/config/.env.production")
    except ServiceError:
        return  # rejected outright — also acceptable
    assert is_within_root(result, workspace_abs)
    assert os.path.realpath(result) != "/opt/rob/config/.env.production"


def test_filesystem_raises_on_symlink_escape(_pm, tmp_path, monkeypatch):
    monkeypatch.setenv("FS_REALPATH_CONFINE", "on")
    tool = _fs_tool()
    workspace = _pm.get_workspace_dir("s1", "u1")
    os.makedirs(workspace, exist_ok=True)
    outside = tmp_path / "escape_target"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET")
    os.symlink(str(outside), os.path.join(str(workspace), "link"))
    with pytest.raises(ServiceError):
        tool._normalize_path("link/secret.txt")


# ---------------------------------------------------------------------------
# coding tool _confine — same vectors
# ---------------------------------------------------------------------------

def _coding_tool():
    from tools.coding.tool import CodingTool
    return object.__new__(CodingTool)


@pytest.mark.parametrize("attack", [
    "../../config/.env.production",
    "../../../../../../config/.env.production",
    "/opt/rob/config/.env.production",
])
def test_coding_confine_blocks_secret_paths(tmp_path, attack):
    from tools.coding.tool import CodingError
    root = tmp_path / "ws"
    root.mkdir()
    tool = _coding_tool()
    with pytest.raises(CodingError):
        tool._confine(attack, str(root))


def test_coding_confine_blocks_symlink_to_config(tmp_path):
    from tools.coding.tool import CodingError
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "config"
    outside.mkdir()
    (outside / ".env.production").write_text("SECRET=1")
    (root / "link").symlink_to(outside)
    tool = _coding_tool()
    with pytest.raises(CodingError):
        tool._confine("link/.env.production", str(root))


# ---------------------------------------------------------------------------
# T5 — structural isolation invariant (workspace lives in a DIFFERENT tree than
# code+secrets). The realpath confine is the *inner* defense; these assert the
# *outer* structural floor: even a confine miss can't reach code/secrets because
# the workspace root is not under the code root.
# ---------------------------------------------------------------------------

def test_workspace_root_not_under_code_root(monkeypatch, tmp_path):
    """Server roots: workspace is NOT under code_root and config is NOT under workspace."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "polyrob-data"))
    paths = resolve_runtime_paths(local=False)
    # The structural floor — a confine miss in a file tool still cannot reach
    # the install/code tree or the secrets dir because they're a different tree.
    assert not is_within_root(str(paths.workspace_root), str(paths.code_root))
    assert not is_within_root(str(paths.config_dir), str(paths.workspace_root))


def test_filesystem_cannot_read_install_source(_pm, tmp_path, monkeypatch):
    """An absolute read of the install's own source is refused or clamped — never the source bytes.

    Drives the filesystem tool via `_normalize_path` (the same idiom the other
    tests in this file use). The fake `code_root/agents/service.py` is a SIBLING
    subtree of the session workspace (both under tmp_path, neither an ancestor of
    the other), mirroring the install-vs-workspace structural separation.
    """
    monkeypatch.setenv("FS_REALPATH_CONFINE", "on")
    tool = _fs_tool()
    workspace_abs = os.path.abspath(str(_pm.get_workspace_dir("s1", "u1")))

    code_root = tmp_path / "installroot"
    source = code_root / "agents" / "service.py"
    source.parent.mkdir(parents=True)
    source.write_text("SOURCE_SECRET = 'do-not-leak'\n")

    # Sanity: the source really is OUTSIDE the workspace (a sibling tree).
    assert not is_within_root(str(source), workspace_abs)

    try:
        result = tool._normalize_path(str(source))
    except ServiceError:
        return  # refused outright — also acceptable
    # Clamped: resolves INSIDE the workspace, never to the real source file.
    assert is_within_root(result, workspace_abs)
    assert os.path.realpath(result) != os.path.realpath(str(source))
    # And if the clamped path happens to exist, it does not contain the source bytes.
    if os.path.exists(result):
        assert "SOURCE_SECRET" not in Path(result).read_text()


def test_filesystem_cannot_read_env_production(_pm, tmp_path, monkeypatch):
    """An absolute read of a fake config/.env.production is unreachable from the workspace."""
    monkeypatch.setenv("FS_REALPATH_CONFINE", "on")
    tool = _fs_tool()
    workspace_abs = os.path.abspath(str(_pm.get_workspace_dir("s1", "u1")))

    code_root = tmp_path / "installroot"
    secret = code_root / "config" / ".env.production"
    secret.parent.mkdir(parents=True)
    secret.write_text("MASTER_SEED=secret123\n")

    assert not is_within_root(str(secret), workspace_abs)

    try:
        result = tool._normalize_path(str(secret))
    except ServiceError:
        return  # refused outright — also acceptable
    assert is_within_root(result, workspace_abs)
    assert os.path.realpath(result) != os.path.realpath(str(secret))
    if os.path.exists(result):
        assert "MASTER_SEED" not in Path(result).read_text()


def test_cli_local_workspace_is_cwd_by_design():
    """CLI local mode keeps CWD-as-workspace — the consented Claude-Code-style behavior.

    This is NOT a confinement bug: in single-user local mode the operator has
    explicitly opted into the agent editing the current project directory (same
    as Claude Code editing the repo it runs in). The server path (tested above)
    is the isolated default; this exception is documented and tested *as* the
    exception so a later refactor can't silently change it.
    """
    paths = resolve_runtime_paths(local=True)
    assert paths.workspace_root == Path.cwd().resolve()
