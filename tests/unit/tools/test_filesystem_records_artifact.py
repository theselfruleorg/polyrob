"""A file the agent writes gets an artifact row at WRITE time, not a guess later.

`collect_artifacts` used to reconstruct a run's outputs from a time-windowed
workspace scan. On the shared project-root workspace that scan sees every OTHER
run's files too, and after the 2026-08-17 wipe it saw nothing at all. Recording
at the write choke point makes attribution a fact instead of an inference.
"""
import logging

import pytest

from core.artifacts import get_artifact_ledger
from tools.filesystem import FileSystem, WriteFileAction


async def _noop():
    return None


def _fs_tool(user_id="rob", session_id="s1"):
    t = object.__new__(FileSystem)
    t.logger = logging.getLogger("fs-artifacts")
    t.session_id = session_id
    t.user_id = user_id
    t._current_session_id = None
    t._enabled = True
    return t


@pytest.fixture
def _pm(tmp_path):
    from agents.task.path import PathManager, set_path_manager
    pm = PathManager(data_root=str(tmp_path / "data"))
    set_path_manager(pm)
    return pm


@pytest.mark.asyncio
async def test_write_file_records_an_artifact(_pm, monkeypatch):
    monkeypatch.setattr(FileSystem, "ensure_initialized", lambda self: _noop(), raising=False)
    t = _fs_tool()

    await t.write_file(WriteFileAction(file_path="report.md", content="# findings\n"))

    rows = get_artifact_ledger().list_for_session("rob", "s1")
    assert len(rows) == 1
    assert rows[0].path.endswith("report.md")
    assert rows[0].bytes == len("# findings\n")
    assert get_artifact_ledger().verify(rows[0].id, "rob") == "ok"


@pytest.mark.asyncio
async def test_recorded_artifact_is_scoped_to_the_writing_tenant(_pm, monkeypatch):
    monkeypatch.setattr(FileSystem, "ensure_initialized", lambda self: _noop(), raising=False)
    await _fs_tool(user_id="rob").write_file(
        WriteFileAction(file_path="mine.md", content="x"))

    assert get_artifact_ledger().list_for_session("someone_else", "s1") == []


@pytest.mark.asyncio
async def test_html_is_recorded_as_a_page(_pm, monkeypatch):
    """Kind drives what the ship rail may do with it, so it is set at write time."""
    monkeypatch.setattr(FileSystem, "ensure_initialized", lambda self: _noop(), raising=False)
    t = _fs_tool()

    await t.write_file(WriteFileAction(file_path="map.html", content="<html>ok</html>"))

    rows = get_artifact_ledger().list_for_session("rob", "s1")
    assert rows[0].kind == "page"


@pytest.mark.asyncio
async def test_a_failed_write_records_nothing(_pm, monkeypatch):
    monkeypatch.setattr(FileSystem, "ensure_initialized", lambda self: _noop(), raising=False)
    t = _fs_tool()

    with pytest.raises(Exception):
        await t.write_file(WriteFileAction(file_path="../../escape.md", content="x"))

    assert get_artifact_ledger().list_for_session("rob", "s1") == []
