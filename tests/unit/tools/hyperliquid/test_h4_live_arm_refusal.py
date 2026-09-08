"""H4 (2026-08-22 audit) — while the polyrob-wallet Hyperliquid path signs with
a key that fully owns its own account (no approveAgent split), arming live
trading on that path must REFUSE instead of trading behind a withdrawal
firewall the code does not provide. Dry-run (flags off) stays available.
"""
import types
import pytest

import core.wallet.factory as wf
from tools.hyperliquid.service import HyperliquidTool
from tools.hyperliquid.models import HyperliquidCredentials


def _async(value):
    async def _coro(*a, **k):
        return value
    return _coro()


class _FakeCreds:
    demo_mode = False
    api_url = "https://api.hyperliquid-testnet.xyz"

    def is_configured(self):
        return True

    def can_trade(self):
        return True


class _FakeExchange:
    def __init__(self, wallet, base_url=None, meta=None, vault_address=None,
                 account_address=None, *args, **kwargs):
        self.wallet = wallet
        self.account_address = account_address


def _wallet_tool(monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "s" * 40)
    wf.reset_agent_wallet_cache()
    monkeypatch.setattr("tools.hyperliquid.service.Exchange", _FakeExchange, raising=False)
    monkeypatch.setattr("tools.hyperliquid.service.HAS_SDK", True, raising=False)
    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "agent"
    monkeypatch.setattr(tool, "_get_info_client", lambda: _async(object()))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(_FakeCreds()))
    return tool


@pytest.mark.asyncio
async def test_live_armed_refuses_on_unfirewalled_wallet_path(monkeypatch):
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    tool = _wallet_tool(monkeypatch)
    ex, err = await tool._get_exchange_client()
    assert ex is None
    assert err is not None and "H4" in err and "approveAgent" in err


@pytest.mark.asyncio
async def test_dry_run_client_still_constructed_when_not_armed(monkeypatch):
    monkeypatch.delenv("CRYPTO_TRADE_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("HYPERLIQUID_TRADING_ENABLED", raising=False)
    tool = _wallet_tool(monkeypatch)
    ex, err = await tool._get_exchange_client()
    assert err is None
    assert ex is not None


@pytest.mark.asyncio
async def test_master_switch_alone_does_not_refuse(monkeypatch):
    # Live master on, venue off => the venue never goes live, so the client may
    # still be constructed for dry-run.
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.delenv("HYPERLIQUID_TRADING_ENABLED", raising=False)
    tool = _wallet_tool(monkeypatch)
    ex, err = await tool._get_exchange_client()
    assert err is None
    assert ex is not None


@pytest.mark.asyncio
async def test_agent_status_reports_polyrob_signer_and_h4(monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "s" * 40)
    wf.reset_agent_wallet_cache()
    expected = wf.get_agent_wallet().account_for("hyperliquid").address

    creds = HyperliquidCredentials(
        user_id="u1",
        wallet_address="0x000000000000000000000000000000000000aaaa",
        private_key="0x" + "1" * 64,
    )
    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(creds))

    res = await tool.agent_status(types.SimpleNamespace())
    assert res["success"] is True
    assert res["delegated"] is False
    assert "polyrob-wallet" in res["signer"] and "H4" in res["signer"]
    assert res["master_address"] == expected
    assert ("1" * 64) not in str(res)


@pytest.mark.asyncio
async def test_agent_status_db_path_unchanged_without_wallet(monkeypatch):
    monkeypatch.delenv("AGENT_WALLET_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    wf.reset_agent_wallet_cache()
    creds = HyperliquidCredentials(
        user_id="u1",
        wallet_address="0x000000000000000000000000000000000000aaaa",
        private_key="0x" + "1" * 64,
    )
    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(creds))

    res = await tool.agent_status(types.SimpleNamespace())
    assert res["signer"] == "master"
    assert res["master_address"] == creds.wallet_address
