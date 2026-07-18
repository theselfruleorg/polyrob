"""WS-1 (computer-use parity): posture-gated dev mode for the docker sandbox.

At AGENT_COMPUTE_POSTURE >= 1 an entitled session gets an INSTALLABLE sandbox:
a writable `/install` dir (session-bound `<workspace>/.pylibs` bind), `python -s`
with `PYTHONPATH=/install` instead of the env-ignoring `python -I`, and
`HOME=/workspace` + `PIP_TARGET=/install` so a plain `pip install X` lands
somewhere importable. The posture-0 path must stay BYTE-IDENTICAL (python -I,
no extra mounts, no extra env) — `-I` is load-bearing for untrusted exec.

Dev entitlement rides on ``ExecutionRequest.dev_mode`` (per-call) and
``DockerBackend(dev_mode=...)`` (persistent-container mounts, fixed at setup).
"""
from __future__ import annotations

import os
import shutil
import stat
import uuid

import pytest

from tools.code_exec.backends.docker import DockerBackend
from tools.code_exec.result import ExecutionRequest

_needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")


def _argv(request, workdir="/tmp/ws"):
    return DockerBackend()._build_run_argv(request, workdir)


class _RecordingDocker:
    """Fake docker CLI runner — records argv lists, succeeds everything."""

    def __init__(self):
        self.log = []

    async def __call__(self, args, *, input=None, timeout=None):
        self.log.append(list(args))
        if args[:2] == ["run", "-d"]:
            return (0, "cid123\n", "")
        return (0, "ok\n", "")


def _pin_workdir(monkeypatch, path: str) -> None:
    monkeypatch.setattr(
        "tools.code_exec.backends.docker.DockerBackend._resolve_persistent_workdir",
        lambda self: path,
    )


# --- posture 0 / dev off: byte-identical hardened path ---------------------------

def test_default_request_has_dev_mode_off():
    assert ExecutionRequest(language="python", code="x").dev_mode is False


def test_non_dev_argv_keeps_isolated_python_and_no_install_mount():
    argv = _argv(ExecutionRequest(language="python", code="print(1)"))
    assert argv[-4:] == ["python", "-I", "-c", "print(1)"]
    assert not any("/install" in a for a in argv)
    assert not any(a.startswith("HOME=") for a in argv)
    assert not any(a.startswith("PYTHONPATH=") for a in argv)


# --- dev mode: importable installs ------------------------------------------------

def test_dev_argv_uses_python_s_with_install_mount_and_env():
    argv = _argv(ExecutionRequest(language="python", code="print(1)", dev_mode=True))
    assert argv[-4:] == ["python", "-s", "-c", "print(1)"]
    assert "-v" in argv and "/tmp/ws/.pylibs:/install" in argv
    for expected in ("HOME=/workspace", "PYTHONPATH=/install", "PIP_TARGET=/install"):
        assert expected in argv, f"missing -e {expected}"


def test_dev_argv_bash_gets_env_but_plain_bash_command():
    argv = _argv(ExecutionRequest(language="bash", code="pip install x", dev_mode=True))
    assert argv[-3:] == ["bash", "-c", "pip install x"]
    assert "PIP_TARGET=/install" in argv and "HOME=/workspace" in argv


def test_dev_env_caller_override_wins_over_defaults():
    req = ExecutionRequest(
        language="python", code="x", dev_mode=True, env={"PYTHONPATH": "/custom"}
    )
    argv = _argv(req)
    assert "PYTHONPATH=/custom" in argv
    assert "PYTHONPATH=/install" not in argv


def test_dev_env_secret_scrub_still_applies():
    req = ExecutionRequest(
        language="python", code="x", dev_mode=True,
        env={"FOO": "bar", "MY_API_KEY": "sk-123"},
    )
    argv = _argv(req)
    assert "FOO=bar" in argv
    assert not any("MY_API_KEY" in a for a in argv)


def test_dev_mode_does_not_relax_the_hardening_flags():
    argv = _argv(ExecutionRequest(language="python", code="x", dev_mode=True))
    assert "--read-only" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in argv
    assert "--pids-limit" in argv and "--memory" in argv


def test_ensure_install_dir_creates_worldwritable_pylibs(tmp_path):
    host = DockerBackend._ensure_install_dir(str(tmp_path))
    assert host == os.path.join(str(tmp_path), ".pylibs")
    assert os.path.isdir(host)
    mode = stat.S_IMODE(os.stat(host).st_mode)
    assert mode & 0o777 == 0o777  # container uid (e.g. 65534) must be able to write


# --- persistent mode ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_persistent_dev_setup_mounts_install_dir(monkeypatch, tmp_path):
    fake = _RecordingDocker()
    _pin_workdir(monkeypatch, str(tmp_path))
    b = DockerBackend(session_id=f"t-{uuid.uuid4().hex}", docker_runner=fake, dev_mode=True)
    await b.setup()
    run_d = next(a for a in fake.log if a[:2] == ["run", "-d"])
    assert f"{tmp_path}/.pylibs:/install" in run_d
    assert os.path.isdir(tmp_path / ".pylibs")


@pytest.mark.asyncio
async def test_persistent_non_dev_setup_has_no_install_mount(monkeypatch, tmp_path):
    fake = _RecordingDocker()
    _pin_workdir(monkeypatch, str(tmp_path))
    b = DockerBackend(session_id=f"t-{uuid.uuid4().hex}", docker_runner=fake)
    await b.setup()
    run_d = next(a for a in fake.log if a[:2] == ["run", "-d"])
    assert not any("/install" in a for a in run_d)


@pytest.mark.asyncio
async def test_persistent_dev_exec_uses_python_s_and_env(monkeypatch, tmp_path):
    fake = _RecordingDocker()
    _pin_workdir(monkeypatch, str(tmp_path))
    b = DockerBackend(session_id=f"t-{uuid.uuid4().hex}", docker_runner=fake, dev_mode=True)
    await b.setup()
    await b.run(ExecutionRequest(language="python", code="print(1)", dev_mode=True))
    ex = next(a for a in fake.log if a and a[0] == "exec")
    joined = ex
    assert "python" in joined and "-s" in joined and "-I" not in joined
    for expected in ("HOME=/workspace", "PYTHONPATH=/install", "PIP_TARGET=/install"):
        assert expected in joined


@pytest.mark.asyncio
async def test_persistent_non_dev_exec_stays_isolated(monkeypatch, tmp_path):
    fake = _RecordingDocker()
    _pin_workdir(monkeypatch, str(tmp_path))
    b = DockerBackend(session_id=f"t-{uuid.uuid4().hex}", docker_runner=fake, dev_mode=True)
    await b.setup()
    await b.run(ExecutionRequest(language="python", code="print(1)"))  # dev_mode off
    ex = next(a for a in fake.log if a and a[0] == "exec")
    assert "-I" in ex and "-s" not in ex
    assert "PYTHONPATH=/install" not in ex


# --- persistent-on default at posture >= 1 ------------------------------------------

def test_persistent_flag_defaults_on_at_posture_1(monkeypatch):
    import agents.task.constants as c
    from tools.code_exec import code_exec_docker_persistent_enabled

    monkeypatch.delenv("CODE_EXEC_DOCKER_PERSISTENT", raising=False)
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    c._refreeze_compute_posture_for_tests()
    try:
        assert code_exec_docker_persistent_enabled() is True
        # explicit env override still wins
        monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "false")
        assert code_exec_docker_persistent_enabled() is False
    finally:
        monkeypatch.delenv("AGENT_COMPUTE_POSTURE", raising=False)
        c._refreeze_compute_posture_for_tests()


def test_persistent_flag_defaults_off_at_posture_0(monkeypatch):
    import agents.task.constants as c
    from tools.code_exec import code_exec_docker_persistent_enabled

    monkeypatch.delenv("CODE_EXEC_DOCKER_PERSISTENT", raising=False)
    monkeypatch.delenv("AGENT_COMPUTE_POSTURE", raising=False)
    c._refreeze_compute_posture_for_tests()
    assert code_exec_docker_persistent_enabled() is False


def test_resolve_backend_passes_dev_mode(monkeypatch):
    import agents.task.constants as c
    from tools.code_exec import resolve_backend

    monkeypatch.setenv("CODE_EXEC_BACKEND", "docker")
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    c._refreeze_compute_posture_for_tests()
    try:
        b = resolve_backend(session_id="s1", dev_mode=True)
        assert isinstance(b, DockerBackend)
        assert b._session_id == "s1"
        assert b._dev_mode is True
    finally:
        monkeypatch.delenv("AGENT_COMPUTE_POSTURE", raising=False)
        c._refreeze_compute_posture_for_tests()


# --- real-docker integration (the acceptance): install AND import -------------------

@_needs_docker
@pytest.mark.asyncio
async def test_docker_dev_install_then_import(tmp_path, monkeypatch):
    """pip install --target=/install into the session bind, then IMPORT it from a
    separate exec — the exact capability whose absence blocked goal 8632a4571b36."""
    monkeypatch.setenv("CODE_EXEC_NETWORK", "egress")
    monkeypatch.setenv("CODE_EXEC_MAX_TIMEOUT_SEC", "120")
    sid = f"devtest-{uuid.uuid4().hex}"
    _pin_workdir(monkeypatch, str(tmp_path))
    b = DockerBackend(session_id=sid, dev_mode=True)
    await b.setup()
    try:
        inst = await b.run(ExecutionRequest(
            language="bash",
            code="python -m pip install --no-input --target=/install cowsay",
            dev_mode=True, timeout=120,
        ))
        assert inst.exit_code == 0, f"pip install failed: {inst.stderr[-2000:]}"
        imp = await b.run(ExecutionRequest(
            language="python", code="import cowsay; print('ok-imported')",
            dev_mode=True,
        ))
        assert "ok-imported" in imp.stdout, f"import failed: {imp.stderr[-2000:]}"
    finally:
        await b.teardown()


# --- 014 B1: dev container defaults to a node-capable image -----------------------

def _setup_argv(fake: "_RecordingDocker"):
    runs = [a for a in fake.log if a[:2] == ["run", "-d"]]
    assert runs, f"no persistent 'docker run -d' recorded; log={fake.log}"
    return runs[0]


@pytest.mark.asyncio
async def test_dev_mode_defaults_to_node_capable_image(monkeypatch, tmp_path):
    monkeypatch.delenv("CODE_EXEC_DOCKER_IMAGE", raising=False)
    monkeypatch.delenv("CODE_EXEC_DEV_IMAGE", raising=False)
    fake = _RecordingDocker()
    _pin_workdir(monkeypatch, str(tmp_path))
    b = DockerBackend(session_id="s-img", docker_runner=fake, dev_mode=True)
    await b.setup()
    assert "nikolaik/python-nodejs:python3.11-nodejs20" in _setup_argv(fake)


@pytest.mark.asyncio
async def test_explicit_image_env_wins_in_dev_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_EXEC_DOCKER_IMAGE", "myimg:pinned")
    fake = _RecordingDocker()
    _pin_workdir(monkeypatch, str(tmp_path))
    b = DockerBackend(session_id="s-img2", docker_runner=fake, dev_mode=True)
    await b.setup()
    assert "myimg:pinned" in _setup_argv(fake)


def test_non_dev_image_default_unchanged(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_DOCKER_IMAGE", raising=False)
    monkeypatch.delenv("CODE_EXEC_DEV_IMAGE", raising=False)
    assert DockerBackend(docker_runner=_RecordingDocker()).image == "python:3.12-slim"


def test_dev_image_env_overrides_dev_default(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_DOCKER_IMAGE", raising=False)
    monkeypatch.setenv("CODE_EXEC_DEV_IMAGE", "mydev:img")
    b = DockerBackend(docker_runner=_RecordingDocker(), dev_mode=True)
    assert b.image == "mydev:img"


# --- 014 B2: effective_setup_network — what a run/exec ACTUALLY experiences -------

def test_effective_network_persistent_dev_auto_bridge(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    b = DockerBackend(session_id="s-net", docker_runner=_RecordingDocker(), dev_mode=True)
    assert b.effective_setup_network() == "bridge"


def test_effective_network_explicit_none_still_wins(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_NETWORK", "none")
    b = DockerBackend(session_id="s-net2", docker_runner=_RecordingDocker(), dev_mode=True)
    assert b.effective_setup_network() == "none"


def test_effective_network_ephemeral_dev_is_env_driven(monkeypatch):
    # ephemeral (no session): per-request policy, NO auto-bridge — honest "none"
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    b = DockerBackend(docker_runner=_RecordingDocker(), dev_mode=True)
    assert b.effective_setup_network() == "none"


# --- 014 B3: dev-mode timeout ceiling matches the shell foreground contract -------
# (pre-014 the backend re-clamp silently cut shell_run foreground commands to 30s:
#  shell/tool.py clamps to 120, executor passes through, backend re-clamped to 30).

def test_dev_mode_timeout_ceiling_120_when_unset(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_MAX_TIMEOUT_SEC", raising=False)
    b = DockerBackend(docker_runner=_RecordingDocker(), dev_mode=True)
    assert b.max_timeout == 120.0
    assert b._clamp_timeout(999) == 120.0


def test_explicit_max_timeout_env_wins_in_dev_mode(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_MAX_TIMEOUT_SEC", "45")
    b = DockerBackend(docker_runner=_RecordingDocker(), dev_mode=True)
    assert b.max_timeout == 45.0


def test_non_dev_timeout_default_unchanged(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_MAX_TIMEOUT_SEC", raising=False)
    b = DockerBackend(docker_runner=_RecordingDocker())
    assert b.max_timeout == 30.0
