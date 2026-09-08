"""M10 (2026-08-22): cancel_order / cancel_all_orders must route through the T11 live
kill-switch (crypto_trade_gate.evaluate_live_mutation), not just reach the venue
whenever `_get_authenticated_client()` succeeds. Proves against the REAL verbs with the
CLOB client mocked (never the gate):

  (a) CRYPTO_TRADE_LIVE_ENABLED off  -> both verbs are blocked before the venue.
  (b) switches on + autonomy HALTED, OWNER acting directly (execution_context is None,
      the default when a verb is called without one) -> both still reach the venue (a
      cancel is a position-management action the owner may perform by hand while
      halted).
  (c) switches on + autonomy HALTED, a genuine non-forged AGENT-LOOP turn
      (execution_context present, role="orchestrator") -> both are REFUSED too (R16,
      2026-08-22): the M10 carve-out is OWNER-only, not "risk-reducing actions are
      exempt from the kill-switch."
"""
import types
import pytest

from tools.polymarket.service import PolymarketTool, CancelOrderParams, CancelAllOrdersParams
from tools.controller.execution_context import ActionExecutionContext


def _async(value):
    async def _coro(*a, **k):
        return value
    return _coro()


class _FakeClient:
    def __init__(self):
        self.cancel_calls = []
        self.cancel_all_calls = []
        self.cancel_market_calls = []

    def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        return {"status": "ok"}

    def cancel_all(self):
        self.cancel_all_calls.append(True)
        return {"status": "ok"}

    def cancel_market_orders(self, market):
        self.cancel_market_calls.append(market)
        return {"status": "ok"}


def _make_tool(monkeypatch, client=None):
    tool = PolymarketTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    tool.db = None
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "rate_limit", lambda *a, **k: _async(None))
    c = client or _FakeClient()
    monkeypatch.setattr(tool, "_get_authenticated_client", lambda: _async((c, None)))
    return tool, c


def _clear_trading_env(monkeypatch):
    for k in ("CRYPTO_TRADE_LIVE_ENABLED", "POLYMARKET_TRADING_ENABLED",
              "POLYMARKET_TRADE_MAX_USD", "AUTONOMY_HALT"):
        monkeypatch.delenv(k, raising=False)


def _halt(monkeypatch):
    monkeypatch.setattr(
        "core.config_policy.AutonomyConfig.autonomy_halted", staticmethod(lambda: True))


# ---- (a) master switch off -> both verbs blocked before the venue -----------------

@pytest.mark.asyncio
async def test_cancel_order_blocked_when_live_trading_off(monkeypatch):
    _clear_trading_env(monkeypatch)
    tool, client = _make_tool(monkeypatch)
    res = await tool.cancel_order(CancelOrderParams(order_id="o1"))
    assert res["success"] is False
    assert client.cancel_calls == []


@pytest.mark.asyncio
async def test_cancel_all_orders_blocked_when_live_trading_off(monkeypatch):
    _clear_trading_env(monkeypatch)
    tool, client = _make_tool(monkeypatch)
    res = await tool.cancel_all_orders(CancelAllOrdersParams())
    assert res["success"] is False
    assert client.cancel_all_calls == []


@pytest.mark.asyncio
async def test_cancel_order_blocked_when_venue_flag_off(monkeypatch):
    _clear_trading_env(monkeypatch)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    tool, client = _make_tool(monkeypatch)
    res = await tool.cancel_order(CancelOrderParams(order_id="o1"))
    assert res["success"] is False
    assert client.cancel_calls == []


# ---- (b) switches on + autonomy HALTED, OWNER acting directly --------------------
# (execution_context is None -> the default when a verb is called without one, i.e.
# a direct/programmatic/CLI call, NOT an agent-loop turn.)

@pytest.mark.asyncio
async def test_cancel_order_succeeds_while_halted(monkeypatch):
    """A halted OWNER with an open order must still be able to cancel it by hand."""
    _clear_trading_env(monkeypatch)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADING_ENABLED", "true")
    _halt(monkeypatch)
    tool, client = _make_tool(monkeypatch)
    res = await tool.cancel_order(CancelOrderParams(order_id="o1"))
    assert res["success"] is True
    assert client.cancel_calls == ["o1"]


@pytest.mark.asyncio
async def test_cancel_all_orders_succeeds_while_halted(monkeypatch):
    _clear_trading_env(monkeypatch)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADING_ENABLED", "true")
    _halt(monkeypatch)
    tool, client = _make_tool(monkeypatch)
    res = await tool.cancel_all_orders(CancelAllOrdersParams())
    assert res["success"] is True
    assert client.cancel_all_calls == [True]


# ---- (c) R16: switches on + HALTED + a genuine AGENT-LOOP turn -------------------

@pytest.mark.asyncio
async def test_cancel_order_refused_while_halted_for_a_genuine_agent_turn(monkeypatch):
    """R16: a genuine, non-forged main-agent turn (role='orchestrator') gets ZERO
    relaxation from the kill-switch — only a literal owner-direct call (no
    execution_context) does."""
    _clear_trading_env(monkeypatch)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADING_ENABLED", "true")
    _halt(monkeypatch)
    tool, client = _make_tool(monkeypatch)
    ctx = ActionExecutionContext(role="orchestrator")
    res = await tool.cancel_order(CancelOrderParams(order_id="o1"), execution_context=ctx)
    assert res["success"] is False
    assert client.cancel_calls == []
    assert "halt" in res["error"].lower()


@pytest.mark.asyncio
async def test_cancel_all_orders_refused_while_halted_for_a_genuine_agent_turn(monkeypatch):
    """R16: same as above for cancel_all_orders — this is the exact verb the review
    proved the regression against (success=True, exchange reached, while halted, for
    a plain role="orchestrator" context)."""
    _clear_trading_env(monkeypatch)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADING_ENABLED", "true")
    _halt(monkeypatch)
    tool, client = _make_tool(monkeypatch)
    ctx = ActionExecutionContext(role="orchestrator")
    res = await tool.cancel_all_orders(CancelAllOrdersParams(), execution_context=ctx)
    assert res["success"] is False
    assert client.cancel_all_calls == []
    assert "halt" in res["error"].lower()


@pytest.mark.asyncio
async def test_cancel_order_succeeds_when_switches_on_and_not_halted(monkeypatch):
    # Sanity/positive-control: the ordinary (not-halted) live path still reaches the
    # venue. Does NOT discriminate pre- vs post-fix code by itself (both would pass);
    # the negative cases above are what discriminate the fix.
    _clear_trading_env(monkeypatch)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADING_ENABLED", "true")
    tool, client = _make_tool(monkeypatch)
    res = await tool.cancel_order(CancelOrderParams(order_id="o2"))
    assert res["success"] is True
    assert client.cancel_calls == ["o2"]
