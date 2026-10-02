"""H09/H11/H12 (security analysis 2026-09-23) through the filesystem tool."""
import logging
import os

import pytest

from core.exceptions import ServiceError


async def _anoop(*a, **k):
    return None


def _fs(tmp_path, monkeypatch, ws):
    from tools.filesystem import FileSystem
    t = object.__new__(FileSystem)
    t.logger = logging.getLogger("fs-deny")
    t.name = "filesystem"
    t._enabled = True
    t.session_id = "s1"
    t.user_id = None
    t._current_session_id = None
    t._workspace_root = lambda: str(ws)
    t._normalize_path = lambda p: os.path.join(str(ws), p)
    monkeypatch.setattr(t, "ensure_initialized", _anoop, raising=False)
    monkeypatch.setattr("asyncio.sleep", _anoop)
    return t


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "far-home"))


@pytest.mark.asyncio
@pytest.mark.parametrize("rel", [".git/config", ".git/hooks/post-checkout",
                                 ".agents/skills/x/SKILL.md", ".claude/skills/y/SKILL.md",
                                 "AGENTS.md", "CLAUDE.md"])
async def test_write_refused(tmp_path, monkeypatch, rel):
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = _fs(tmp_path, monkeypatch, ws)
    from tools.controller.views import WriteFileAction
    with pytest.raises(ServiceError, match="Refusing"):
        await tool.write_file(WriteFileAction(file_path=rel, content="pwn"))
    assert not (ws / rel).exists()


@pytest.mark.asyncio
async def test_delete_and_copy_guarded(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    (ws / ".git" / "config").write_text("[core]\n")
    (ws / ".env").write_text("KEY=1\n")
    tool = _fs(tmp_path, monkeypatch, ws)
    from tools.controller.views import CopyFileAction, DeleteFileAction
    with pytest.raises(ServiceError):
        await tool.delete_file(DeleteFileAction(file_path=".git/config"))
    assert (ws / ".git" / "config").exists()
    with pytest.raises(ServiceError):
        await tool.copy_file(CopyFileAction(source_path=".env", dest_path="notes.txt"))
    assert not (ws / "notes.txt").exists()


@pytest.mark.asyncio
async def test_local_data_home_refused(tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    home = ws / ".polyrob"
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / "message_history.json").write_text("[]")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    tool = _fs(tmp_path, monkeypatch, ws)
    from tools.controller.views import ReadFileAction, WriteFileAction
    with pytest.raises(ServiceError, match="data home"):
        await tool.read_file(ReadFileAction(file_path=".polyrob/sessions/message_history.json"))
    with pytest.raises(ServiceError, match="data home"):
        await tool.write_file(WriteFileAction(
            file_path=".polyrob/sessions/message_history.json", content="[]"))


@pytest.mark.asyncio
async def test_write_over_planted_symlink_does_not_touch_target(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "host.txt"
    outside.write_text("host")
    (ws / "report.md").symlink_to(outside)
    tool = _fs(tmp_path, monkeypatch, ws)
    from tools.controller.views import WriteFileAction
    # _normalize_path is stubbed here (no realpath confinement), so this is the
    # race window itself: the I/O layer must replace the ENTRY, never follow it.
    await tool.write_file(WriteFileAction(file_path="report.md", content="x"))
    assert outside.read_text() == "host"
    assert not (ws / "report.md").is_symlink()


@pytest.mark.asyncio
async def test_no_predictable_temp_left_and_write_works(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = _fs(tmp_path, monkeypatch, ws)
    from tools.controller.views import AppendFileAction, WriteFileAction
    await tool.write_file(WriteFileAction(file_path="a.txt", content="one"))
    await tool.append_file(AppendFileAction(file_path="a.txt", content="two"))
    assert (ws / "a.txt").read_text() == "one\ntwo"
    assert sorted(os.listdir(ws)) == ["a.txt"]


@pytest.mark.asyncio
async def test_jsonl_append_refuses_symlink(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    other = ws / "real.jsonl"
    other.write_text("")
    (ws / "log.jsonl").symlink_to(other)
    tool = _fs(tmp_path, monkeypatch, ws)
    from tools.controller.views import JsonlAppendAction
    with pytest.raises(ServiceError):
        await tool.jsonl_append(JsonlAppendAction(file_path="log.jsonl", record={"a": 1}))
    assert other.read_text() == ""
