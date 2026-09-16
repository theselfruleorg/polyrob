from unittest.mock import Mock

import pytest

from tools.code_exec.sandbox_guard import require_sandbox_or_none, code_exec_execution_blocked_reason


@pytest.fixture
def custody(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("CODE_EXEC_BACKEND", "local_subprocess")


def test_local_mode_cannot_bypass_custody_sandbox(custody):
    assert require_sandbox_or_none("local_subprocess")
    assert code_exec_execution_blocked_reason()
    assert require_sandbox_or_none("docker") is None


def test_seed_presence_alone_requires_sandbox(custody, monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "false")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "test-only-not-a-real-seed")
    assert require_sandbox_or_none("local_subprocess")


def test_lsp_does_not_spawn_on_custody_host(custody, monkeypatch):
    from tools.coding import lsp
    spawn = Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(lsp.subprocess, "run", spawn)
    assert lsp.diagnose_file("x.py", "/tmp") == ""
    spawn.assert_not_called()


def test_snapshot_does_not_spawn_on_custody_host(custody, monkeypatch, tmp_path):
    from tools.coding import snapshot
    spawn = Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(snapshot.subprocess, "run", spawn)
    assert snapshot.snapshot_file(str(tmp_path / "snapshot"), str(tmp_path), "x.py") is None
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_git_does_not_run_workspace_hooks_on_custody_host(custody, monkeypatch):
    from tools.git.tool import GitTool
    import subprocess
    spawn = Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(subprocess, "run", spawn)
    ok, message = await GitTool(config=Mock())._run_git(["status"])
    assert not ok and "custody" in message
    spawn.assert_not_called()


@pytest.mark.parametrize('name', ['PAYMENT_MASTER_SEED', 'MASTER_SEED'])
def test_deposit_seed_requires_custody_isolation(custody, monkeypatch, name):
    from core.security.host_execution import host_execution_refusal
    monkeypatch.setenv('AGENT_WALLET_ENABLED', 'false')
    monkeypatch.delenv('AGENT_WALLET_MASTER_SEED', raising=False)
    monkeypatch.delenv('PAYMENT_MASTER_SEED', raising=False)
    monkeypatch.delenv('MASTER_SEED', raising=False)
    monkeypatch.setenv(name, 'synthetic-test-seed-not-a-wallet')
    assert require_sandbox_or_none('local_subprocess')
    message = host_execution_refusal()
    assert message and 'synthetic-test-seed' not in message
    assert require_sandbox_or_none('docker') is None
