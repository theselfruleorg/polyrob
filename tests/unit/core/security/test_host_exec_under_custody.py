"""Owner decision D1 (2026-10-10): the agent uses the wallet AND keeps full host
capabilities. Host children (git, MCP stdio, lazy installs, coding snapshots and
LSP, self_env, packs) run under wallet custody in every signer mode. The key is
protected by isolation (scrubbed child env, non-dumpable process, the signer),
not by refusing the tool. The owner may still refuse through ONE settings row.
The only fixed refusal is the isolation floor: a key that a child could read."""
import asyncio
from unittest.mock import Mock

import pytest

import core.security.custody_env as ce
import core.security.host_execution as he
import core.security.process_hardening as ph
from core.security import settings as ss

SEED = "test-only-not-a-real-seed-" * 3


@pytest.fixture
def custody(monkeypatch):
    for name in (*ce.CUSTODY_SECRET_ENV, ss.ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("WALLET_SIGNER", "local")
    ce._reset_for_tests()
    # isolation holds (the prod shape: prctl held, env scrubbed)
    monkeypatch.setattr(ph, "hardening_state",
                        lambda: ph.HardeningResult(ph.STATE_HELD, "test: held"))
    yield
    ce._reset_for_tests()


@pytest.mark.parametrize("mode", ["local", "shadow", "remote"])
def test_host_execution_runs_under_custody_in_every_signer_mode(custody, monkeypatch, mode):
    monkeypatch.setenv("WALLET_SIGNER", mode)
    assert he.host_execution_refusal() is None


def test_a_held_seed_with_isolation_runs(custody, monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    ce.take_custody_secrets()
    assert he.host_execution_refusal() is None


@pytest.mark.parametrize("mode", ["local", "shadow", "remote"])
def test_the_owner_setting_refuses_in_every_signer_mode(custody, monkeypatch, mode):
    monkeypatch.setenv("WALLET_SIGNER", mode)
    monkeypatch.setenv(ss.ENV, "host_exec_under_custody=refuse")
    why = he.host_execution_refusal()
    assert why and "host_exec_under_custody=refuse" in why and "SECURITY_SETTINGS" in why


def test_no_custody_never_refuses_even_with_the_setting(monkeypatch):
    for name in (*ce.CUSTODY_SECRET_ENV, "AGENT_WALLET_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WALLET_SIGNER", "local")
    monkeypatch.setenv(ss.ENV, "host_exec_under_custody=refuse")
    ce._reset_for_tests()
    assert he.host_execution_refusal() is None


def test_the_isolation_floor_refuses_when_a_child_could_read_the_key(custody, monkeypatch):
    """Floor row ``key_isolation_gap``: not a setting, never natural work."""
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    ce.take_custody_secrets()
    monkeypatch.setattr(ph, "hardening_state",
                        lambda: ph.HardeningResult(ph.STATE_FAILED, "mock prctl failure"))
    why = he.host_execution_refusal()
    assert why and "could read" in why and SEED not in why


def test_git_runs_under_custody(custody, monkeypatch, tmp_path):
    from tools.git.tool import GitTool
    import subprocess
    seen = {}

    def _run(argv, **kw):
        seen["env"] = kw.get("env")
        return subprocess.CompletedProcess(argv, 0, b"clean", b"")
    monkeypatch.setattr(subprocess, "run", _run)
    tool = GitTool(config=Mock())
    monkeypatch.setattr(tool, "_resolve_root", lambda ctx: str(tmp_path))
    ok, _ = asyncio.run(tool._run_git(["status"]))
    assert ok
    assert not set(seen["env"]) & set(ce.CUSTODY_SECRET_ENV)


@pytest.mark.asyncio
async def test_mcp_stdio_spawns_under_custody_without_the_key(custody, monkeypatch):
    """The child env never carries a custody secret, even when a config names one."""
    from tools.mcp.protocol import MCPStdioTransport
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    ce.take_custody_secrets()
    seen = {}

    def _popen(cmd, **kw):
        seen["env"] = kw.get("env")
        raise RuntimeError("stop after spawn")
    monkeypatch.setattr("tools.mcp.protocol.subprocess.Popen", _popen)
    transport = MCPStdioTransport(command=["python", "server.py"],
                                  env={"AGENT_WALLET_MASTER_SEED": SEED, "LEAK": SEED,
                                       "MCP_GATEWAY_TOKEN": "tok"})
    with pytest.raises(Exception):
        await transport.connect()
    env = seen["env"]
    assert "AGENT_WALLET_MASTER_SEED" not in env and SEED not in env.values()
    assert env.get("MCP_GATEWAY_TOKEN") == "tok"   # an operator-configured credential still passes


def test_lsp_and_snapshot_spawn_under_custody(custody, monkeypatch, tmp_path):
    from tools.coding import lsp, snapshot
    calls = []

    def _run(*a, **kw):
        calls.append(kw.get("env"))
        import subprocess
        return subprocess.CompletedProcess(a, 1, "", "")
    monkeypatch.setattr(lsp.subprocess, "run", _run)
    monkeypatch.setattr(snapshot.subprocess, "run", _run)
    lsp.default_runner(["true"], str(tmp_path), 1)
    snapshot._run(str(tmp_path / "s"), str(tmp_path), ["status"])
    assert len(calls) == 2


def test_packs_load_under_custody_by_default_and_the_owner_can_refuse(custody, monkeypatch):
    from core.packs import loader
    for mode in ("local", "shadow", "remote"):
        monkeypatch.setenv("WALLET_SIGNER", mode)
        assert loader.custody_refusal() is None, mode
    monkeypatch.setenv(ss.ENV, "packs_under_custody=refuse")
    why = loader.custody_refusal()
    assert why and "packs_under_custody=refuse" in why
    # the host-children row does not govern packs (a pack is not a child process)
    monkeypatch.setenv(ss.ENV, "host_exec_under_custody=refuse")
    assert loader.custody_refusal() is None


def test_self_env_is_not_disabled_by_custody(custody, monkeypatch):
    from tools.self_env.tool import SelfEnvTool
    monkeypatch.setattr("core.config_policy.compute_posture_allows_safe", lambda ctx, n: True)
    assert SelfEnvTool._allowed(object()) is True
    monkeypatch.setenv(ss.ENV, "host_exec_under_custody=refuse")
    assert SelfEnvTool._allowed(object()) is False


def test_local_run_code_follows_the_same_row(custody, monkeypatch):
    from tools.code_exec import sandbox_guard
    monkeypatch.setattr(sandbox_guard, "docker_socket_unreachable_reason", lambda b: None)
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    assert sandbox_guard.require_sandbox_or_none("local_subprocess") is None
    monkeypatch.setenv(ss.ENV, "host_exec_under_custody=refuse")
    assert sandbox_guard.require_sandbox_or_none("local_subprocess")
