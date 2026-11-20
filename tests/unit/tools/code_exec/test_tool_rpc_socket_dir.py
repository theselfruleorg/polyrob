"""Audit 2026-11-21 L-26: the tool RPC socket dir gives the data group no write
right, so a group member cannot swap the socket inode for its own."""
import os
import stat
import tempfile
from pathlib import Path

import pytest

import core.runtime_paths as rp
from tools.code_exec import tool_rpc


@pytest.fixture
def short_home(monkeypatch):
    home = tempfile.mkdtemp(prefix="h", dir="/tmp")   # short, like /var/lib/polyrob
    os.chmod(home, 0o2770)
    monkeypatch.setattr(rp, "effective_data_home", lambda: Path(home))
    old = os.umask(0o002)                              # the units' UMask
    yield home
    os.umask(old)
    import shutil
    shutil.rmtree(home, ignore_errors=True)


def test_the_socket_dir_has_no_group_or_other_write(short_home):
    d = tool_rpc.make_socket_dir(owner="1000:1000")
    assert d.startswith(short_home)
    assert not stat.S_IMODE(os.stat(d).st_mode) & 0o022


def test_as_root_for_a_forced_uid_the_dir_is_traverse_only(short_home, monkeypatch):
    monkeypatch.setattr(tool_rpc.os, "geteuid", lambda: 0)
    d = tool_rpc.make_socket_dir(owner="65534:65534")
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o711


def test_an_entry_planted_before_the_narrowing_refuses(short_home, monkeypatch):
    real = rp.sandbox_scratch_dir

    def planted(prefix, **kw):
        path = real(prefix, **kw)
        Path(path, "x.sock").write_text("")
        return path
    monkeypatch.setattr(rp, "sandbox_scratch_dir", planted)
    with pytest.raises(OSError, match="not empty"):
        tool_rpc.make_socket_dir(owner="1000:1000")
