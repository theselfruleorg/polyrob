"""Test isolation for the agent-wallet factory singleton.

See tests/unit/tools/x402/conftest.py for the rationale: ``get_agent_wallet``
caches a process-level singleton configured from ``AGENT_WALLET_*`` env vars, so
each test needs a clean env + clean cache to avoid cross-test poisoning.
"""
import pytest

import core.wallet.factory as _factory

_WALLET_ENV = [
    "AGENT_WALLET_ENABLED",
    "AGENT_WALLET_BACKEND",
    "AGENT_WALLET_MASTER_SEED",
    "AGENT_WALLET_NETWORK",
    "AGENT_WALLET_MAX_PER_TX_USD",
    # H3 (2026-08-22): WALLET_DAILY_CAP_USD now has a FINITE default, so a
    # leaked value from the real shell env (or a prior test) can silently
    # change daily_cap_usd for any zero-arg load_wallet_config() call in this
    # directory — reset it alongside the other wallet env vars.
    "WALLET_DAILY_CAP_USD",
    "X402_CLIENT_ENABLED",
    "X402_CLIENT_FACILITATOR_URL",
]


@pytest.fixture(autouse=True)
def _reset_agent_wallet(monkeypatch):
    for key in _WALLET_ENV:
        monkeypatch.delenv(key, raising=False)
    _factory.reset_agent_wallet_cache()
    yield
    _factory.reset_agent_wallet_cache()


@pytest.fixture(autouse=True)
def _isolate_polyrob_home(tmp_path, monkeypatch):
    """G-13: load_wallet_config() fail-open-resolves an owner/home for its pref
    merge whenever a caller doesn't pass user_id/home_dir explicitly (see
    core/wallet/config.py). Isolate POLYROB_HOME across every test in this
    directory so none of them can pick up a REAL preferences.toml from the
    machine running the suite — this covers zero-arg load_wallet_config(env)
    calls in test_per_venue_cap.py / test_operational_venue.py, not just
    test_config.py."""
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "_isolated_polyrob_home"))


@pytest.fixture(autouse=True)
def _no_leaked_alchemy_key(monkeypatch):
    """Assert RPC defaults against a CLEAN env.

    A CLI test earlier in a full run calls ``load_env(local_mode=True)``, which
    leaks the developer's real ``~/.polyrob/.env`` — including
    ``ALCHEMY_API_KEY`` — into ``os.environ``. Since 2026-08-24
    ``core.wallet.onchain.rpc_url_for_chain`` composes an Alchemy endpoint from
    that key when no ``DEFI_EVM_RPC_<CHAIN>`` pin is set, so a leaked key
    silently changes what "the default endpoint" resolves to and these tests
    fail only in the full suite. Same precedent as
    tests/unit/tools/defi/conftest.py. A test that WANTS the key sets it
    explicitly with monkeypatch.
    """
    monkeypatch.delenv("ALCHEMY_API_KEY", raising=False)
