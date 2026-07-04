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
