"""Coding-agent review B4 (2026-09-24): an edit keeps the file's line endings,
BOM and mode, replaces it atomically, and refuses binary cleanly."""
import logging
import os
import stat

import pytest

from tools.coding.textio import NotTextError, read_text, write_text
from tools.coding.tool import (ApplyPatchParams, CodingTool, CreateFileParams,
                               StrReplaceParams)


def _tool(root):
    t = object.__new__(CodingTool)
    t.logger = logging.getLogger("coding-test")
    t._root_override = str(root)
    t._backend = None
    return t


@pytest.mark.asyncio
async def test_str_replace_keeps_crlf(tmp_path):
    f = tmp_path / "win.txt"
    f.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    res = await _tool(tmp_path).str_replace(StrReplaceParams(
        file_path="win.txt", old_string="beta\ngamma", new_string="BETA\nGAMMA"))
    assert not res.error, res.error
    assert f.read_bytes() == b"alpha\r\nBETA\r\nGAMMA\r\n"


@pytest.mark.asyncio
async def test_str_replace_accepts_crlf_fragments_on_an_lf_file(tmp_path):
    f = tmp_path / "unix.txt"
    f.write_bytes(b"a\nb\n")
    res = await _tool(tmp_path).str_replace(StrReplaceParams(
        file_path="unix.txt", old_string="a\r\nb", new_string="A\r\nB"))
    assert not res.error, res.error
    assert f.read_bytes() == b"A\nB\n"


@pytest.mark.asyncio
async def test_mixed_line_endings_are_left_alone(tmp_path):
    f = tmp_path / "mixed.txt"
    f.write_bytes(b"one\r\ntwo\nthree\r\n")
    res = await _tool(tmp_path).str_replace(StrReplaceParams(
        file_path="mixed.txt", old_string="two", new_string="TWO"))
    assert not res.error, res.error
    assert f.read_bytes() == b"one\r\nTWO\nthree\r\n"


@pytest.mark.asyncio
async def test_apply_patch_keeps_crlf_and_bom(tmp_path):
    f = tmp_path / "bom.py"
    f.write_bytes("﻿x = 1\r\ny = 2\r\n".encode("utf-8"))
    patch = "@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 3\n"
    res = await _tool(tmp_path).apply_patch(ApplyPatchParams(file_path="bom.py", patch=patch))
    assert not res.error, res.error
    assert f.read_bytes() == "﻿x = 1\r\ny = 3\r\n".encode("utf-8")


@pytest.mark.asyncio
async def test_binary_is_refused_with_a_clear_error(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x89PNG\x00\x01\x02")
    res = await _tool(tmp_path).str_replace(StrReplaceParams(
        file_path="blob.bin", old_string="PNG", new_string="X"))
    assert res.error and "binary" in res.error
    (tmp_path / "latin.txt").write_bytes("caf\xe9".encode("latin-1"))
    res = await _tool(tmp_path).str_replace(StrReplaceParams(
        file_path="latin.txt", old_string="caf", new_string="x"))
    assert res.error and "not UTF-8" in res.error


@pytest.mark.asyncio
async def test_edit_keeps_mode_and_writes_through_a_symlink(tmp_path):
    real = tmp_path / "run.sh"
    real.write_text("echo old\n")
    os.chmod(real, 0o755)
    link = tmp_path / "link.sh"
    link.symlink_to(real)
    res = await _tool(tmp_path).str_replace(StrReplaceParams(
        file_path="link.sh", old_string="old", new_string="new"))
    assert not res.error, res.error
    assert link.is_symlink()
    assert real.read_text() == "echo new\n"
    assert stat.S_IMODE(os.stat(real).st_mode) == 0o755
    assert not [p for p in os.listdir(tmp_path) if p.startswith(".polyrob-edit-")]


@pytest.mark.asyncio
async def test_create_file_is_atomic_and_readable(tmp_path):
    res = await _tool(tmp_path).create_file(CreateFileParams(file_path="n/new.txt", content="hi\n"))
    assert not res.error, res.error
    p = tmp_path / "n" / "new.txt"
    assert p.read_text() == "hi\n"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o644


def test_a_failed_write_leaves_the_original(tmp_path, monkeypatch):
    f = tmp_path / "keep.txt"
    f.write_text("original\n")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_text(str(f), "new\n")
    assert f.read_text() == "original\n"
    assert not [p for p in os.listdir(tmp_path) if p.startswith(".polyrob-edit-")]


def test_read_text_reports_the_shape(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"x\r\ny\r\n")
    t = read_text(str(f))
    assert (t.content, t.eol, t.bom) == ("x\ny\n", "\r\n", False)
    f.write_bytes(b"\x00\x00")
    with pytest.raises(NotTextError):
        read_text(str(f))


# --- codex review 2026-09-25 follow-ups ---------------------------------------

@pytest.mark.asyncio
async def test_a_symlink_swapped_in_during_the_snapshot_cannot_escape(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n")
    target = ws / "f.txt"
    target.write_text("old\n")
    tool = _tool(ws)

    async def swap(*a, **k):  # the awaited snapshot is the race window
        target.unlink()
        target.symlink_to(outside)
    tool._snapshot_before_edit = swap
    res = await tool.str_replace(StrReplaceParams(file_path="f.txt", old_string="old", new_string="new"))
    assert res.error and "outside the workspace" in res.error
    assert outside.read_text() == "keep\n"


def test_a_read_only_file_is_refused(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root bypasses file modes")
    f = tmp_path / "ro.txt"
    f.write_text("x\n")
    os.chmod(f, 0o444)
    with pytest.raises(PermissionError, match="read-only"):
        write_text(str(f), "y\n")
    assert f.read_text() == "x\n"


def test_a_hard_link_keeps_its_identity(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("one\n")
    b = tmp_path / "b.txt"
    os.link(a, b)
    write_text(str(a), "two\n")
    assert b.read_text() == "two\n"
    assert os.stat(a).st_ino == os.stat(b).st_ino
