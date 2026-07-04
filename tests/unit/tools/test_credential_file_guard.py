"""Secret-file content guard — the local-mode gap the confinement floor can't cover.

Under POLYROB_LOCAL the workspace IS the project cwd (see
``test_cli_local_workspace_is_cwd_by_design``), so a ``config/.env.production`` that
lives *inside* the project is reachable by confinement — the agent read one wholesale
and leaked secrets. These tests lock the content guard: the ``filesystem`` and
``coding`` tools refuse secret-SHAPED files (``.env*``/``*.pem``/``config/.env.*``)
even when they are legitimately in-root, while ordinary project files stay editable.
"""
import logging
import os

import pytest

from core.exceptions import ServiceError


def _fs_tool(tmp_path):
    from tools.filesystem import FileSystem
    t = object.__new__(FileSystem)
    t.logger = logging.getLogger("fs-secret")
    t.name = "filesystem"
    t._enabled = True
    t.session_id = "s1"
    t.user_id = "u1"
    t._current_session_id = None
    t.workspace_dir = str(tmp_path)
    return t


def _coding_tool():
    from tools.coding.tool import CodingTool
    return object.__new__(CodingTool)


# ---------------------------------------------------------------------------
# coding tool _confine — refuse a secret-shaped target that IS inside the root
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("secret_name", [
    ".env",
    ".env.production",
    ".env.local",
    "id_rsa.pem",
])
def test_coding_confine_blocks_in_root_secret_files(tmp_path, secret_name):
    from tools.coding.tool import CodingError
    root = tmp_path / "ws"
    (root / "config").mkdir(parents=True)
    (root / secret_name).write_text("SECRET=1")
    tool = _coding_tool()
    with pytest.raises(CodingError):
        tool._confine(secret_name, str(root))


def test_coding_confine_blocks_in_root_config_env(tmp_path):
    from tools.coding.tool import CodingError
    root = tmp_path / "ws"
    (root / "config").mkdir(parents=True)
    (root / "config" / ".env.production").write_text("MASTER_SEED=x")
    tool = _coding_tool()
    with pytest.raises(CodingError):
        tool._confine("config/.env.production", str(root))


def test_coding_confine_allows_ordinary_project_file(tmp_path):
    """The guard must not break legitimate local project editing (Claude-Code-style)."""
    root = tmp_path / "ws"
    root.mkdir()
    tool = _coding_tool()
    # A normal source file confines fine (returns an absolute in-root path).
    target = tool._confine("src/app.py", str(root))
    assert target.startswith(str(root))


# ---------------------------------------------------------------------------
# filesystem read_file — refuse a secret file that lives inside the workspace
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_filesystem_read_file_refuses_in_workspace_secret(tmp_path, monkeypatch):
    tool = _fs_tool(tmp_path)
    # Neutralize path normalization: return the path as-is inside the workspace so
    # the test exercises the CONTENT guard, not confinement (which local mode
    # deliberately relaxes).
    secret = tmp_path / "config" / ".env.production"
    secret.parent.mkdir(parents=True)
    secret.write_text("ANTHROPIC_API_KEY=sk-should-never-leak\n")
    monkeypatch.setattr(tool, "_normalize_path", lambda p: str(secret))
    monkeypatch.setattr(tool, "ensure_initialized", _anoop, raising=False)

    from tools.filesystem import ReadFileAction
    with pytest.raises(ServiceError):
        await tool.read_file(ReadFileAction(file_path="config/.env.production"))


@pytest.mark.asyncio
async def test_filesystem_write_file_refuses_secret(tmp_path, monkeypatch):
    """Symmetric with read: the agent must not WRITE/tamper a credential file either."""
    tool = _fs_tool(tmp_path)
    secret = tmp_path / ".env"
    monkeypatch.setattr(tool, "_normalize_path", lambda p: str(secret))
    monkeypatch.setattr(tool, "ensure_initialized", _anoop, raising=False)

    from tools.filesystem import WriteFileAction
    with pytest.raises(ServiceError):
        await tool.write_file(WriteFileAction(file_path=".env", content="X=1"))
    assert not secret.exists()  # nothing written


@pytest.mark.asyncio
async def test_filesystem_read_file_allows_ordinary_file(tmp_path, monkeypatch):
    tool = _fs_tool(tmp_path)
    note = tmp_path / "notes.txt"
    note.write_text("hello world\n")
    monkeypatch.setattr(tool, "_normalize_path", lambda p: str(note))
    monkeypatch.setattr(tool, "ensure_initialized", _anoop, raising=False)

    from tools.filesystem import ReadFileAction
    out = await tool.read_file(ReadFileAction(file_path="notes.txt"))
    assert "hello world" in out


async def _anoop(*a, **k):
    return None
