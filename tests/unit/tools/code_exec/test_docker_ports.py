"""WS-4: the persistent dev sandbox publishes ports to host loopback.

Docker can't add ports to a running container, so a dev (posture>=1) persistent
container publishes a configured set of container ports to 127.0.0.1:<ephemeral>
at `docker run -d` time. `published_ports()` reports the resulting map so the agent
(and the loopback SSRF allowlist) knows the reachable host ports. Posture-0 /
non-dev / ephemeral runs publish NOTHING.
"""
import uuid

import pytest

from tools.code_exec.backends.docker import DockerBackend


class _RecordingDocker:
    def __init__(self, port_out=""):
        self.log = []
        self.port_out = port_out

    async def __call__(self, args, *, input=None, timeout=None):
        self.log.append(list(args))
        if args[:2] == ["run", "-d"]:
            return (0, "cid123\n", "")
        if args and args[0] == "port":
            return (0, self.port_out, "")
        return (0, "", "")


def _pin_workdir(monkeypatch, path):
    monkeypatch.setattr(
        "tools.code_exec.backends.docker.DockerBackend._resolve_persistent_workdir",
        lambda self: str(path),
    )


@pytest.mark.asyncio
async def test_dev_setup_publishes_configured_ports_to_loopback(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_EXEC_PUBLISH_PORTS", "8000,5000")
    fake = _RecordingDocker()
    _pin_workdir(monkeypatch, tmp_path)
    b = DockerBackend(session_id=f"t-{uuid.uuid4().hex}", docker_runner=fake, dev_mode=True)
    await b.setup()
    run_d = next(a for a in fake.log if a[:2] == ["run", "-d"])
    # each configured port is published to loopback with a docker-assigned host port
    assert "-p" in run_d
    assert "127.0.0.1::8000" in run_d
    assert "127.0.0.1::5000" in run_d


@pytest.mark.asyncio
async def test_non_dev_setup_publishes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_EXEC_PUBLISH_PORTS", "8000")
    fake = _RecordingDocker()
    _pin_workdir(monkeypatch, tmp_path)
    b = DockerBackend(session_id=f"t-{uuid.uuid4().hex}", docker_runner=fake)  # dev_mode=False
    await b.setup()
    run_d = next(a for a in fake.log if a[:2] == ["run", "-d"])
    assert "-p" not in run_d


def test_ephemeral_argv_never_publishes(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_PUBLISH_PORTS", "8000")
    from tools.code_exec.result import ExecutionRequest
    argv = DockerBackend()._build_run_argv(
        ExecutionRequest(language="python", code="x", dev_mode=True), "/tmp/ws")
    assert "-p" not in argv  # ephemeral --rm runs are not servers; no publish


@pytest.mark.asyncio
async def test_published_ports_parses_docker_port_output(monkeypatch, tmp_path):
    fake = _RecordingDocker(port_out="8000/tcp -> 127.0.0.1:49153\n5000/tcp -> 127.0.0.1:49154\n")
    _pin_workdir(monkeypatch, tmp_path)
    b = DockerBackend(session_id=f"t-{uuid.uuid4().hex}", docker_runner=fake, dev_mode=True)
    await b.setup()
    ports = await b.published_ports()
    assert ports == {8000: 49153, 5000: 49154}


@pytest.mark.asyncio
async def test_published_ports_empty_for_non_dev(monkeypatch, tmp_path):
    fake = _RecordingDocker()
    _pin_workdir(monkeypatch, tmp_path)
    b = DockerBackend(session_id=f"t-{uuid.uuid4().hex}", docker_runner=fake)
    await b.setup()
    assert await b.published_ports() == {}


def test_dev_setup_network_defaults_to_bridge_when_unset(monkeypatch):
    # docker ignores -p under --network none, and a posture-1 dev sandbox is networked
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    b = DockerBackend(session_id="s1", dev_mode=True)
    assert b._resolve_setup_network() == "bridge"


def test_dev_setup_network_explicit_none_still_wins(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_NETWORK", "none")
    b = DockerBackend(session_id="s1", dev_mode=True)
    assert b._resolve_setup_network() == "none"


def test_non_dev_setup_network_unchanged(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    b = DockerBackend(session_id="s1")  # non-dev
    assert b._resolve_setup_network() == "none"
