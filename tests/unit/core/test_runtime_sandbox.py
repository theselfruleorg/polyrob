"""The runtime-sandbox contract (the runtime-sandbox contract).

The sandbox is simulated: a chmod/fchmod carrying setuid/setgid raises EPERM (what
``RestrictSUIDSGID=true`` does). Each case was first reproduced under the real unit
with ``ops/sandbox/harness.py`` (2026-11-20).
"""
import errno
import os
import stat
import sys

import pytest

import core.runtime_paths as scratch
from core import fs_copy


@pytest.fixture
def restrict_suid_sgid(monkeypatch):
    """Call it to switch the simulated sandbox on (after the test's own setup)."""
    real_chmod, real_fchmod = os.chmod, os.fchmod

    def refuse(mode):
        if mode & (stat.S_ISUID | stat.S_ISGID):
            raise PermissionError(errno.EPERM, "Operation not permitted")

    def chmod(path, mode, *a, **kw):
        refuse(mode)
        return real_chmod(path, mode, *a, **kw)

    def fchmod(fd, mode):
        refuse(mode)
        return real_fchmod(fd, mode)

    def on():
        monkeypatch.setattr(os, "chmod", chmod)
        monkeypatch.setattr(os, "fchmod", fchmod)
    return on


def _setgid_dir(path, mode=0o2750):
    path.mkdir()
    os.chmod(path, mode)
    if not os.stat(path).st_mode & stat.S_ISGID:
        pytest.skip("this filesystem does not keep a setgid bit on a directory")
    return path


# --- widen_dir_mode -----------------------------------------------------------

def test_widen_dir_mode_is_a_no_op_when_the_bits_are_there(tmp_path, restrict_suid_sgid):
    d = _setgid_dir(tmp_path / "d", 0o2770)
    restrict_suid_sgid()
    assert fs_copy.widen_dir_mode(d, 0o770) == 0o2770


def test_widen_dir_mode_keeps_setgid_in_the_unit(tmp_path, restrict_suid_sgid):
    """A plain chmod(0o770) would clear setgid; the seam keeps it and skips."""
    d = _setgid_dir(tmp_path / "d")
    restrict_suid_sgid()
    mode = fs_copy.widen_dir_mode(d, 0o770)
    assert mode & stat.S_ISGID
    assert os.stat(d).st_mode & stat.S_ISGID


def test_widen_dir_mode_widens_and_keeps_setgid_outside_a_unit(tmp_path):
    d = _setgid_dir(tmp_path / "d")
    assert fs_copy.widen_dir_mode(d, 0o770) == 0o2770


def test_widen_dir_mode_refuses_a_symlink(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(NotADirectoryError):
        fs_copy.widen_dir_mode(tmp_path / "link", 0o777)


def test_perm_bits_drops_special_bits():
    assert fs_copy.perm_bits(0o104755) == 0o755
    assert fs_copy.perm_bits(0o2770) == 0o770


# --- confined_write -------------------------------------------------------------

def test_confined_write_dirs_in_a_shared_store_keep_setgid(tmp_path, restrict_suid_sgid):
    from core.security.confined_write import write_confined_text
    root = _setgid_dir(tmp_path / "store", 0o2770)
    restrict_suid_sgid()
    write_confined_text(root / "a" / "b" / "f.txt", root, "x")
    # BSD/macOS dirs never inherit the bit, so there only the first level is shared.
    levels = (root / "a", root / "a" / "b") if sys.platform.startswith("linux") else (root / "a",)
    for d in levels:
        m = os.stat(d).st_mode
        if sys.platform.startswith("linux"):
            assert m & stat.S_ISGID, oct(m)
        assert m & 0o070 == 0o070, oct(m)


def test_confined_write_private_store_stays_private(tmp_path):
    from core.security.confined_write import write_confined_text
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    write_confined_text(root / "a" / "f.txt", root, "x")
    assert stat.S_IMODE(os.stat(root / "a").st_mode) & 0o077 == 0


# --- files a sandbox container marked setuid/setgid ---------------------------

def test_workspace_rewrite_of_a_setgid_file(tmp_path, monkeypatch):
    from core.security import workspace_io
    f = tmp_path / "g.sh"
    f.write_text("x")
    os.chmod(f, 0o2775)
    real_open = os.open

    def sandboxed_open(path, flags, mode=0o777, *a, **kw):
        if flags & os.O_CREAT and mode & (stat.S_ISUID | stat.S_ISGID):
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real_open(path, flags, mode, *a, **kw)
    monkeypatch.setattr(os, "open", sandboxed_open)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {sandboxed_open})
    workspace_io.write_bytes(f, tmp_path, b"y")
    assert f.read_bytes() == b"y"
    assert not os.stat(f).st_mode & 0o6000


def test_snapshot_ships_readable_files_without_special_bits(tmp_path, restrict_suid_sgid):
    """The app container runs as 65534: a group-readable 0640 file must ship
    readable, an owner-only 0600 file never ships (audit 2026-11-21 M-9); a setuid
    file must not stop the snapshot (EPERM under the supervisor's unit)."""
    from core.app_service.snapshot import snapshot_file_mode, snapshot_tree
    src = tmp_path / "ws"
    src.mkdir()
    (src / "app.py").write_text("print(1)")
    (src / "run.sh").write_text("x")
    (src / "private.txt").write_text("p")
    os.chmod(src / "app.py", 0o640)
    os.chmod(src / "private.txt", 0o600)
    (src / "run.sh").chmod(0o755)
    restrict_suid_sgid()
    total, files, skipped = snapshot_tree(str(src), str(tmp_path / "out" / "d"), max_mb=1)
    assert files == 2 and "private.txt (private)" in skipped
    assert stat.S_IMODE(os.stat(tmp_path / "out" / "d" / "app.py").st_mode) == 0o644
    assert stat.S_IMODE(os.stat(tmp_path / "out" / "d" / "run.sh").st_mode) == 0o755
    assert snapshot_file_mode(0o104755) == 0o755
    assert snapshot_file_mode(0o102600) == 0o644


def test_container_widening_never_carries_setgid():
    from tools.code_exec.backends.docker import widen_mode_for_container
    assert widen_mode_for_container(0o2755) == 0o777
    assert widen_mode_for_container(0o4644) == 0o666


# --- the scratch seam ---------------------------------------------------------

def test_scratch_root_is_in_the_data_home_and_shared(tmp_path, monkeypatch):
    import core.runtime_paths as rp
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr(rp, "effective_data_home", lambda: tmp_path / "home")
    root = scratch.scratch_root()
    assert root == tmp_path / "home" / "tmp"
    assert stat.S_IMODE(os.stat(root).st_mode) & 0o070 == 0o070 or os.umask(0o022) & 0o070
    d = scratch.scratch_mkdtemp("x-")
    assert d.startswith(str(root))


def test_an_operator_tmpdir_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path / "mine"))
    assert scratch.scratch_root() is None


def test_sandbox_scratch_dir_is_in_a_view_tree(tmp_path, monkeypatch):
    import core.runtime_paths as rp
    monkeypatch.setattr(rp, "effective_data_home", lambda: tmp_path / "home")
    d = scratch.sandbox_scratch_dir("prpc-")
    assert d.startswith(str(tmp_path / "home" / "sandbox_installs" / "_scratch" / "prpc-"))
    stable = scratch.sandbox_scratch_dir("persistent-", name="../s1")
    assert stable == scratch.sandbox_scratch_dir("persistent-", name="../s1")
    assert "/../" not in stable


def test_sandbox_scratch_dir_refuses_with_the_cause(monkeypatch):
    import core.runtime_paths as rp

    def boom():
        raise RuntimeError("no data home")
    monkeypatch.setattr(rp, "effective_data_home", boom)
    with pytest.raises(scratch.ScratchUnavailable, match="no data home"):
        scratch.sandbox_scratch_dir("run-")


def test_tool_rpc_socket_dir_is_not_in_the_private_tmp(monkeypatch):
    import shutil
    import tempfile
    import core.runtime_paths as rp
    from tools.code_exec.tool_rpc import make_socket_dir
    home = tempfile.mkdtemp(prefix="h", dir="/tmp")   # short, like /var/lib/polyrob
    try:
        monkeypatch.setattr(rp, "effective_data_home", lambda: __import__("pathlib").Path(home))
        d = make_socket_dir(owner="1000:1000")
        assert d.startswith(os.path.join(home, "sandbox_installs", "_scratch"))
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_tool_rpc_long_data_home_falls_back_to_a_short_path(tmp_path, monkeypatch):
    import core.runtime_paths as rp
    from tools.code_exec.tool_rpc import make_socket_dir
    deep = tmp_path / ("x" * 90)
    monkeypatch.setattr(rp, "effective_data_home", lambda: deep)
    assert len(make_socket_dir(owner="1000:1000")) < 80


def test_tool_rpc_without_a_data_home_refuses_for_a_container(monkeypatch):
    import core.runtime_paths as rp
    from tools.code_exec.tool_rpc import make_socket_dir

    def boom():
        raise RuntimeError("no data home")
    monkeypatch.setattr(rp, "effective_data_home", boom)
    with pytest.raises(scratch.ScratchUnavailable):
        make_socket_dir(owner="1000:1000")
    assert make_socket_dir()      # in-process: the /tmp fallback is fine


def test_docker_install_root_has_no_tmp_fallback(monkeypatch):
    import core.runtime_paths as rp
    from tools.code_exec.backend import ExecutionBackendError
    from tools.code_exec.backends import docker

    def boom():
        raise RuntimeError("no data home")
    monkeypatch.setattr(rp, "effective_data_home", boom)
    with pytest.raises(ExecutionBackendError, match="no data home"):
        docker._install_root()


# --- the apps supervisor --------------------------------------------------------

def test_supervisor_drops_the_sandbox_docker_host():
    from core.app_service.config import supervisor_docker_env
    env = {"DOCKER_HOST": "unix:///run/polyrob-sandbox/docker.sock"}
    assert supervisor_docker_env(env) == "unix:///run/polyrob-sandbox/docker.sock"
    assert "DOCKER_HOST" not in env
    other = {"DOCKER_HOST": "unix:///var/run/docker.sock"}
    assert supervisor_docker_env(other) is None
    assert other["DOCKER_HOST"] == "unix:///var/run/docker.sock"


# --- legacy lazy installs -------------------------------------------------------

def test_legacy_lazy_install_refuses_a_read_only_venv(monkeypatch):
    from core import lazy_deps
    real_access = os.access
    monkeypatch.setattr(lazy_deps.os, "access",
                        lambda p, m: False if m == os.W_OK else real_access(p, m))
    with pytest.raises(lazy_deps.FeatureUnavailable, match="read-only"):
        lazy_deps._ensure_legacy("anything", ("x==1",), prompt=False)


# --- restore keeps each tree's own mode -------------------------------------------

def test_restore_dir_mode_follows_the_live_dir(tmp_path):
    from core.update_snapshot.restore import _target_dir_mode
    (tmp_path / "skills").mkdir()
    os.chmod(tmp_path / "skills", 0o770)
    (tmp_path / "wallet").mkdir()
    os.chmod(tmp_path / "wallet", 0o700)
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        assert _target_dir_mode(fd, "skills") == 0o770
        assert _target_dir_mode(fd, "wallet") == 0o700
        assert _target_dir_mode(fd, "absent") == 0o700
    finally:
        os.close(fd)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores the dir mode")
def test_restore_refuses_a_read_only_target_before_any_write(tmp_path):
    """EROFS on /opt/polyrob/config/.env came AFTER the databases were replaced."""
    import sqlite3
    from pathlib import Path
    from core.update_snapshot.snapshot import create_snapshot, restore_snapshot
    home = tmp_path / "home"
    (home / "database").mkdir(parents=True)
    db = home / "database" / "bot.db"
    sqlite3.connect(db).execute("create table t(x)").connection.commit()
    code_cfg = tmp_path / "opt" / "config"
    code_cfg.mkdir(parents=True)
    env = code_cfg / ".env.production"
    env.write_text("A=1\n")
    info = create_snapshot(snapshots_root=tmp_path / "snaps", data_home=home, from_version="0",
                           db_paths=[db], config_paths=[env], dir_paths=[])
    # Same bytes in a read-only dir: nothing to write, the restore goes through.
    os.chmod(code_cfg, 0o500)
    try:
        restore_snapshot(Path(info.path), data_home=home, allowed_paths=[env])
        # Changed bytes in a read-only dir: refused, and the db is untouched.
        os.chmod(code_cfg, 0o700)
        env.write_text("A=2\n")
        os.chmod(code_cfg, 0o500)
        sqlite3.connect(db).execute("create table marker(x)").connection.commit()
        with pytest.raises(OSError, match="nothing was restored"):
            restore_snapshot(Path(info.path), data_home=home, allowed_paths=[env])
        tables = {r[0] for r in sqlite3.connect(db).execute("select name from sqlite_master")}
        assert "marker" in tables
    finally:
        os.chmod(code_cfg, 0o700)


# --- review fixes -----------------------------------------------------------------

def test_widen_dir_mode_required_drops_setgid_for_access(tmp_path, restrict_suid_sgid):
    d = _setgid_dir(tmp_path / "d", 0o2700)
    restrict_suid_sgid()
    assert fs_copy.widen_dir_mode(d, 0o711, required=True) == 0o711
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o711


def test_copy_tree_keeps_a_private_dir_private(tmp_path):
    src = tmp_path / "src"
    (src / "secret").mkdir(parents=True)
    os.chmod(src / "secret", 0o700)
    (src / "secret" / "k").write_text("x")
    fs_copy.copy_tree(src, tmp_path / "dst")
    assert stat.S_IMODE(os.stat(tmp_path / "dst" / "secret").st_mode) & 0o077 == 0


def test_sandbox_scratch_names_never_collide(tmp_path, monkeypatch):
    monkeypatch.setattr(scratch, "effective_data_home", lambda: tmp_path)
    assert scratch.sandbox_scratch_dir("p-", name="a.b") != scratch.sandbox_scratch_dir("p-", name="a_b")


def test_tool_rpc_long_path_under_a_unit_refuses_for_a_container(tmp_path, monkeypatch):
    from tools.code_exec.tool_rpc import make_socket_dir
    monkeypatch.setattr(scratch, "effective_data_home", lambda: tmp_path / ("x" * 90))
    monkeypatch.setenv("INVOCATION_ID", "unit")
    with pytest.raises(scratch.ScratchUnavailable, match="POLYROB_DATA_DIR"):
        make_socket_dir(owner="1000:1000")


def test_profile_handover_keeps_exec_bits_and_never_follows_a_link(tmp_path, monkeypatch):
    import types
    from cli.commands import profile as prof
    home = tmp_path / "p"
    (home / "hooks").mkdir(parents=True)
    (home / "hooks" / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(home / "hooks" / "run.sh", 0o755)
    (home / ".env").write_text("A=1\n")
    outside = tmp_path / "outside"
    outside.write_text("x")
    os.chmod(outside, 0o644)
    (home / "link").symlink_to(outside)
    owners = []
    monkeypatch.setattr(prof.os, "geteuid", lambda: 0)
    monkeypatch.setattr(prof.os, "fchown", lambda fd, u, g: owners.append((u, g)))
    import pwd, grp
    monkeypatch.setattr(pwd, "getpwnam", lambda n: types.SimpleNamespace(pw_uid=4242))
    monkeypatch.setattr(grp, "getgrnam", lambda n: types.SimpleNamespace(gr_gid=4343))
    real_fchmod = os.fchmod
    monkeypatch.setattr(prof.os, "fchmod", lambda fd, m: real_fchmod(fd, m & 0o777))
    prof._hand_to_service_user(home)
    assert stat.S_IMODE(os.stat(home / "hooks" / "run.sh").st_mode) == 0o770
    assert stat.S_IMODE(os.stat(home / ".env").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(outside).st_mode) == 0o644      # the link was not followed
    assert owners and set(owners) == {(4242, 4343)}
