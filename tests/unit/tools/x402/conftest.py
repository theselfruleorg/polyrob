"""Test isolation for the agent-wallet factory singleton.

``core.wallet.factory.get_agent_wallet`` caches a process-level singleton, and
the wallet is configured from ``AGENT_WALLET_*`` / ``X402_CLIENT_*`` env vars.
Without isolation, a test that enables the wallet poisons the cache (and a leaked
env var poisons config) for later tests — e.g. ``test_disabled_wallet_errors_cleanly``
would see a cached enabled wallet. This autouse fixture gives every test in this
directory a clean env + clean cache, before and after.
"""
import pytest

import core.wallet.factory as _factory

_WALLET_ENV = [
    "AGENT_WALLET_ENABLED",
    "AGENT_WALLET_BACKEND",
    "AGENT_WALLET_MASTER_SEED",
    "AGENT_WALLET_NETWORK",
    "AGENT_WALLET_MAX_PER_TX_USD",
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
