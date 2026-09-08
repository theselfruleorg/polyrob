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
def _offline_url_validation(request, monkeypatch):
    """The x402 unit suite must never touch DNS. Allow every URL and pin nothing;
    the SSRF policy itself is covered by tests/unit/tools/x402/test_net_guard.py.

    Deliberately skipped for test_net_guard.py itself: those tests call
    ``validate_x402_url`` directly to exercise its REAL refuse/allow/pin
    behaviour, and a blanket allow-everything patch on the very function under
    test would silently defeat every assertion in that file (verified: without
    this exclusion, 5 of test_net_guard.py's 7 tests fail).
    """
    if request.module.__name__.rsplit(".", 1)[-1] == "test_net_guard":
        yield
        return
    async def _allow(url, *, validator=None):
        return None, None
    monkeypatch.setattr("tools.x402.net_guard.validate_x402_url", _allow, raising=False)
    yield
