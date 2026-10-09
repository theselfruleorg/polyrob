"""Keep the dev-mode ``/install`` dirs (H02: now OUTSIDE the workspace, under the
data home) inside a per-test tmp dir so no test writes to a real data home."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_install_root(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("sandbox_installs")
    monkeypatch.setattr("tools.code_exec.backends.docker._install_root", lambda: str(root))
    return str(root)


@pytest.fixture(autouse=True)
def _owner_tenant_for_host_exec(monkeypatch):
    """These tests exercise the code_exec mechanics, not WHO may run code. The
    local-mode owner-tenant gate (CHAT-1) has its own test:
    tests/unit/tools/test_host_exec_owner_gate.py."""
    monkeypatch.setattr("tools.code_exec.sandbox_guard.local_host_exec_refusal",
                        lambda ctx: None)
