"""T10 (observability) — agent_status reports HL agent-wallet delegation state
without ever exposing a private key. (On-chain approve_agent/revoke_agent is a
separate change requiring testnet sign-off.)
"""
import types
import pytest

from tools.hyperliquid.service import HyperliquidTool
from tools.hyperliquid.models import HyperliquidCredentials, AgentWallet

MASTER = "0x000000000000000000000000000000000000aaaa"
AGENT_ADDR = "0x000000000000000000000000000000000000bbbb"
SECRET_KEY = "0x" + "1" * 64


def _async(value):
    async def _coro(*a, **k):
        return value
    return _coro()


def _tool(monkeypatch, creds):
    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "rate_limit", lambda *a, **k: _async(None))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(creds))
    return tool


@pytest.mark.asyncio
async def test_agent_status_reports_delegation_without_key(monkeypatch):
    creds = HyperliquidCredentials(
        user_id="u1", wallet_address=MASTER, private_key=SECRET_KEY,
        agent_wallet=AgentWallet(address=AGENT_ADDR, private_key=SECRET_KEY, name="bot"),
    )
    tool = _tool(monkeypatch, creds)
    res = await tool.agent_status(types.SimpleNamespace())
    assert res["success"] is True
    assert res["delegated"] is True
    assert res["master_address"] == MASTER
    assert res["agent_address"] == AGENT_ADDR
    assert res["signer"] == "agent"
    # No private key may ever appear anywhere in the payload.
    assert SECRET_KEY not in str(res)


@pytest.mark.asyncio
async def test_agent_status_master_signer_when_no_agent(monkeypatch):
    creds = HyperliquidCredentials(
        user_id="u1", wallet_address=MASTER, private_key=SECRET_KEY,
    )
    tool = _tool(monkeypatch, creds)
    res = await tool.agent_status(types.SimpleNamespace())
    assert res["delegated"] is False
    assert res["signer"] == "master"
    assert res["agent_address"] is None


@pytest.mark.asyncio
async def test_agent_status_is_routable(monkeypatch):
    creds = HyperliquidCredentials(
        user_id="u1", wallet_address=MASTER, private_key=SECRET_KEY,
    )
    tool = _tool(monkeypatch, creds)
    res = await tool.execute_action("agent_status", {})
    text = str(getattr(res, "error", "") or res)
    assert "Unknown tool" not in text
