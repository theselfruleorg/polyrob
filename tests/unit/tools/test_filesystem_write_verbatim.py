"""F9 (live-test): write_file/append_file must persist content VERBATIM.

The agent sent correctly-indented Python to filesystem_write_file, but _clean_text
collapsed horizontal whitespace and stripped every line → the file on disk was
unindented (IndentationError). A write must preserve exactly what was requested.
"""
import ast
import logging

import pytest

from tools.filesystem import FileSystem, WriteFileAction, AppendFileAction


def _fs_tool():
    t = object.__new__(FileSystem)
    t.logger = logging.getLogger("fs-verbatim")
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


_CODE = 'def add(a, b):\n    """sum."""\n    return a + b\n\n\nclass C:\n    def m(self):\n        return 1\n'


@pytest.mark.asyncio
async def test_write_file_preserves_indentation_and_compiles(_pm, monkeypatch):
    monkeypatch.setattr(FileSystem, "ensure_initialized", lambda self: _noop(), raising=False)
    t = _fs_tool()
    await t.write_file(WriteFileAction(file_path="m.py", content=_CODE))
    written = open(t._normalize_path("m.py")).read()
    assert written == _CODE                     # byte-for-byte
    ast.parse(written)                          # compiles — no IndentationError
    assert "\n    return a + b" in written      # indentation intact


@pytest.mark.asyncio
async def test_append_file_preserves_indentation(_pm, monkeypatch):
    monkeypatch.setattr(FileSystem, "ensure_initialized", lambda self: _noop(), raising=False)
    t = _fs_tool()
    await t.append_file(AppendFileAction(file_path="a.py", content=_CODE))
    written = open(t._normalize_path("a.py")).read()
    assert "\n    return a + b" in written


async def _noop():
    return None


def test_write_file_action_coerces_dict_content_to_json():
    """LLMs routinely pass structured content (a dict) when writing JSON-ish
    files like package.json instead of pre-serializing it themselves. Before
    this fix, Pydantic's strict string_type check rejected the dict outright
    (the field_validator never got a chance to run), so every such write_file
    call failed validation and the agent looped."""
    a = WriteFileAction(file_path="package.json", content={"name": "foo", "version": "1.0.0"})
    assert a.content == '{\n  "name": "foo",\n  "version": "1.0.0"\n}'


def test_append_file_action_coerces_list_content_to_json():
    a = AppendFileAction(file_path="data.json", content=[1, 2, 3])
    assert a.content == "[\n  1,\n  2,\n  3\n]"
