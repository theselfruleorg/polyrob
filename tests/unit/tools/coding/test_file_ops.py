"""P0 Task 6 — coding create/move/delete, confined."""
import logging

import pytest

from tools.coding.tool import (
    CodingTool, CreateFileParams, MoveFileParams, DeleteFileParams,
)


def _tool(root):
    t = object.__new__(CodingTool)
    t.logger = logging.getLogger("coding-test")
    t._root_override = str(root)
    t._backend = None
    return t


@pytest.mark.asyncio
async def test_create_file_writes(tmp_path):
    res = await _tool(tmp_path).create_file(CreateFileParams(file_path="sub/new.py", content="x=1\n"))
    assert res.error is None
    assert (tmp_path / "sub" / "new.py").read_text() == "x=1\n"


@pytest.mark.asyncio
async def test_create_file_refuses_existing_without_overwrite(tmp_path):
    (tmp_path / "a.py").write_text("keep")
    res = await _tool(tmp_path).create_file(CreateFileParams(file_path="a.py", content="new"))
    assert res.error and "exists" in res.error
    assert (tmp_path / "a.py").read_text() == "keep"


@pytest.mark.asyncio
async def test_create_file_escape_refused(tmp_path):
    res = await _tool(tmp_path).create_file(CreateFileParams(file_path="../evil.py", content="x"))
    assert res.error and "escape" in res.error.lower()


@pytest.mark.asyncio
async def test_move_file(tmp_path):
    (tmp_path / "a.py").write_text("body")
    res = await _tool(tmp_path).move_file(MoveFileParams(src_path="a.py", dest_path="b/c.py"))
    assert res.error is None
    assert not (tmp_path / "a.py").exists()
    assert (tmp_path / "b" / "c.py").read_text() == "body"


@pytest.mark.asyncio
async def test_move_file_escape_refused(tmp_path):
    (tmp_path / "a.py").write_text("body")
    res = await _tool(tmp_path).move_file(MoveFileParams(src_path="a.py", dest_path="../out.py"))
    assert res.error and "escape" in res.error.lower()
    assert (tmp_path / "a.py").exists()


@pytest.mark.asyncio
async def test_delete_file(tmp_path):
    (tmp_path / "a.py").write_text("x")
    res = await _tool(tmp_path).delete_file(DeleteFileParams(file_path="a.py"))
    assert res.error is None
    assert not (tmp_path / "a.py").exists()


@pytest.mark.asyncio
async def test_delete_file_escape_refused(tmp_path):
    res = await _tool(tmp_path).delete_file(DeleteFileParams(file_path="../../etc/hosts"))
    assert res.error and "escape" in res.error.lower()
