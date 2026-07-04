"""T9 + T10(addr) — Hyperliquid reads and delegated-signer trading must target the
MASTER account address, never the agent/API-wallet address (which returns empty).
"""
import types
import pytest

import core.wallet.factory as wf
from tools.hyperliquid.service import HyperliquidTool
from tools.hyperliquid.models import HyperliquidCredentials, AgentWallet

MASTER = "0x000000000000000000000000000000000000aaaa"
AGENT_ADDR = "0x000000000000000000000000000000000000bbbb"
AGENT_KEY = "0x" + "1" * 64  # valid 32-byte hex for eth_account


def _async(value):
    async def _coro(*a, **k):
        return value
    return _coro()


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


def _creds_with_agent():
    return HyperliquidCredentials(
        user_id="u1",
        wallet_address=MASTER,
        private_key=AGENT_KEY,
        agent_wallet=AgentWallet(address=AGENT_ADDR, private_key=AGENT_KEY),
        demo_mode=False,
        testnet=True,
    )


@pytest.mark.asyncio
async def test_account_state_read_queries_master_not_agent(monkeypatch):
    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "rate_limit", lambda *a, **k: _async(None))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(_creds_with_agent()))

    captured = {}

    async def _post(url, json=None, **kw):
        captured["user"] = json.get("user")
        return _FakeResp({"marginSummary": {}, "assetPositions": []})

    tool._http_client = types.SimpleNamespace(post=_post)

    await tool.get_account_state(types.SimpleNamespace())
    assert captured["user"] == MASTER  # never the agent address


@pytest.mark.asyncio
async def test_exchange_db_delegated_signer_uses_master_account_address(monkeypatch):
    # Force the DB-creds branch (no factory agent wallet) with a delegated agent_wallet.
    monkeypatch.setattr("core.wallet.factory.get_agent_wallet", lambda: None)

    captured = {}

    class FakeExchange:
        def __init__(self, wallet, base_url=None, meta=None, vault_address=None,
                     account_address=None, *a, **k):
            captured["account_address"] = account_address

    monkeypatch.setattr("tools.hyperliquid.service.Exchange", FakeExchange, raising=False)
    monkeypatch.setattr("tools.hyperliquid.service.HAS_SDK", True, raising=False)

    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    monkeypatch.setattr(tool, "_get_info_client", lambda: _async(object()))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(_creds_with_agent()))

    ex, err = await tool._get_exchange_client()
    assert err is None
    # The agent key signs, but the account being traded is the MASTER.
    assert captured["account_address"] == MASTER
