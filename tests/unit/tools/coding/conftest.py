import pytest


@pytest.fixture(autouse=True)
def _owner_tenant_for_host_exec(monkeypatch):
    """These tests exercise the coding tool, not WHO may run code. The
    local-mode owner-tenant gate (CHAT-1) has its own test:
    tests/unit/tools/test_host_exec_owner_gate.py."""
    monkeypatch.setattr("tools.code_exec.sandbox_guard.local_host_exec_refusal",
                        lambda ctx: None)
