"""A Hyperliquid cancel runs without an owner tap unless it would remove a
TRIGGER order (a stop-loss / take-profit). Natural flow: the owner's "cancel my
ETH limit order" runs at once. Attack path kept closed: a cancel that strips a
stop-loss (or one whose open orders cannot be read) still waits on the owner's
tap, and nothing is cancelled until it is granted."""
import types

import pytest

pytest.importorskip("polyrob_markets")

from polyrob_markets.hyperliquid.service import CancelAllOrdersParams, CancelOrderParams
from tests.packs.markets.test_hl_cancel_leverage_live_gate import (
    _async, _clear_trading_env, _make_tool,
)
from tools.controller.execution_context import ActionExecutionContext


def _live(monkeypatch):
    _clear_trading_env(monkeypatch)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    monkeypatch.setattr("polyrob_markets.trade_gate.trade_turn_refusal",
                        lambda *a, **k: None)


class _Approver:
    calls = []
    outcome = False

    def __init__(self, user_id=None):
        pass

    async def request(self, action, summary, ctx, hash_params=None):
        _Approver.calls.append((action, hash_params))
        return _Approver.outcome


@pytest.fixture
def approver(monkeypatch):
    _Approver.calls = []
    _Approver.outcome = False
    monkeypatch.setattr("tools.controller.approval_queue.OwnerQueueApprover", _Approver)
    return _Approver


def _orders(monkeypatch, tool, orders):
    monkeypatch.setattr(tool, "_trigger_orders_in_scope",
                        lambda coin, oid=None: _async(orders))


CTX = ActionExecutionContext(role="orchestrator")


@pytest.mark.asyncio
async def test_a_plain_limit_cancel_runs_without_a_tap(monkeypatch, approver):
    _live(monkeypatch)
    tool, ex = _make_tool(monkeypatch)
    _orders(monkeypatch, tool, [])
    res = await tool.cancel_order(CancelOrderParams(coin="eth", order_id=7), execution_context=CTX)
    assert res["success"] is True
    assert ex.cancel_calls == [("ETH", 7)]
    assert approver.calls == []


@pytest.mark.asyncio
async def test_a_stop_loss_cancel_waits_on_the_owner(monkeypatch, approver):
    _live(monkeypatch)
    tool, ex = _make_tool(monkeypatch)
    _orders(monkeypatch, tool, [{"oid": 7, "coin": "ETH", "isTrigger": True,
                                 "orderType": "Stop Market"}])
    res = await tool.cancel_order(CancelOrderParams(coin="ETH", order_id=7), execution_context=CTX)
    assert res["success"] is False and res["owner_approval_pending"]
    assert ex.cancel_calls == []
    assert approver.calls == [("hyperliquid_cancel_order", {"coin": "ETH", "order_id": 7})]
    approver.outcome = True
    res = await tool.cancel_order(CancelOrderParams(coin="ETH", order_id=7), execution_context=CTX)
    assert res["success"] is True and ex.cancel_calls == [("ETH", 7)]


@pytest.mark.asyncio
async def test_unreadable_orders_and_cancel_all_over_a_stop_still_ask(monkeypatch, approver):
    _live(monkeypatch)
    tool, ex = _make_tool(monkeypatch)
    _orders(monkeypatch, tool, None)
    res = await tool.cancel_order(CancelOrderParams(coin="ETH", order_id=7), execution_context=CTX)
    assert res["success"] is False and ex.cancel_calls == []
    _orders(monkeypatch, tool, [{"oid": 9, "coin": "BTC", "orderType": "Take Profit Limit"}])
    res = await tool.cancel_all_orders(CancelAllOrdersParams(), execution_context=CTX)
    assert res["success"] is False and ex.cancel_all_calls == []
    assert len(approver.calls) == 2


@pytest.mark.asyncio
async def test_the_trigger_reader_scopes_by_coin_and_order(monkeypatch):
    tool, _ = _make_tool(monkeypatch)
    rows = [{"oid": 1, "coin": "ETH", "orderType": "Limit", "isTrigger": False},
            {"oid": 2, "coin": "ETH", "orderType": "Stop Market", "isTrigger": True},
            {"oid": 3, "coin": "BTC", "orderType": "Limit", "isPositionTpsl": True}]

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return rows

    class _Http:
        async def post(self, url, json=None):
            assert json["type"] == "frontendOpenOrders"
            return _Resp()

    creds = types.SimpleNamespace(api_url="https://x", wallet_address="0xabc")
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(creds))
    tool._http_client = _Http()
    assert await tool._trigger_orders_in_scope("ETH", 1) == []
    assert [o["oid"] for o in await tool._trigger_orders_in_scope("ETH", 2)] == [2]
    assert [o["oid"] for o in await tool._trigger_orders_in_scope(None)] == [2, 3]
