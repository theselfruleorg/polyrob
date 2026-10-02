"""CR-M14: Hyperliquid/Polymarket bind the tenant per task (never a mutable
singleton re-read after an await), and the HTTP /execute routes are owner-gated
for every non-read verb, with the caller bound as the ambient exec identity."""
import pytest

pytest.importorskip("polyrob_markets")

import asyncio

import pytest
from fastapi import HTTPException

from polyrob_markets.hyperliquid.service import HyperliquidTool
from polyrob_markets.polymarket.service import PolymarketTool


@pytest.mark.parametrize("cls", [HyperliquidTool, PolymarketTool])
def test_user_context_is_task_local_across_awaits(cls):
    import types
    tool = cls(config=types.SimpleNamespace(), container=None)
    creds = {"alice": "creds-alice", "mallory": "creds-mallory"}

    class _DB:
        async def get_credentials(self, uid):
            await asyncio.sleep(0.01)
            return creds[uid]

    tool.db = _DB()
    tool._credentials_cache = {}
    seen = {}

    async def turn(uid, delay):
        tool.set_user_context(uid)
        await asyncio.sleep(delay)          # a sibling sets its tenant here
        c = await tool._get_user_credentials()
        await asyncio.sleep(delay)
        seen[uid] = (tool._user_id, c)

    async def main():
        await asyncio.gather(turn("alice", 0.03), turn("mallory", 0.0))

    asyncio.run(main())
    assert seen["alice"] == ("alice", "creds-alice")
    assert seen["mallory"] == ("mallory", "creds-mallory")


class _Req:
    client = None
    headers = {}


class _HLTool:
    def __init__(self):
        self.calls = []

    def set_user_context(self, uid):
        pass

    async def execute_action(self, name, args):
        from core.exec_identity import current_exec_identity
        self.calls.append((name, current_exec_identity()[0]))

        class R:
            success, data, error, tool_name, execution_time_ms = True, {}, None, name, 0.0
        return R()


@pytest.fixture
def _owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")


def test_hyperliquid_execute_refuses_a_non_owner_trade(monkeypatch, _owner):
    import polyrob_markets.hyperliquid.routes as r
    from polyrob_markets.hyperliquid.api_models import ExecuteToolRequest
    tool = _HLTool()
    monkeypatch.setattr(r, "get_hyperliquid_tool", lambda request: tool)
    body = ExecuteToolRequest(tool_name="place_market_order",
                              arguments={"coin": "ETH", "is_buy": True, "size": 1})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(r.execute_tool(_Req(), body, user_id="u_stranger",
                                   client_ip="1.2.3.4"))
    assert exc.value.status_code == 403
    assert tool.calls == []
    # a read verb still works for the tenant, bound to the tenant
    asyncio.run(r.execute_tool(_Req(), ExecuteToolRequest(
        tool_name="get_all_mids", arguments={}), user_id="u_stranger",
        client_ip="1.2.3.4"))
    assert tool.calls == [("get_all_mids", "u_stranger")]
    # the owner may trade, and the verb sees the owner as the ambient identity
    asyncio.run(r.execute_tool(_Req(), body, user_id="owner-1", client_ip="x"))
    assert tool.calls[-1] == ("place_market_order", "owner-1")


def test_polymarket_execute_refuses_a_non_owner_trade(monkeypatch, _owner):
    import polyrob_markets.polymarket.routes as r
    from polyrob_markets.polymarket.api_models import ExecuteToolRequest

    tool = _HLTool()

    class _DB:
        async def audit_log(self, **kw):
            pass

    async def _tool():
        return tool

    async def _db():
        return _DB()

    monkeypatch.setattr(r, "get_polymarket_tool", _tool)
    monkeypatch.setattr(r, "get_polymarket_db", _db)
    monkeypatch.setattr(r, "get_client_ip", lambda request: "1.2.3.4")
    body = ExecuteToolRequest(tool_name="place_market_order", arguments={})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(r.execute_tool(_Req(), body, user_id="u_stranger"))
    assert exc.value.status_code == 403
    assert tool.calls == []


def test_trade_turn_refusal_without_context_checks_the_bound_http_caller(_owner):
    from core.exec_identity import reset_exec_identity, set_exec_identity
    from polyrob_markets.trade_gate import trade_turn_refusal
    token = set_exec_identity("u_stranger", "http:hyperliquid")
    try:
        assert trade_turn_refusal(None, None)
    finally:
        reset_exec_identity(token)
    token = set_exec_identity("owner-1", "http:hyperliquid")
    try:
        assert trade_turn_refusal(None, None) is None
    finally:
        reset_exec_identity(token)
