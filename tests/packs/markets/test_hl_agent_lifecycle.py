"""T10 — approve_agent/revoke_agent: delegate signing to a freshly generated agent
wallet, signed by the MASTER key. Persists the agent wallet (never leaks the key).
On-chain behavior is testnet-verified; here the SDK call is mocked.
"""
import pytest

pytest.importorskip("polyrob_markets")

import types
import pytest

import polyrob_markets.hyperliquid.service as svc
from polyrob_markets.hyperliquid.service import HyperliquidTool
from polyrob_markets.hyperliquid.models import HyperliquidCredentials

MASTER_KEY = "0x" + "1" * 64
AGENT_KEY = "0x" + "2" * 64
MASTER_ADDR = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"  # from MASTER_KEY


def _async(v):
    async def _c(*a, **k):
        return v
    return _c()


def _creds():
    return HyperliquidCredentials(
        user_id="u1", wallet_address=MASTER_ADDR, private_key=MASTER_KEY,
        testnet=True, demo_mode=False,
    )


def _tool(monkeypatch, saved):
    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "rate_limit", lambda *a, **k: _async(None))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(_creds()))
    monkeypatch.setattr(tool, "_get_info_client", lambda: _async(object()))
    monkeypatch.setattr(svc, "HAS_SDK", True, raising=False)
    monkeypatch.setattr(svc, "_SDK_LOADED", True)
    def no_network(*args, **kw):
        raise RuntimeError("offline test: no exchange configured")
    monkeypatch.setattr(svc, "Exchange", no_network)

    class _DB:
        async def update_agent_wallet(self, user_id, agent_wallet):
            saved.update(user_id=user_id, agent_wallet=agent_wallet)
        async def audit_log(self, *a, **k):
            return None
    tool.db = _DB()
    return tool


@pytest.mark.asyncio
async def test_approve_agent_signs_with_master_and_persists_agent(monkeypatch):
    saved = {}
    tool = _tool(monkeypatch, saved)

    captured = {}

    class FakeExchange:
        def __init__(self, wallet, base_url=None, account_address=None, *a, **k):
            captured["signer"] = wallet.address
            captured["account_address"] = account_address
        def approve_agent(self, name=None):
            captured["name"] = name
            return ({"status": "ok"}, AGENT_KEY)

    monkeypatch.setattr(svc, "Exchange", FakeExchange, raising=False)

    res = await tool.approve_agent(types.SimpleNamespace(name="bot"))
    assert res["success"] is True
    # Signed by the master account.
    assert captured["signer"] == MASTER_ADDR
    # A fresh agent wallet was persisted; its key is never returned.
    assert saved.get("agent_wallet") is not None
    assert AGENT_KEY not in str(res)
    assert res["agent_address"] == saved["agent_wallet"].address


@pytest.mark.asyncio
async def test_approve_agent_requires_master_key(monkeypatch):
    saved = {}
    tool = _tool(monkeypatch, saved)
    monkeypatch.setattr(tool, "_get_user_credentials",
                        lambda: _async(HyperliquidCredentials(
                            user_id="u1", wallet_address=MASTER_ADDR, private_key="")))
    res = await tool.approve_agent(types.SimpleNamespace(name=None))
    assert res["success"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{"status": "ok"}, {"status": "err"}, None])
async def test_revoke_replaces_the_old_approval_before_clearing_local_wallet(monkeypatch, result):
    from polyrob_markets.hyperliquid.models import AgentWallet
    saved, captured = {}, {}
    tool = _tool(monkeypatch, saved)
    creds = _creds()
    creds.agent_wallet = AgentWallet(address="0x"+'2'*40, private_key=AGENT_KEY, name="bot")
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(creds))
    class Exchange:
        def __init__(self, master, **kw):
            assert master.address == MASTER_ADDR
        def approve_agent(self, name):
            captured['name'] = name
            assert not saved
            if result is None:
                raise TimeoutError('lost response')
            return result, 'replacement-private-key'
    monkeypatch.setattr(svc, 'Exchange', Exchange)
    res = await tool.revoke_agent(types.SimpleNamespace())
    assert captured['name'] == 'bot'
    assert res['success'] is (result == {"status": "ok"})
    if res['success']:
        assert saved['agent_wallet'] is None
    else:
        assert not saved
    assert AGENT_KEY not in str(res) and 'replacement-private-key' not in str(res)


@pytest.mark.asyncio
async def test_lifecycle_actions_registered(monkeypatch):
    saved = {}
    tool = _tool(monkeypatch, saved)
    for name in ("approve_agent", "revoke_agent"):
        res = await tool.execute_action(name, {})
        assert "Unknown tool" not in str(getattr(res, "error", "") or res)
