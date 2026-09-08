"""Shared env isolation for the x402 unit tests."""
import pytest


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
