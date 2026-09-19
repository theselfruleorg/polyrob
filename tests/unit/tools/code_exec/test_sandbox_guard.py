"""P0 Task 4 — sandbox-required invariant."""
from tools.code_exec.sandbox_guard import (
    require_sandbox_or_none,
    code_exec_execution_blocked_reason,
)


def test_local_mode_always_allows(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    assert require_sandbox_or_none("local_subprocess") is None
    assert code_exec_execution_blocked_reason() is None


def test_server_local_subprocess_refused(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    reason = require_sandbox_or_none("local_subprocess")
    assert reason and "not a sandbox" in reason


def test_server_docker_allowed(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    assert require_sandbox_or_none("docker") is None


def test_server_exec_blocked_when_disabled(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("CODE_EXEC_ENABLED", raising=False)
    reason = code_exec_execution_blocked_reason()
    assert reason and "disabled" in reason


def test_server_exec_blocked_enabled_but_not_sandbox(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("CODE_EXEC_ENABLED", "true")
    monkeypatch.setenv("CODE_EXEC_BACKEND", "local_subprocess")
    reason = code_exec_execution_blocked_reason()
    assert reason and "not a sandbox" in reason


def test_server_exec_allowed_with_docker(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("CODE_EXEC_ENABLED", "true")
    monkeypatch.setenv("CODE_EXEC_BACKEND", "docker")
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    assert code_exec_execution_blocked_reason() is None


# --- 2026-09-19 (prod tick 93): an unreachable Docker socket is a refusal, not a retry ---
#
# Since the 2026-09-16 identity cutover the agent runs as `polyrob-agent`, which is
# deliberately NOT in the `docker` group (security-rollout: rootful Docker authority
# removed until the rootless sandbox of proposal 053 lands). `coding.run_tests` still
# reached `docker run -d` and failed with "permission denied while trying to connect
# to the docker API" — an error that reads like a transient, so the agent retried
# (16 failures across 4 attempts in the last 24 h). The guard now says the real
# reason ONCE, before any docker call, and tells the agent not to retry.

def test_docker_socket_unreachable_is_named(monkeypatch, tmp_path):
    import os
    from tools.code_exec import sandbox_guard as g
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    monkeypatch.setenv("DOCKER_HOST", f"unix://{sock}")
    monkeypatch.setattr(g.os, "access", lambda p, m: False)
    reason = g.docker_socket_unreachable_reason("docker")
    assert reason and "053" in reason and "not retry" in reason.lower()
    assert str(sock) in reason


def test_docker_socket_reachable_is_silent(monkeypatch, tmp_path):
    from tools.code_exec import sandbox_guard as g
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    monkeypatch.setenv("DOCKER_HOST", f"unix://{sock}")
    monkeypatch.setattr(g.os, "access", lambda p, m: True)
    assert g.docker_socket_unreachable_reason("docker") is None


def test_docker_socket_check_only_applies_to_unix_docker(monkeypatch):
    from tools.code_exec import sandbox_guard as g
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
    assert g.docker_socket_unreachable_reason("docker") is None, "a TCP daemon has no socket file"
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(g.os, "access", lambda p, m: False)
    assert g.docker_socket_unreachable_reason("local_subprocess") is None, "not docker"


def test_blocked_reason_names_the_socket_before_docker_runs(monkeypatch, tmp_path):
    from tools.code_exec import sandbox_guard as g
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("CODE_EXEC_ENABLED", "true")
    monkeypatch.setenv("CODE_EXEC_BACKEND", "docker")
    monkeypatch.delenv("CODE_EXEC_NETWORK", raising=False)
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    monkeypatch.setenv("DOCKER_HOST", f"unix://{sock}")
    monkeypatch.setattr(g.os, "access", lambda p, m: False)
    reason = g.code_exec_execution_blocked_reason()
    assert reason and "053" in reason
