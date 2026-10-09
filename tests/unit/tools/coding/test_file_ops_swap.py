"""IO-C1: grep / move_file / delete_file must not follow a path swapped after the check."""
import os
import shutil

import pytest

from tools.coding.tool import CodingTool, DeleteFileParams, GrepParams, MoveFileParams
from tests.unit.tools.coding.test_file_ops import _tool


def _swap_to_outside(ws_dir, outside):
    """Replace a workspace directory with a symlink to *outside* (the race)."""
    shutil.rmtree(ws_dir)
    os.symlink(outside, ws_dir)


@pytest.fixture
def layout(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "f.txt").write_text("inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("SECRET")
    return ws, outside


@pytest.mark.asyncio
async def test_delete_file_does_not_follow_a_swapped_parent(layout, monkeypatch):
    ws, outside = layout
    t = _tool(ws)

    async def swap(*a, **k):
        _swap_to_outside(ws / "sub", outside)
    monkeypatch.setattr(t, "_snapshot_before_edit", swap)
    res = await t.delete_file(DeleteFileParams(file_path="sub/f.txt"))
    assert res.error
    assert (outside / "f.txt").read_text() == "SECRET"


@pytest.mark.asyncio
async def test_move_file_does_not_follow_a_swapped_parent(layout, monkeypatch):
    ws, outside = layout
    t = _tool(ws)

    async def swap(*a, **k):
        _swap_to_outside(ws / "sub", outside)
    monkeypatch.setattr(t, "_snapshot_before_edit", swap)
    res = await t.move_file(MoveFileParams(src_path="sub/f.txt", dest_path="stolen.txt"))
    assert res.error
    assert (outside / "f.txt").read_text() == "SECRET"
    assert not (ws / "stolen.txt").exists()


@pytest.mark.asyncio
async def test_move_file_without_overwrite_keeps_an_existing_dest(tmp_path):
    (tmp_path / "a.py").write_text("a")
    (tmp_path / "b.py").write_text("b")
    res = await _tool(tmp_path).move_file(MoveFileParams(src_path="a.py", dest_path="b.py"))
    assert res.error and "exists" in res.error
    assert (tmp_path / "b.py").read_text() == "b"


@pytest.mark.asyncio
async def test_grep_does_not_read_through_a_parent_swapped_after_the_check(layout, monkeypatch):
    ws, outside = layout
    t = _tool(ws)
    real_confine = CodingTool._confine
    swapped = {"done": False}

    def confine(self, file_path, root, *, write=True):
        out = real_confine(self, file_path, root, write=write)
        if file_path.endswith("f.txt") and not swapped["done"]:
            swapped["done"] = True
            _swap_to_outside(ws / "sub", outside)
        return out
    monkeypatch.setattr(CodingTool, "_confine", confine)
    res = await t.grep(GrepParams(pattern="SECRET", path="sub"))
    assert "SECRET" not in (res.extracted_content or "")
