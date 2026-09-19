"""A whole-file read of an over-cap MULTI-LINE file returns a bounded TAIL, not an error.

Prod evidence (2026-09-17, 6 h window): `kb-root-position-ledger.md` — an
append-only ledger at ~92k tokens — was read 84 times and REFUSED 15 times
("exceeds maximum allowed tokens ... use offset and limit"). Every refusal is a
paid step whose only output is the instruction to try again; the agent then
recovers with offset/limit and continues. The tail of an append-only file is
what a reconcile needs, so the read returns it directly, with an explicit
header that names the truncation and the offset/limit shape for the rest.

The JSON/dense branch (few lines, many tokens) keeps refusing — a line window
does not help there, and a silent partial JSON document would be worse than an
error.
"""
import logging

import pytest

from tools.filesystem import FileSystem, ReadFileAction


def _fs_tool():
    t = object.__new__(FileSystem)
    t.logger = logging.getLogger("fs-read-overcap")
    t.session_id = "s1"
    t.user_id = "u1"
    t._current_session_id = None
    t._enabled = True
    return t


@pytest.fixture
def _pm(tmp_path):
    from agents.task.path import PathManager, set_path_manager
    pm = PathManager(data_root=str(tmp_path / "data"))
    set_path_manager(pm)
    return pm


async def _noop():
    return None


@pytest.fixture
def _tool(_pm, monkeypatch):
    monkeypatch.setattr(FileSystem, "ensure_initialized", lambda self: _noop(), raising=False)
    return _fs_tool()


async def _write_raw(tool, name, text):
    import os
    path = tool._normalize_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    return path


def _big_ledger(n_lines=3000):
    # ~50 chars/line → ~150k chars → ~37k estimated tokens, over the 25k cap.
    return "".join(f"| 2026-09-{(i % 28) + 1:02d} | entry {i:05d} | note {'x' * 30} |\n"
                   for i in range(n_lines))


@pytest.mark.asyncio
async def test_overcap_multiline_read_returns_numbered_tail(_tool):
    text = _big_ledger()
    total = text.count("\n")
    await _write_raw(_tool, "ledger.md", text)
    out = await _tool.read_file(ReadFileAction(filePath="ledger.md"))
    # 1) it is a result, not a refusal, and it says so honestly up front
    head = out.splitlines()[0]
    assert "TRUNCATED" in head and f"{total}" in head and "offset" in head
    # 2) the tail is the newest content, numbered like the offset/limit branch
    assert "entry 02999" in out
    assert "entry 00000" not in out
    last = out.rstrip("\n").splitlines()[-1]
    assert last.startswith(f"{total:6}|")
    # 3) bounded: well under the cap (~4 chars/token)
    assert len(out) // 4 <= 25000


@pytest.mark.asyncio
async def test_overcap_tail_header_names_how_to_read_the_rest(_tool):
    text = _big_ledger()
    await _write_raw(_tool, "ledger.md", text)
    out = await _tool.read_file(ReadFileAction(filePath="ledger.md"))
    head = out.splitlines()[0]
    assert '"offset": 1' in head and '"limit"' in head


@pytest.mark.asyncio
async def test_overcap_dense_json_still_refuses(_tool):
    from core.exceptions import ServiceError
    await _write_raw(_tool, "blob.json", '{"k": "' + "v" * 120_000 + '"}\n')
    with pytest.raises(ServiceError, match="char_offset"):
        await _tool.read_file(ReadFileAction(filePath="blob.json"))


@pytest.mark.asyncio
async def test_under_cap_read_is_untouched(_tool):
    text = "a\nb\nc\n"
    await _write_raw(_tool, "small.md", text)
    assert await _tool.read_file(ReadFileAction(filePath="small.md")) == text
