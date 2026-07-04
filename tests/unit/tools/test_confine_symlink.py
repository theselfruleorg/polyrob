"""Phase 4 (path-concerns upgrade): symlink-safe confinement (G1) + fail-loud (N2).

coding/tool.py _confine and filesystem.py used abspath().startswith(root) — an
in-root symlink pointing outside slipped through. Reuse a realpath+commonpath
helper. filesystem.py also SILENTLY rewrote an escaping path to basename (N2) —
now a loud rejection (gated FS_REALPATH_CONFINE, default on).
"""
import os

import pytest

from core.path_safety import is_within_root


def test_is_within_root_plain(tmp_path):
    root = str(tmp_path)
    assert is_within_root(str(tmp_path / "a" / "b.txt"), root)
    assert not is_within_root(str(tmp_path.parent / "outside.txt"), root)


def test_is_within_root_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    # A path that traverses the in-root symlink resolves outside -> rejected.
    assert not is_within_root(str(root / "link" / "secret.txt"), str(root))


def test_is_within_root_allows_new_file(tmp_path):
    # Non-existent leaf inside root must still be allowed (create case).
    assert is_within_root(str(tmp_path / "newfile.txt"), str(tmp_path))


def _coding_tool():
    from tools.coding.tool import CodingTool
    return object.__new__(CodingTool)  # _confine needs no init state


def test_coding_confine_blocks_symlink_escape(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "out"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    from tools.coding.tool import CodingError

    tool = _coding_tool()
    with pytest.raises(CodingError):
        tool._confine("link/evil.txt", str(root))


def test_coding_confine_blocks_traversal(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    from tools.coding.tool import CodingError

    tool = _coding_tool()
    with pytest.raises(CodingError):
        tool._confine("../escape.txt", str(root))


def test_coding_confine_allows_in_root(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    tool = _coding_tool()
    target = tool._confine("sub/ok.txt", str(root))
    assert target == os.path.abspath(os.path.join(str(root), "sub/ok.txt"))
