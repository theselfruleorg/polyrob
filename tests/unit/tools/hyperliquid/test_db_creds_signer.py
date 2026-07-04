"""F1 (P0-2): the DB-credentials Exchange() init must pass a LocalAccount signer.

The original else-branch passed `credentials.trading_wallet_address` (a str) where
the SDK expects a LocalAccount signer, and the Info object where base_url goes —
crashing the only trading path for users without an agent wallet.
"""
import types
import pytest
import core.wallet.factory as wf
from tools.hyperliquid.service import HyperliquidTool

# 0x000...001 -> this address (deterministic eth_account test key).
_TEST_KEY = "0x" + "0" * 63 + "1"
_TEST_ADDR = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"


def _async(value):
    async def _coro():
        return value
    return _coro()


class _DbCreds:
    demo_mode = False
    api_url = "https://api.hyperliquid-testnet.xyz"
    # No agent wallet here, so the master account == the signer address.
    wallet_address = _TEST_ADDR
    trading_wallet_address = _TEST_ADDR
    trading_private_key = _TEST_KEY

    def is_configured(self):
        return True

    def can_trade(self):
        return True


@pytest.mark.asyncio
async def test_db_creds_path_passes_local_account_signer(monkeypatch):
    # No agent wallet -> exercise the DB-creds else-branch.
    monkeypatch.delenv("AGENT_WALLET_ENABLED", raising=False)
    wf.reset_agent_wallet_cache()

    captured = {}

    class FakeExchange:
        def __init__(self, wallet, base_url=None, meta=None, vault_address=None,
                     account_address=None, *args, **kwargs):
            captured["wallet"] = wallet
            captured["base_url"] = base_url
            captured["account_address"] = account_address

    monkeypatch.setattr("tools.hyperliquid.service.Exchange", FakeExchange, raising=False)
    monkeypatch.setattr("tools.hyperliquid.service.HAS_SDK", True, raising=False)

    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "dbuser"
    monkeypatch.setattr(tool, "_get_info_client", lambda: _async(object()))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(_DbCreds()))

    ex, err = await tool._get_exchange_client()
    assert err is None
    # `wallet` must be a LocalAccount (has .address), NOT a bare address string.
    assert not isinstance(captured["wallet"], str)
    assert captured["wallet"].address == _TEST_ADDR
    assert captured["base_url"] == "https://api.hyperliquid-testnet.xyz"
    assert captured["account_address"] == _TEST_ADDR


@pytest.mark.asyncio
async def test_db_creds_bad_key_fails_clean_no_key_leak(monkeypatch):
    monkeypatch.delenv("AGENT_WALLET_ENABLED", raising=False)
    wf.reset_agent_wallet_cache()

    class _BadCreds(_DbCreds):
        trading_private_key = "not-a-valid-key"

    class FakeExchange:
        def __init__(self, *a, **k):
            raise AssertionError("Exchange must not be constructed with a bad key")

    monkeypatch.setattr("tools.hyperliquid.service.Exchange", FakeExchange, raising=False)
    monkeypatch.setattr("tools.hyperliquid.service.HAS_SDK", True, raising=False)

    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "dbuser2"
    monkeypatch.setattr(tool, "_get_info_client", lambda: _async(object()))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(_BadCreds()))

    ex, err = await tool._get_exchange_client()
    assert ex is None
    assert err is not None
    # The raw private key must never appear in the surfaced error.
    assert "not-a-valid-key" not in err
