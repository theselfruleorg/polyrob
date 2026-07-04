import types
import pytest
import core.wallet.factory as wf
from tools.hyperliquid.service import HyperliquidTool


@pytest.mark.asyncio
async def test_exchange_uses_agent_wallet_account_when_enabled(monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "s" * 40)
    wf.reset_agent_wallet_cache()
    wallet = wf.get_agent_wallet()
    expected_addr = wallet.account_for("hyperliquid").address

    captured = {}

    class FakeExchange:
        # Mirrors the real SDK signature:
        # Exchange(wallet: LocalAccount, base_url=None, ..., account_address=None)
        def __init__(self, wallet, base_url=None, meta=None, vault_address=None,
                     account_address=None, *args, **kwargs):
            captured["wallet"] = wallet
            captured["base_url"] = base_url
            captured["account_address"] = account_address

    # Patch the SDK Exchange symbol + force HAS_SDK + a fake info client
    monkeypatch.setattr("tools.hyperliquid.service.Exchange", FakeExchange, raising=False)
    monkeypatch.setattr("tools.hyperliquid.service.HAS_SDK", True, raising=False)

    # NOTE: brief shows config=None but BaseComponent.__init__ raises ValueError on falsy config.
    # Use a truthy stand-in (SimpleNamespace) instead — production behavior is unchanged.
    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "agent"
    monkeypatch.setattr(tool, "_get_info_client", lambda: _async(object()))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(_FakeCreds()))

    ex, err = await tool._get_exchange_client()
    assert err is None
    # The agent-wallet LocalAccount is passed as the SDK `wallet` (signer), not a
    # bare address string, and account_address carries its address.
    assert captured["wallet"].address == expected_addr
    assert captured["account_address"] == expected_addr
    assert captured["base_url"] == "https://api.hyperliquid-testnet.xyz"


def _async(value):
    async def _coro():
        return value
    return _coro()


class _FakeCreds:
    demo_mode = False
    api_url = "https://api.hyperliquid-testnet.xyz"
    def is_configured(self):
        return True
    def can_trade(self):
        return True
