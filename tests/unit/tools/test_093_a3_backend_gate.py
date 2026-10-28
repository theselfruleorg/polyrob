"""093 A3: the exec backend gate (shell / process / code_execution) + doctor line."""
import os
import socket
import tempfile

import pytest

import tools  # noqa: F401  (registers the gates and the explain probe)
from tools.code_exec import backend_probe


@pytest.fixture(autouse=True)
def _fresh_probe(monkeypatch):
    for k in ("CODE_EXEC_BACKEND", "DOCKER_HOST", "SHELL_BACKEND", "CODE_EXEC_SSH_HOST",
              "CODE_EXEC_DOCKER_BINARY"):
        monkeypatch.delenv(k, raising=False)
    backend_probe.reset_probe_cache()
    yield
    backend_probe.reset_probe_cache()


class _Container:
    def __init__(self, services):
        self._s = services

    def has_service(self, name):
        return name in self._s

    def get_service(self, name):
        return self._s[name]


# -- A3: the exec backend gate --------------------------------------------------

def test_docker_socket_missing_is_a_gap(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_BACKEND", "docker")
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    reason = backend_probe.backend_unavailable_reason()
    assert reason and "does not exist" in reason
    gap = backend_probe.shell_backend_gap()
    from core.tool_gates import remedy_text
    assert gap[0] == "backend-unavailable"
    assert gap[1].startswith(remedy_text("backend-unavailable", flag="docker"))


def test_docker_socket_that_refuses_a_connection_is_a_gap(monkeypatch):
    d = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(d, "d.sock")
    open(path, "w").close()  # a readable file, not a listening socket
    monkeypatch.setenv("DOCKER_HOST", f"unix://{path}")
    assert "does not accept a connection" in backend_probe.backend_unavailable_reason("docker")


def test_docker_socket_that_accepts_is_no_gap(monkeypatch):
    d = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(d, "d.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    try:
        monkeypatch.setenv("DOCKER_HOST", f"unix://{path}")
        monkeypatch.setenv("CODE_EXEC_BACKEND", "docker")
        assert backend_probe.backend_unavailable_reason() is None
        assert backend_probe.shell_backend_gap() is None
    finally:
        srv.close()


def test_probe_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(backend_probe, "_probe", lambda name: calls.append(name) or "x")
    for _ in range(5):
        assert backend_probe.backend_unavailable_reason("docker") == "x"
    assert calls == ["docker"]


def test_shell_gap_skips_host_and_remote_choices(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_BACKEND", "docker")
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    monkeypatch.setenv("SHELL_BACKEND", "host")
    assert backend_probe.shell_backend_gap() is None
    monkeypatch.setenv("SHELL_BACKEND", "ssh")
    assert backend_probe.shell_backend_gap() is None


def test_shell_gap_on_a_non_sandbox_backend(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_BACKEND", "local_subprocess")
    monkeypatch.setattr("core.config_policy.compute_posture", lambda: 1)
    gap = backend_probe.shell_backend_gap()
    assert gap and gap[0] == "backend-unavailable" and "not a sandbox" in gap[1]


def test_catalog_reports_a_shell_whose_backend_cannot_start(monkeypatch):
    """The prod incident: docker socket permission denied, catalog said loadable."""
    monkeypatch.setenv("CODE_EXEC_BACKEND", "docker")
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    from tools.shell.process_tool import ProcessTool
    from tools.shell.tool import ShellTool
    from tools.tool_disclosure import resolve_tool_status
    c = _Container({"shell": object.__new__(ShellTool),
                    "process": object.__new__(ProcessTool)})
    for tid in ("shell", "process"):
        st = resolve_tool_status(tid, container=c, loaded_ids=set())
        assert (st.status, st.reason) == ("gated", "backend-unavailable")
        assert "CODE_EXEC_BACKEND" in st.remedy


def test_code_execution_gap_follows_the_backend(monkeypatch):
    from tools.code_exec.tool import CodeExecutionTool
    monkeypatch.setenv("CODE_EXEC_BACKEND", "docker")
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    gap = object.__new__(CodeExecutionTool).availability_gap()
    assert gap and gap[0] == "backend-unavailable"


def test_doctor_names_which_backends_work(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    line = backend_probe.doctor_backend_line({"CODE_EXEC_BACKEND": "docker"})
    assert line.startswith(f"code-exec backends (as uid {os.getuid()} ")
    assert "docker (configured): unavailable" in line
    assert "local_subprocess: ok (not a sandbox)" in line
    assert "ssh: not configured" in line


def test_doctor_line_names_the_uid_it_probed(monkeypatch):
    """Root reaches docker.sock; the agent uid may not. The line says whom it probed."""
    import pwd
    monkeypatch.setattr(backend_probe.os, "getuid", lambda: 4242, raising=False)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: type("P", (), {"pw_name": "agentx"})())
    assert backend_probe.probe_identity() == "uid 4242 agentx"
    line = backend_probe.doctor_backend_line({"CODE_EXEC_BACKEND": "local_subprocess"})
    assert line.startswith("code-exec backends (as uid 4242 agentx): ")


def test_probe_identity_without_a_passwd_row(monkeypatch):
    import pwd

    def _missing(uid):
        raise KeyError(uid)
    monkeypatch.setattr(backend_probe.os, "getuid", lambda: 9999, raising=False)
    monkeypatch.setattr(pwd, "getpwuid", _missing)
    assert backend_probe.probe_identity() == "uid 9999 no name (KeyError)"
