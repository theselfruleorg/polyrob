"""core.fs_copy — copies and moves that work inside the hardened service.

The sandbox is simulated: a chmod carrying setuid/setgid raises EPERM (what
RestrictSUIDSGID=true does), and a rename raises EXDEV (two bind mounts). The
real thing was reproduced on a box with systemd-run under the unit's properties.
"""
import errno
import os
import shutil
import stat

import pytest

from core import fs_copy


@pytest.fixture
def sandbox(monkeypatch):
    real_chmod = os.chmod

    def chmod(path, mode, *a, **kw):
        if mode & (stat.S_ISUID | stat.S_ISGID):
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
        return real_chmod(path, mode, *a, **kw)
    monkeypatch.setattr(os, "chmod", chmod)


def _setgid_tree(root):
    src = root / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "f.txt").write_text("x")
    (src / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(src / "run.sh", 0o755)
    # what a data-home directory looks like (set before the sandbox patch)
    for d in (src, src / "sub"):
        os.chmod(d, 0o2770)
    return src


def test_shutil_copytree_fails_in_the_sandbox_and_the_seam_does_not(tmp_path, monkeypatch):
    src = _setgid_tree(tmp_path)
    if not os.stat(src).st_mode & stat.S_ISGID:
        pytest.skip("this filesystem drops the setgid bit")
    real_chmod = os.chmod
    monkeypatch.setattr(os, "chmod", lambda p, m, *a, **k: (
        (_ for _ in ()).throw(PermissionError(errno.EPERM, "EPERM")) if m & stat.S_ISGID
        else real_chmod(p, m, *a, **k)))
    with pytest.raises(shutil.Error):
        shutil.copytree(src, tmp_path / "bad")
    out = fs_copy.copy_tree(src, tmp_path / "good")
    assert (out / "sub" / "f.txt").read_text() == "x"
    assert stat.S_IMODE(os.stat(out / "run.sh").st_mode) & 0o777 == 0o755


def test_a_file_never_gets_setuid_or_setgid(tmp_path, sandbox):
    f = tmp_path / "a"
    f.write_text("x")
    os.chmod(f, 0o644)
    fs_copy.copy_file(f, tmp_path / "b")
    assert not os.stat(tmp_path / "b").st_mode & (stat.S_ISUID | stat.S_ISGID)


def test_ignore_symlinks_and_dirs_exist_ok_match_shutil(tmp_path):
    src = tmp_path / "s"
    (src / "keep").mkdir(parents=True)
    (src / "keep" / "a").write_text("1")
    (src / "__pycache__").mkdir()
    os.symlink("keep/a", src / "link")
    dst = tmp_path / "d"
    dst.mkdir()
    fs_copy.copy_tree(src, dst, ignore=shutil.ignore_patterns("__pycache__"),
                      dirs_exist_ok=True, symlinks=True)
    assert not (dst / "__pycache__").exists()
    assert os.readlink(dst / "link") == "keep/a"
    with pytest.raises(FileExistsError):
        fs_copy.copy_tree(src, dst)


@pytest.mark.parametrize("kind", ["dir", "file"])
def test_move_across_mounts_copies_then_deletes(tmp_path, monkeypatch, sandbox, kind):
    src = tmp_path / "src"
    if kind == "dir":
        (src / "sub").mkdir(parents=True)
        (src / "sub" / "f").write_text("x")
    else:
        src.write_text("x")

    def exdev(a, b):
        raise OSError(errno.EXDEV, "Invalid cross-device link")
    monkeypatch.setattr(os, "rename", exdev)
    dst = fs_copy.move(src, tmp_path / "dst")
    assert not src.exists()
    assert (dst / "sub" / "f" if kind == "dir" else dst).read_text() == "x"


def test_move_reraises_anything_but_exdev(tmp_path, monkeypatch):
    def eacces(a, b):
        raise PermissionError(errno.EACCES, "denied")
    monkeypatch.setattr(os, "rename", eacces)
    (tmp_path / "a").write_text("x")
    with pytest.raises(PermissionError):
        fs_copy.move(tmp_path / "a", tmp_path / "b")
    assert (tmp_path / "a").exists()
