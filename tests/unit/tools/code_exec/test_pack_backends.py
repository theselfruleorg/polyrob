"""073 W6/W7: the pack seam for execution backends, the remote-shell sandbox rule,
the Podman binary and re-attach across restart (all without a daemon)."""
from types import SimpleNamespace

import pytest

import core.config_policy as cp
import core.security.host_execution as he
from tools.code_exec import default_registry, pack_backends, resolve_backend
from tools.code_exec.backend import ExecutionBackendError
from tools.code_exec.backends import docker as dk
from tools.code_exec.sandbox_guard import (docker_socket_unreachable_reason,
                                          remote_shell_refusal)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("CODE_EXEC_DOCKER_BINARY", "CODE_EXEC_DOCKER_REUSE_ACROSS_RESTART",
              "CODE_EXEC_BACKEND", "DOCKER_HOST", "CODE_EXEC_TOOL_CALLS"):
        monkeypatch.delenv(k, raising=False)
    yield
    for src in ("test", "pack:fakepack"):
        pack_backends.unregister_source(src)
    pack_backends._DISCOVERED.discard("fakepack")


class Box:
    name = "box"

    def __init__(self, *, session_id=None, dev_mode=False):
        self.session_id, self.dev_mode = session_id, dev_mode

    @property
    def capabilities(self):
        return {"sandbox": True}


C = pack_backends.ExecBackendContribution


# --- the registry ------------------------------------------------------------------

def test_register_creates_through_the_default_registry():
    pack_backends.register_exec_backend(C(name="box", backend=Box), source="test")
    assert isinstance(default_registry.create("box"), Box)
    assert "box" not in pack_backends.shell_backend_names()   # no shell executor
    pack_backends.unregister_source("test")
    with pytest.raises(ExecutionBackendError):
        default_registry.create("box")


def test_reserved_and_duplicate_names_are_refused():
    for name in ("docker", "ssh", "host", "local_subprocess", "auto", "Bad Name"):
        with pytest.raises(pack_backends.PackBackendError):
            pack_backends.register_exec_backend(C(name=name, backend=Box), source="test")
    pack_backends.register_exec_backend(C(name="box", backend=Box), source="test")
    pack_backends.register_exec_backend(C(name="box", backend=Box), source="test")  # idempotent
    with pytest.raises(pack_backends.PackBackendError, match="already registered"):
        pack_backends.register_exec_backend(C(name="box", backend=Box), source="pack:other")


def test_discovery_pulls_only_loaded_packs(monkeypatch):
    import sys
    import types
    from core.packs import state
    mod = types.ModuleType("fake_exec_pack")
    mod.exec_backends = lambda: (C(name="fakebox", backend=Box, shell_executor=lambda **k: None),)
    monkeypatch.setitem(sys.modules, "fake_exec_pack", mod)
    rec = SimpleNamespace(id="fakepack", entry_point="fake_exec_pack:pack")
    monkeypatch.setattr(state, "loaded", lambda: [])
    assert not pack_backends.is_pack_backend("fakebox")
    monkeypatch.setattr(state, "loaded", lambda: [rec])
    # a registry miss pulls the loaded packs (run_code's CODE_EXEC_BACKEND=<name>)
    assert isinstance(default_registry.create("fakebox"), Box)
    assert pack_backends.source_of("fakebox") == "pack:fakepack"
    assert "fakebox" in pack_backends.shell_backend_names()


def test_resolve_backend_gives_a_pack_backend_its_session(monkeypatch):
    pack_backends.register_exec_backend(C(name="box", backend=Box), source="test")
    monkeypatch.setenv("CODE_EXEC_BACKEND", "box")
    monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "1")
    b = resolve_backend(session_id="s9", dev_mode=True)
    assert b.session_id == "s9" and b.dev_mode is True
    assert resolve_backend().session_id is None


# --- the remote-shell sandbox rule ---------------------------------------------------

def test_remote_shell_refusal_mirrors_the_code_exec_guard(monkeypatch):
    monkeypatch.setattr(cp, "local_mode_enabled", lambda: True)
    monkeypatch.setattr(he, "wallet_custody_enabled", lambda: False)
    assert remote_shell_refusal("ssh", {"sandbox": False}) is None
    monkeypatch.setattr(cp, "local_mode_enabled", lambda: False)
    assert "CODE_EXEC_SSH_SANDBOXED" in remote_shell_refusal("ssh", {"sandbox": False})
    assert "modal" in remote_shell_refusal("modal", {})
    assert remote_shell_refusal("modal", {"sandbox": True}) is None
    assert remote_shell_refusal("ssh", {"sandbox": "yes"})   # only the literal True


# --- podman ------------------------------------------------------------------------------

def test_docker_binary_default_and_override(monkeypatch):
    assert dk.docker_binary() == "docker"
    monkeypatch.setenv("CODE_EXEC_DOCKER_BINARY", "podman")
    assert dk.docker_binary() == "podman" and dk.docker_binary_is_daemonless()
    from tools.code_exec.result import ExecutionRequest
    argv = dk.DockerBackend()._build_run_argv(ExecutionRequest(language="bash", code="true"),
                                              "/tmp/ws")
    assert argv[:3] == ["podman", "run", "--rm"]
    # no docker socket to probe for a daemonless engine
    assert docker_socket_unreachable_reason("docker") is None
    monkeypatch.setenv("CODE_EXEC_DOCKER_BINARY", "-oevil")
    with pytest.raises(ExecutionBackendError):
        dk.docker_binary()


@pytest.mark.asyncio
async def test_default_runner_uses_the_binary(monkeypatch):
    seen = {}

    async def fake_run_group(argv, **kw):
        seen["argv"] = argv
        return 0, "", "", False

    monkeypatch.setattr(dk, "run_group", fake_run_group)
    monkeypatch.setenv("CODE_EXEC_DOCKER_BINARY", "/usr/bin/podman")
    await dk._default_docker_runner(["ps"])
    assert seen["argv"] == ["/usr/bin/podman", "ps"]


# --- re-attach across restart ----------------------------------------------------------

class FakeDocker:
    def __init__(self, ps_out=""):
        self.calls = []
        self.ps_out = ps_out

    async def __call__(self, args, *, input=None, timeout=None):
        self.calls.append(list(args))
        if args[0] == "ps":
            return 0, self.ps_out, ""
        return 0, "cid\n", ""


def _backend(monkeypatch, tmp_path, runner):
    b = dk.DockerBackend(docker_runner=runner, session_id="sess1")
    monkeypatch.setattr(b, "_resolve_persistent_workdir", lambda: str(tmp_path))
    return b


@pytest.mark.asyncio
async def test_reuse_off_is_byte_identical(monkeypatch, tmp_path):
    runner = FakeDocker()
    await _backend(monkeypatch, tmp_path, runner).setup()
    assert [c[0] for c in runner.calls] == ["run"]
    assert "polyrob.reuse=1" not in runner.calls[0]


@pytest.mark.asyncio
async def test_reuse_reattaches_the_same_sessions_container(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_EXEC_DOCKER_REUSE_ACROSS_RESTART", "1")
    runner = FakeDocker(ps_out="abc123\n")
    b = _backend(monkeypatch, tmp_path, runner)
    await b.setup()
    assert [c[0] for c in runner.calls] == ["ps"] and b._container == "abc123"
    ps = runner.calls[0]
    assert "label=polyrob.session=sess1" in ps and "status=running" in ps
    assert any(a.startswith("label=polyrob.config=") for a in ps)


@pytest.mark.asyncio
async def test_reuse_creates_a_labelled_container_when_none_matches(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_EXEC_DOCKER_REUSE_ACROSS_RESTART", "1")
    runner = FakeDocker(ps_out="")
    b = _backend(monkeypatch, tmp_path, runner)
    await b.setup()
    run = runner.calls[-1]
    assert run[0] == "run" and "polyrob.reuse=1" in run
    probe_cfg = [a for a in runner.calls[0] if a.startswith("label=polyrob.config=")][0]
    assert probe_cfg.split("label=", 1)[1] in run       # the same config hash
    assert b._container.startswith("polyrob-sbx-")


@pytest.mark.asyncio
async def test_cold_start_sweep_spares_reattachable_containers(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_DOCKER_REUSE_ACROSS_RESTART", "1")
    calls = []

    async def runner(args, *, input=None, timeout=None):
        calls.append(list(args))
        if args[0] == "ps":
            return 0, "keep\ngone\n", ""
        if args[0] == "inspect":
            return 0, "2020-01-01T00:00:00Z\t1\n2020-01-01T00:00:00Z\t<no value>\n", ""
        return 0, "", ""

    assert await dk.DockerBackend.reap_orphans(runner) == 1
    assert ["rm", "-f", "gone"] in calls and ["rm", "-f", "keep"] not in calls
