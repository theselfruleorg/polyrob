"""073 W8: the sandbox egress allowlist proxy (pure, no network — a local upstream)."""
import asyncio

import pytest

from tools.code_exec.egress_proxy import (
    DEFAULT_ALLOW, EgressProxy, host_allowed, parse_allowlist, parse_ports)


def test_parse_allowlist():
    assert parse_allowlist(None) == DEFAULT_ALLOW
    assert parse_allowlist("a.com, B.com") == ("a.com", "b.com")
    assert parse_allowlist("+x.org")[-1] == "x.org" and "pypi.org" in parse_allowlist("+x.org")


def test_host_allowed():
    allow = ("pypi.org", ".githubusercontent.com", "*.npmjs.org")
    assert host_allowed("pypi.org", allow)
    assert host_allowed("PYPI.ORG.", allow)
    assert not host_allowed("evil-pypi.org", allow)
    assert not host_allowed("pypi.org.evil.com", allow)
    assert host_allowed("objects.githubusercontent.com", allow)
    assert host_allowed("registry.npmjs.org", allow)
    assert not host_allowed("", allow)
    assert not host_allowed("169.254.169.254", allow)


def test_parse_ports():
    assert parse_ports(None) == frozenset({80, 443})
    assert parse_ports("8443, x, 70000") == frozenset({80, 443, 8443})


async def _request(port, data: bytes) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(data)
    await w.drain()
    out = await asyncio.wait_for(r.read(4096), 5)
    w.close()
    return out


@pytest.mark.asyncio
async def test_proxy_refuses_and_forwards():
    # a local "upstream" on an allowed host/port
    async def upstream(r, w):
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await w.drain()
        w.close()
    up = await asyncio.start_server(upstream, "127.0.0.1", 0)
    up_port = up.sockets[0].getsockname()[1]
    proxy = EgressProxy(allow=("127.0.0.1",), ports=(up_port, 443))
    srv = await proxy.serve("127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        refused = await _request(port, b"CONNECT evil.com:443 HTTP/1.1\r\nHost: evil.com\r\n\r\n")
        assert refused.startswith(b"HTTP/1.1 403") and b"allowlist" in refused
        bad_port = await _request(port, b"CONNECT 127.0.0.1:22 HTTP/1.1\r\n\r\n")
        assert bad_port.startswith(b"HTTP/1.1 403") and b"port 22" in bad_port
        ok = await _request(port, f"GET http://127.0.0.1:{up_port}/x HTTP/1.1\r\nHost: a\r\n\r\n".encode())
        assert ok.startswith(b"HTTP/1.1 200") and ok.endswith(b"ok")
        tunnel = await _request(port, f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
        assert tunnel.startswith(b"HTTP/1.1 200 Connection Established")
        assert proxy.refused == 2
    finally:
        srv.close()
        up.close()


# --- docker wiring (fake docker CLI) ---------------------------------------------

import uuid as _uuid

from tools.code_exec.backends.docker import DockerBackend


class _Docker:
    def __init__(self, fail_on=None):
        self.log = []
        self.fail_on = fail_on

    async def __call__(self, args, *, input=None, timeout=None):
        self.log.append(list(args))
        if self.fail_on and args[:2] == self.fail_on:
            return 1, "", "boom"
        if args[:2] == ["run", "-d"]:
            return 0, "cid\n", ""
        return 0, "", ""


@pytest.mark.asyncio
async def test_proxy_setup_builds_internal_net_and_sidecar(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_EXEC_NETWORK", "proxy")
    monkeypatch.setenv("CODE_EXEC_EGRESS_ALLOW", "+example.org")
    fake = _Docker()
    b = DockerBackend(session_id=f"t-{_uuid.uuid4().hex}", docker_runner=fake, dev_mode=True)
    monkeypatch.setattr(b, "_resolve_persistent_workdir", lambda: str(tmp_path))
    await b.setup()
    cmds = [a[:2] for a in fake.log]
    assert cmds[0] == ["network", "create"] and "--internal" in fake.log[0]
    side = fake.log[1]
    assert side[:2] == ["run", "-d"] and "--read-only" in side and "--cap-drop" in side
    assert "example.org" in side[-2] and "pypi.org" in side[-2]
    assert fake.log[2][:3] == ["network", "connect", "--alias"]
    main = fake.log[3]
    net = fake.log[0][-1]
    assert main[main.index("--network") + 1] == net
    assert "HTTPS_PROXY=http://polyrob-egress:3128" in main
    assert "-p" not in main  # no publish: an internal network has no route
    await b.teardown()
    assert ["network", "rm", net] in fake.log
    assert any(a[:2] == ["rm", "-f"] and a[2].startswith("polyrob-egp-") for a in fake.log)


@pytest.mark.asyncio
async def test_proxy_setup_failure_never_falls_open(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_EXEC_NETWORK", "proxy")
    fake = _Docker(fail_on=["network", "connect"])
    b = DockerBackend(session_id=f"t-{_uuid.uuid4().hex}", docker_runner=fake, dev_mode=True)
    monkeypatch.setattr(b, "_resolve_persistent_workdir", lambda: str(tmp_path))
    from tools.code_exec.backend import ExecutionBackendError
    with pytest.raises(ExecutionBackendError):
        await b.setup()
    assert not any(a[:2] == ["run", "-d"] and "sleep" in a for a in fake.log)
    assert any(a[:2] == ["network", "rm"] for a in fake.log)


def test_ephemeral_proxy_means_no_network(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_NETWORK", "proxy")
    from tools.code_exec.result import ExecutionRequest
    assert DockerBackend()._resolve_network(ExecutionRequest(language="bash", code="true")) == "none"
