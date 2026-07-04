"""P0 Task 3 — DockerBackend argv hardening (no Docker required) + guarded integration."""
import os
import shutil

import pytest

from tools.code_exec.backends.docker import DockerBackend
from tools.code_exec.result import ExecutionRequest


def _argv(monkeypatch, request, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return DockerBackend()._build_run_argv(request, "/tmp/ws")


def test_argv_has_hardening_flags(monkeypatch):
    argv = _argv(monkeypatch, ExecutionRequest(language="python", code="print(1)"))
    assert argv[:3] == ["docker", "run", "--rm"]
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in argv and "no-new-privileges" in argv
    assert "--read-only" in argv
    assert "--pids-limit" in argv
    assert "--memory" in argv
    assert "--cpus" in argv
    assert "--user" in argv and argv[argv.index("--user") + 1]  # non-empty
    assert "-v" in argv and "/tmp/ws:/workspace" in argv        # workspace-only mount
    assert argv[argv.index("-w") + 1] == "/workspace"


def test_network_none_by_default(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    argv = _argv(monkeypatch, ExecutionRequest(language="python", code="print(1)"))
    assert argv[argv.index("--network") + 1] == "none"


def test_request_network_host_overrides_default(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    argv = _argv(monkeypatch, ExecutionRequest(language="bash", code="true", network="host"))
    assert argv[argv.index("--network") + 1] == "host"


def test_env_default_network_egress_maps_to_bridge(monkeypatch):
    argv = _argv(monkeypatch, ExecutionRequest(language="bash", code="true"), CODE_EXEC_NETWORK="egress")
    assert argv[argv.index("--network") + 1] == "bridge"


def test_pids_image_and_user_from_env(monkeypatch):
    argv = _argv(
        monkeypatch,
        ExecutionRequest(language="python", code="print(1)"),
        CODE_EXEC_PIDS_LIMIT="99",
        CODE_EXEC_DOCKER_IMAGE="myimg:latest",
        CODE_EXEC_DOCKER_USER="1234:5678",
    )
    assert argv[argv.index("--pids-limit") + 1] == "99"
    assert "myimg:latest" in argv
    assert argv[argv.index("--user") + 1] == "1234:5678"


def test_python_command_uses_isolated_flag(monkeypatch):
    argv = _argv(monkeypatch, ExecutionRequest(language="python", code="print(1)"))
    assert argv[-4:] == ["python", "-I", "-c", "print(1)"]


def test_bash_command(monkeypatch):
    argv = _argv(monkeypatch, ExecutionRequest(language="bash", code="echo hi"))
    assert argv[-3:] == ["bash", "-c", "echo hi"]


def test_request_env_forwarded_and_scrubbed(monkeypatch):
    req = ExecutionRequest(language="python", code="print(1)", env={"FOO": "bar", "MY_TOKEN": "sk"})
    argv = _argv(monkeypatch, req)
    assert "-e" in argv and "FOO=bar" in argv
    assert "MY_TOKEN=sk" not in argv  # secret-named stripped from container env


def test_unsupported_language_raises():
    with pytest.raises(ValueError):
        DockerBackend()._build_run_argv(ExecutionRequest(language="ruby", code="x"), "/tmp/ws")


def test_capabilities_advertise_sandbox_true(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    caps = DockerBackend().capabilities
    assert caps["sandbox"] is True
    assert caps["isolation"] == "container"
    assert caps["network"] is False  # default none


def test_capabilities_network_true_when_egress(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_NETWORK", "egress")
    assert DockerBackend().capabilities["network"] is True


def test_registered_in_default_registry():
    from tools.code_exec import default_registry
    assert "docker" in default_registry.names


def test_docker_user_forced_unprivileged_when_host_is_root(monkeypatch):
    """Host process runs as root (e.g. prod systemd User=root) -> container must NOT be root."""
    monkeypatch.setattr(os, "getuid", lambda: 0)
    monkeypatch.setattr(os, "getgid", lambda: 0)
    monkeypatch.delenv("CODE_EXEC_DOCKER_USER", raising=False)
    assert DockerBackend().user == "65534:65534"


def test_docker_user_matches_host_when_non_root(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.setattr(os, "getgid", lambda: 1000)
    monkeypatch.delenv("CODE_EXEC_DOCKER_USER", raising=False)
    assert DockerBackend().user == "1000:1000"


def test_docker_user_env_override_wins_even_when_host_is_root(monkeypatch):
    """Explicit operator override is honored verbatim, even if that choice is root."""
    monkeypatch.setattr(os, "getuid", lambda: 0)
    monkeypatch.setattr(os, "getgid", lambda: 0)
    monkeypatch.setenv("CODE_EXEC_DOCKER_USER", "7:7")
    assert DockerBackend().user == "7:7"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
@pytest.mark.asyncio
async def test_docker_runs_and_blocks_network(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    b = DockerBackend()
    await b.setup()
    ok = await b.run(ExecutionRequest(language="python", code="print('hi')"))
    assert "hi" in ok.stdout and ok.exit_code == 0
    blocked = await b.run(ExecutionRequest(
        language="python",
        code="import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)",
    ))
    assert blocked.exit_code != 0  # --network none denies egress


# -- P0 review: --memory-swap must be pinned to --memory (hard RAM cap) -----------
#
# Docker defaults --memory-swap to 2x --memory when --memory-swap is unset, so a
# process could still balloon to 2x the configured RAM via swap. Setting
# --memory-swap equal to --memory disables swap entirely, making the RAM cap hard.

def test_memory_swap_pinned_to_memory(monkeypatch):
    argv = _argv(monkeypatch, ExecutionRequest(language="python", code="print(1)"))
    assert "--memory-swap" in argv
    assert argv[argv.index("--memory-swap") + 1] == argv[argv.index("--memory") + 1]


def test_memory_swap_tracks_configured_memory_mb(monkeypatch):
    argv = _argv(
        monkeypatch,
        ExecutionRequest(language="python", code="print(1)"),
        CODE_EXEC_CONTAINER_MEMORY_MB="512",
    )
    assert argv[argv.index("--memory") + 1] == "512m"
    assert argv[argv.index("--memory-swap") + 1] == "512m"


# -- P0 review: _resolve_network invariant (never falls back to host) -------------

def test_resolve_network_unknown_policy_never_falls_back_to_host():
    net = DockerBackend()._resolve_network(
        ExecutionRequest(language="bash", code="true", network="garbage")
    )
    assert net == "none"


def test_resolve_network_host_policy_returns_host():
    net = DockerBackend()._resolve_network(
        ExecutionRequest(language="bash", code="true", network="host")
    )
    assert net == "host"


def test_resolve_network_default_returns_none(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    net = DockerBackend()._resolve_network(
        ExecutionRequest(language="bash", code="true")
    )
    assert net == "none"
