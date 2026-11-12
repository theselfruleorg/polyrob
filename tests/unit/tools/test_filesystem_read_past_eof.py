"""A paged read that walks past the end of the file is an honest EOF, not a crash.

Observed on prod 2026-09-20 22:41Z (and 2026-09-01): a run paging through its own
report asked for ``offset=1040`` of a 1031-line file. The raise became a
RuntimeError chain, four tracebacks in the journal and a FAILED step the model
had to recover from — for a question ("is there more?") whose answer is "no".
The page header now also names the total on every explicit window, so the
model can see where the end is before it asks past it.
"""
import logging
import os

import pytest

from tools.filesystem import FileSystem, ReadFileAction


def _fs_tool():
    t = object.__new__(FileSystem)
    t.logger = logging.getLogger("fs-eof")
    t.session_id = "s1"
    t.user_id = "u1"
    t._current_session_id = None
    t._enabled = True
    return t


async def _noop():
    return None


@pytest.fixture
def _pm(tmp_path):
    from agents.task.path import PathManager, set_path_manager
    pm = PathManager(data_root=str(tmp_path / "data"))
    set_path_manager(pm)
    return pm


@pytest.fixture
def tool(_pm, monkeypatch):
    monkeypatch.setattr(FileSystem, "ensure_initialized", lambda self: _noop(), raising=False)
    monkeypatch.setattr(FileSystem, "_record_artifact", lambda self, p: None, raising=False)
    monkeypatch.delenv("AUTONOMOUS_READ_PAGE_LINES", raising=False)
    return _fs_tool()


async def _write_raw(tool, name, text):
    path = tool._normalize_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    return path


_TEN = "".join(f"line {i}\n" for i in range(1, 11))


@pytest.mark.asyncio
async def test_offset_past_eof_is_an_honest_result_not_an_error(tool):
    await _write_raw(tool, "r.md", _TEN)
    out = await tool.read_file(ReadFileAction(filePath="r.md", offset=1040, limit=400))
    assert out.startswith("[EOF"), out
    assert "10 lines" in out and "1040" in out
    assert "line 1" not in out, "no content rides on an EOF answer"


@pytest.mark.asyncio
async def test_offset_exactly_one_past_last_line_is_eof(tool):
    await _write_raw(tool, "r.md", _TEN)
    out = await tool.read_file(ReadFileAction(filePath="r.md", offset=11, limit=5))
    assert out.startswith("[EOF")


@pytest.mark.asyncio
async def test_explicit_window_names_the_total(tool):
    await _write_raw(tool, "r.md", _TEN)
    out = await tool.read_file(ReadFileAction(filePath="r.md", offset=3, limit=4))
    first, body = out.split("\n", 1)
    assert first == "[lines 3-6 of 10]"
    assert "line 3\n" in body and "line 6\n" in body and "line 7\n" not in body


@pytest.mark.asyncio
async def test_last_page_says_it_reached_the_end(tool):
    await _write_raw(tool, "r.md", _TEN)
    out = await tool.read_file(ReadFileAction(filePath="r.md", offset=9, limit=400))
    first = out.split("\n", 1)[0]
    assert first == "[lines 9-10 of 10 — end of file]"


@pytest.mark.asyncio
async def test_char_offset_past_eof_is_honest_too(tool):
    await _write_raw(tool, "r.md", _TEN)
    out = await tool.read_file(ReadFileAction(filePath="r.md", char_offset=99999, char_limit=10))
    assert out.startswith("[EOF")
    assert f"{len(_TEN)} chars" in out


@pytest.mark.asyncio
async def test_negative_offset_is_still_refused(tool):
    # A malformed window is still an error; only "past the end" is a result.
    await _write_raw(tool, "r.md", _TEN)
    with pytest.raises(Exception):
        await tool.read_file(ReadFileAction(filePath="r.md", offset=-1, limit=1))


@pytest.mark.asyncio
async def test_a_saved_tool_result_is_read_in_slices(tool):
    """116 W1.2: an unpaged read of a saved result returns one slice and the
    next offset; a whole read was over the wire cap and was saved again."""
    from core.tool_results import READ_SLICE_CHARS, relative_path
    rel = relative_path("s1", "big.txt")
    await _write_raw(tool, rel, "a" * (READ_SLICE_CHARS + 500))
    out = await tool.read_file(ReadFileAction(filePath=rel))
    head, body = out.split("\n", 1)
    assert len(body) == READ_SLICE_CHARS and f"char_offset={READ_SLICE_CHARS}" in head
    out = await tool.read_file(ReadFileAction(filePath=rel, char_offset=READ_SLICE_CHARS,
                                              char_limit=10 ** 9))
    assert out.split("\n", 1)[1] == "a" * 500 and "end of the saved result" in out
