"""H02 + H10 (harness security analysis 2026-09-23): the docker backend must never
bind-mount, or chmod through, a path the sandbox can swap for a symlink.

H02: dev mode bind-mounted ``<workspace>/.pylibs`` — sandbox code replaced it with a
symlink and the daemon (root) mounted the HOST target read-write. The install dir now
lives outside the workspace and every bind source is ``lstat``-checked right before
the argv. H10: the root-run chmod walk followed directory symlinks.
No Docker daemon needed.
"""
import os
import stat
import uuid

import pytest

from tools.code_exec.backend import ExecutionBackendError
from tools.code_exec.backends import docker as docker_mod
from tools.code_exec.backends.docker import DockerBackend
from tools.code_exec.result import ExecutionRequest


class _RecordingDocker:
    def __init__(self):
        self.log = []

    async def __call__(self, args, *, input=None, timeout=None):
        self.log.append(list(args))
        return (0, "cid\n", "")


def _pin_workdir(monkeypatch, path):
    monkeypatch.setattr(DockerBackend, "_resolve_persistent_workdir", lambda self: path)


def _as_root_owned(monkeypatch, root):
    real = os.lstat

    def fake(path, *a, **kw):
        st = real(path, *a, **kw)
        if str(path).startswith(str(root)):
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 0, 0,
                                   st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
        return st
    monkeypatch.setattr(os, "lstat", fake)
    monkeypatch.setattr(os, "stat", fake)


# --- H02: the install dir is outside the workspace ---------------------------------

def test_install_dir_is_outside_the_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    path = DockerBackend._ensure_install_dir(str(ws))
    assert not os.path.realpath(path).startswith(os.path.realpath(ws) + os.sep)
    assert os.path.isdir(path) and not os.path.islink(path)


def test_install_dir_is_stable_per_workspace(tmp_path):
    a = DockerBackend._install_dir_path(str(tmp_path / "a"))
    assert a == DockerBackend._install_dir_path(str(tmp_path / "a"))
    assert a != DockerBackend._install_dir_path(str(tmp_path / "b"))


# --- H02: the bind-source check -----------------------------------------------------

def test_check_bind_source_accepts_a_real_owned_dir(tmp_path):
    DockerBackend._check_bind_source(str(tmp_path), "workspace")


def test_check_bind_source_refuses_a_symlinked_dir(tmp_path):
    target = tmp_path / "host_secret_dir"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ExecutionBackendError, match="symlink"):
        DockerBackend._check_bind_source(str(link), "install dir")


def test_check_bind_source_refuses_a_file_and_a_missing_path(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    with pytest.raises(ExecutionBackendError, match="not a directory"):
        DockerBackend._check_bind_source(str(f), "workspace")
    with pytest.raises(ExecutionBackendError, match="cannot lstat"):
        DockerBackend._check_bind_source(str(tmp_path / "nope"), "workspace")


def test_check_bind_source_refuses_a_foreign_owner(tmp_path, monkeypatch):
    real = os.lstat
    monkeypatch.setattr(os, "lstat", lambda p, *a, **kw: os.stat_result(
        (lambda st: (st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 4242, 4242,
                     st.st_size, st.st_atime, st.st_mtime, st.st_ctime))(real(p))))
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(ExecutionBackendError, match="owned by uid 4242"):
        DockerBackend._check_bind_source(str(tmp_path), "workspace")


@pytest.mark.asyncio
async def test_persistent_setup_refuses_a_symlinked_install_dir(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    host_target = tmp_path / "host_target"
    host_target.mkdir()
    os.chmod(host_target, 0o700)
    install = DockerBackend._install_dir_path(str(ws))
    os.makedirs(os.path.dirname(install), exist_ok=True)
    os.symlink(host_target, install)  # the swap the sandbox used to be able to do
    _pin_workdir(monkeypatch, str(ws))
    fake = _RecordingDocker()
    b = DockerBackend(session_id=f"t-{uuid.uuid4().hex}", docker_runner=fake, dev_mode=True)
    with pytest.raises(ExecutionBackendError, match="symlink"):
        await b.setup()
    assert not any(a[:2] == ["run", "-d"] for a in fake.log)  # nothing was mounted
    assert stat.S_IMODE(os.stat(host_target).st_mode) == 0o700  # never chmodded through


@pytest.mark.asyncio
async def test_persistent_setup_refuses_a_symlinked_workspace(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "ws_link"
    link.symlink_to(real)
    _pin_workdir(monkeypatch, str(link))
    fake = _RecordingDocker()
    b = DockerBackend(session_id=f"t-{uuid.uuid4().hex}", docker_runner=fake)
    with pytest.raises(ExecutionBackendError, match="symlink"):
        await b.setup()
    assert not any(a[:2] == ["run", "-d"] for a in fake.log)


@pytest.mark.asyncio
async def test_ephemeral_run_refuses_a_symlinked_workspace(monkeypatch, tmp_path):
    import subprocess
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "ws_link"
    link.symlink_to(real)

    def _boom(*a, **kw):
        raise AssertionError("docker must not be launched")
    monkeypatch.setattr(subprocess, "Popen", _boom)
    res = await DockerBackend().run(
        ExecutionRequest(language="python", code="print(1)", workdir=str(link), dev_mode=True))
    assert res.exit_code == 2 and "symlink" in res.stderr


# --- H10: the chmod walk never follows a link --------------------------------------

def test_chmod_walk_leaves_a_symlinked_dir_target_unchanged(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    inner = outside / "secret.txt"
    inner.write_text("s")
    os.chmod(inner, 0o600)
    os.chmod(outside, 0o700)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "dirlink").symlink_to(outside, target_is_directory=True)
    _as_root_owned(monkeypatch, tmp_path)

    DockerBackend._ensure_workspace_writable(str(ws))

    assert stat.S_IMODE(os.lstat(outside).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(inner).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(ws).st_mode) == 0o777  # the real root still widened


def test_chmod_walk_refuses_a_symlinked_workspace_root(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.chmod(outside, 0o700)
    link = tmp_path / "ws"
    link.symlink_to(outside, target_is_directory=True)
    _as_root_owned(monkeypatch, tmp_path)

    DockerBackend._ensure_workspace_writable(str(link))

    assert stat.S_IMODE(os.lstat(outside).st_mode) == 0o700


def test_chmod_nofollow_refuses_a_symlink(tmp_path):
    target = tmp_path / "t.txt"
    target.write_text("x")
    os.chmod(target, 0o600)
    link = tmp_path / "l"
    link.symlink_to(target)
    with pytest.raises(OSError):
        docker_mod._chmod_nofollow(str(link), 0o666, want_dir=False)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
