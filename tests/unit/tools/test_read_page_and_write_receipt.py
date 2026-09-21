"""057 WS-B: a default read page, and write receipts that name bytes not content."""
import hashlib
import json
import logging
import os

import pytest

from tools.filesystem import FileSystem, ReadFileAction, WriteFileAction, AppendFileAction


def _fs_tool():
    t = object.__new__(FileSystem)
    t.logger = logging.getLogger("fs-ws-b")
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


_BIG = "".join(f"line {i}\n" for i in range(1, 1001))


@pytest.mark.asyncio
async def test_unpaged_read_is_whole_file_by_default(tool):
    await _write_raw(tool, "big.txt", _BIG)
    out = await tool.read_file(ReadFileAction(filePath="big.txt"))
    assert out == _BIG, "default must stay byte-identical"


@pytest.mark.asyncio
async def test_default_page_applies_and_names_itself(tool, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_READ_PAGE_LINES", "400")
    await _write_raw(tool, "big.txt", _BIG)
    out = await tool.read_file(ReadFileAction(filePath="big.txt"))
    assert out.startswith("[lines 1-400 of 1000")
    assert "offset/limit" in out.splitlines()[0], "the way out must be named"
    assert "line 400\n" in out and "line 401\n" not in out


@pytest.mark.asyncio
async def test_an_explicit_window_always_wins(tool, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_READ_PAGE_LINES", "5")
    await _write_raw(tool, "big.txt", _BIG)
    out = await tool.read_file(ReadFileAction(filePath="big.txt", offset=10, limit=3))
    assert not out.startswith("[lines 1-5")
    assert "line 10" in out and "line 13" not in out


@pytest.mark.asyncio
async def test_a_short_file_is_untouched_by_the_page(tool, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_READ_PAGE_LINES", "400")
    await _write_raw(tool, "small.txt", "a\nb\nc\n")
    assert await tool.read_file(ReadFileAction(filePath="small.txt")) == "a\nb\nc\n"


@pytest.mark.asyncio
async def test_write_result_reports_bytes_and_digest_never_content(tool):
    body = "hello world\n" * 50
    res = await tool.write_file(WriteFileAction(filePath="out.txt", content=body))
    payload = json.loads(res if isinstance(res, str) else res.extracted_content)
    v = payload["verification"]
    assert v["size_bytes"] == len(body.encode())
    assert v["sha256"] == hashlib.sha256(body.encode()).hexdigest()[:16]
    assert body not in json.dumps(payload), "a write result must never echo the content"


@pytest.mark.asyncio
async def test_json_write_also_carries_bytes_and_digest(tool):
    body = json.dumps([{"a": 1}, {"a": 2}])
    res = await tool.write_file(WriteFileAction(filePath="out.json", content=body))
    payload = json.loads(res if isinstance(res, str) else res.extracted_content)
    v = payload["verification"]
    assert v["item_count"] == 2          # the JSON branch's own fact, preserved
    assert v["size_bytes"] == len(body.encode())
    assert "sha256" in v


@pytest.mark.asyncio
async def test_append_result_names_what_landed(tool):
    await tool.write_file(WriteFileAction(filePath="log.txt", content="first\n"))
    added = "second\n"
    out = await tool.append_file(AppendFileAction(filePath="log.txt", content=added))
    assert "log.txt" in out
    assert f"{len(added.encode())} bytes" in out
    assert hashlib.sha256(added.encode()).hexdigest()[:16] in out
    assert added.strip() not in out.replace("log.txt", ""), "never echo the content"
