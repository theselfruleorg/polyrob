"""read_file must return the file's content VERBATIM — the twin of F9's write fix.

A full-file read piped its content through ``_clean_text``, which collapses
horizontal whitespace and strips every line. That is right for prose extracted
from a PDF and catastrophic for anything indentation-carrying: Python, YAML,
nested Markdown. The agent read its own source and got back code that no longer
compiles, then edited from that corrupted copy.

The WRITE path was fixed for exactly this reason (test_filesystem_write_verbatim,
F9). The READ path was missed — same helper, same damage, opposite direction.
Read and write must round-trip.

Only the whole-file read was affected: the offset/limit and char_offset branches
return before the cleaner and were always verbatim.
"""
import ast
import logging

import pytest

from tools.filesystem import FileSystem, ReadFileAction, WriteFileAction


def _fs_tool():
    t = object.__new__(FileSystem)
    t.logger = logging.getLogger("fs-read-verbatim")
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


_CODE = (
    '#!/usr/bin/env python3\n'
    '"""A module with real indentation."""\n'
    '\n'
    '\n'
    'def probe(url, timeout=12):\n'
    '    if not url:\n'
    '        raise ValueError("url required")\n'
    '    result = {\n'
    '        "url": url,\n'
    '        "timeout": timeout,\n'
    '    }\n'
    '    return result\n'
)

_YAML = 'services:\n  web:\n    image: nginx\n    ports:\n      - "80:80"\n'


@pytest.fixture
def _tool(_pm, monkeypatch):
    monkeypatch.setattr(FileSystem, "ensure_initialized", lambda self: _noop(), raising=False)
    return _fs_tool()


async def _write_raw(tool, name, text):
    path = tool._normalize_path(name)
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    return path


@pytest.mark.asyncio
async def test_read_python_source_round_trips_byte_for_byte(_tool):
    await _write_raw(_tool, "probe.py", _CODE)

    out = await _tool.read_file(ReadFileAction(file_path="probe.py"))

    assert out == _CODE


@pytest.mark.asyncio
async def test_read_python_source_still_compiles(_tool):
    """The failure that actually bit: read-back source raised IndentationError."""
    await _write_raw(_tool, "probe.py", _CODE)

    out = await _tool.read_file(ReadFileAction(file_path="probe.py"))

    ast.parse(out)
    assert "\n    if not url:" in out


@pytest.mark.asyncio
async def test_read_yaml_indentation_survives(_tool):
    await _write_raw(_tool, "compose.yml", _YAML)

    out = await _tool.read_file(ReadFileAction(file_path="compose.yml"))

    assert out == _YAML
    assert "\n  web:" in out


@pytest.mark.asyncio
async def test_blank_line_runs_are_not_collapsed(_tool):
    text = "a\n\n\n\nb\n"
    await _write_raw(_tool, "spaced.txt", text)

    assert await _tool.read_file(ReadFileAction(file_path="spaced.txt")) == text


@pytest.mark.asyncio
async def test_trailing_newline_is_preserved(_tool):
    """`.strip()` used to eat it, so an edit-and-rewrite silently changed the file."""
    await _write_raw(_tool, "t.txt", "one line\n")

    assert await _tool.read_file(ReadFileAction(file_path="t.txt")) == "one line\n"


@pytest.mark.asyncio
async def test_write_then_read_round_trips(_tool):
    """The invariant: what the agent wrote is what the agent reads back."""
    await _tool.write_file(WriteFileAction(file_path="rt.py", content=_CODE))

    assert await _tool.read_file(ReadFileAction(file_path="rt.py")) == _CODE
