"""`filesystem.copy_file` (2026-09-19) — a workspace-confined copy primitive.

Prod evidence: goal 1a5a313bfe9f (store dedupe) declared BLOCKED with "need a
file-copy primitive" — the rule was "back up targets.jsonl before rewriting it",
the store is 230 KB, and the only way to copy was read_file → create_file, which
cannot carry a file that size through the model's context. A copy is a byte
operation, not a model operation. Same confinement as every other filesystem
verb (both paths through `_normalize_path`), never overwrites unless asked,
never copies a directory, and reports what it did in bytes.
"""
import logging
import os

import pytest

from core.exceptions import ServiceError


def _fs_tool():
    from tools.filesystem import FileSystem
    t = object.__new__(FileSystem)
    t.logger = logging.getLogger("fs-copy")
    t.session_id = "s1"
    t.user_id = "u1"
    t._current_session_id = None
    t._enabled = True
    t._initialized = True
    return t


@pytest.fixture
def _pm(tmp_path):
    from agents.task.path import PathManager, set_path_manager
    pm = PathManager(data_root=str(tmp_path / "data"))
    set_path_manager(pm)
    return pm


def _ws(_pm):
    ws = _pm.get_workspace_dir("s1", "u1")
    os.makedirs(ws, exist_ok=True)
    return str(ws)


@pytest.mark.asyncio
async def test_copy_file_copies_bytes_and_reports_size(_pm, monkeypatch):
    monkeypatch.setenv("FS_REALPATH_CONFINE", "on")
    from tools.controller.views import CopyFileAction
    ws = _ws(_pm)
    payload = ("{\"a\":1}\n" * 5000).encode()  # ~40 KB, well past any read cap
    with open(os.path.join(ws, "store.jsonl"), "wb") as f:
        f.write(payload)
    tool = _fs_tool()
    out = await tool.copy_file(CopyFileAction(source_path="store.jsonl",
                                              dest_path="store.jsonl.bak"))
    assert "store.jsonl.bak" in out and str(len(payload)) in out
    with open(os.path.join(ws, "store.jsonl.bak"), "rb") as f:
        assert f.read() == payload


@pytest.mark.asyncio
async def test_copy_file_refuses_to_overwrite_unless_asked(_pm, monkeypatch):
    monkeypatch.setenv("FS_REALPATH_CONFINE", "on")
    from tools.controller.views import CopyFileAction
    ws = _ws(_pm)
    open(os.path.join(ws, "a.txt"), "w").write("new")
    open(os.path.join(ws, "b.txt"), "w").write("old")
    tool = _fs_tool()
    with pytest.raises(ServiceError):
        await tool.copy_file(CopyFileAction(source_path="a.txt", dest_path="b.txt"))
    assert open(os.path.join(ws, "b.txt")).read() == "old"
    await tool.copy_file(CopyFileAction(source_path="a.txt", dest_path="b.txt",
                                        overwrite=True))
    assert open(os.path.join(ws, "b.txt")).read() == "new"


@pytest.mark.asyncio
async def test_copy_file_is_confined_and_file_only(_pm, tmp_path, monkeypatch):
    monkeypatch.setenv("FS_REALPATH_CONFINE", "on")
    from tools.controller.views import CopyFileAction
    ws = _ws(_pm)
    open(os.path.join(ws, "a.txt"), "w").write("x")
    os.makedirs(os.path.join(ws, "sub"))
    tool = _fs_tool()
    with pytest.raises(ServiceError):
        await tool.copy_file(CopyFileAction(
            source_path="../../../../../../config/.env.production", dest_path="leak.txt"))
    with pytest.raises(ServiceError):
        await tool.copy_file(CopyFileAction(source_path="sub", dest_path="sub2"))
    with pytest.raises(ServiceError):
        await tool.copy_file(CopyFileAction(source_path="missing.txt", dest_path="c.txt"))
