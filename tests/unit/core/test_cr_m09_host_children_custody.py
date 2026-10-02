"""CR-M09: lazy pip installs and the anysite CLI obey the wallet-custody rule."""
import asyncio
import subprocess

import pytest

import core.lazy_deps as ld
from core.lazy_deps import FeatureUnavailable


@pytest.fixture
def lazy_on(monkeypatch):
    monkeypatch.setenv("LAZY_DEPS_ENABLED", "1")
    monkeypatch.setattr(ld, "_dist_present", lambda name: False)
    for n in ("AGENT_WALLET_ENABLED", "AGENT_WALLET_MASTER_SEED",
              "PAYMENT_MASTER_SEED", "MASTER_SEED"):
        monkeypatch.delenv(n, raising=False)


def test_lazy_ensure_refuses_under_custody(lazy_on, monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "s" * 40)
    calls = []
    monkeypatch.setattr(ld, "_run_installer", lambda cmd, **kw: calls.append(cmd))
    feature = next(iter(ld.LAZY_DEPS))
    with pytest.raises(FeatureUnavailable, match="wallet custody"):
        ld.ensure(feature, prompt=False)
    assert calls == []


def test_pip_child_env_drops_secret_named_vars(lazy_on, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("PIP_INDEX_URL", "https://pypi.org/simple")
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw.get("env") or {})
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ld.subprocess, "run", fake_run)
    ld._run_installer(["pip", "--version"])
    assert "OPENAI_API_KEY" not in seen
    assert seen.get("PIP_INDEX_URL") == "https://pypi.org/simple"
    assert "PATH" in seen


def _hardening(monkeypatch, state):
    import core.security.process_hardening as ph
    monkeypatch.setattr(ph, "_apply", lambda: ph.HardeningResult(state, f"test {state}"))
    ph._reset_for_tests()
    monkeypatch.setattr(ph, "_result", None)


def test_anysite_cli_refused_under_custody_while_dumpable(monkeypatch):
    """066 D4: the refusal stays only where /proc/<agent>/environ is still open."""
    client = pytest.importorskip("polyrob_discovery.anysite.client")
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    _hardening(monkeypatch, "failed")

    async def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("anysite CLI spawned under custody while dumpable")

    monkeypatch.setattr(client.asyncio, "create_subprocess_exec", boom)
    res = asyncio.run(client.run_anysite(["anysite", "api", "/x"]))
    assert res.exit_code == -1 and "wallet custody" in res.stderr and "dumpable" in res.stderr


def test_anysite_configure_refused_under_custody_while_dumpable(monkeypatch):
    client = pytest.importorskip("polyrob_discovery.anysite.client")
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    _hardening(monkeypatch, "failed")
    monkeypatch.setattr(client, "api_key", lambda: "k")
    monkeypatch.setattr(client, "binary_path", lambda: "/bin/true")
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))
    assert client.ensure_configured() is False
    assert ran == []


@pytest.mark.parametrize("state", ["held", "unsupported"])
def test_anysite_cli_runs_under_custody_with_a_scrubbed_env(monkeypatch, state):
    """066 D4: after P0 the CLI runs again — and its env holds no seed and no
    provider key, only the anysite credential it is meant to have."""
    client = pytest.importorskip("polyrob_discovery.anysite.client")
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "s" * 40)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leak")
    monkeypatch.setattr(client, "api_key", lambda: "anysite-key")
    _hardening(monkeypatch, state)
    seen = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    async def fake_exec(*argv, **kw):
        seen["argv"] = argv
        seen["env"] = kw.get("env")
        return _Proc()

    monkeypatch.setattr(client.asyncio, "create_subprocess_exec", fake_exec)
    res = asyncio.run(client.run_anysite(["anysite", "api", "/x"]))
    assert res.exit_code == 0 and res.stdout == "ok"
    env = seen["env"]
    assert "AGENT_WALLET_MASTER_SEED" not in env and "OPENAI_API_KEY" not in env
    assert "s" * 40 not in env.values() and "sk-leak" not in env.values()
    assert "anysite-key" in env.values()


def test_other_host_children_keep_the_custody_refusal(monkeypatch):
    """D4 relaxes anysite ONLY: the shared rule still refuses under custody,
    hardened or not (git, LSP, MCP stdio, lazy installs — until the signer)."""
    from core.security.host_execution import host_execution_refusal
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    _hardening(monkeypatch, "held")
    assert "wallet custody" in (host_execution_refusal() or "")
