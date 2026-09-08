"""M10 (2026-08-22): cancel_order / cancel_all_orders / update_leverage must route
through the T11 live kill-switch (crypto_trade_gate.evaluate_live_mutation), not just
`credentials.can_trade()`. Proves against the REAL verbs with the exchange client
mocked (never the gate):

  (a) CRYPTO_TRADE_LIVE_ENABLED off  -> all three verbs are blocked before the venue.
  (b) switches on + autonomy HALTED, OWNER acting directly (execution_context is None,
      the default when a verb is called without one)
                                      -> cancel_order / cancel_all_orders still reach
      the venue (a cancel is a position-management action the owner may perform by hand
      while halted); update_leverage does not (it is a risk-posture mutation, not a
      position-management one).
  (c) switches on + autonomy HALTED, a genuine non-forged AGENT-LOOP turn
      (execution_context present, role="orchestrator") -> cancel_order / cancel_all_orders
      are REFUSED too (R16, 2026-08-22): the M10 carve-out is OWNER-only. An agent turn
      gets ZERO relaxation from the kill-switch just because the action happens to be
      risk-reducing/position-management — only the owner acting directly does.
"""
import types
import pytest

from tools.hyperliquid.service import (
    HyperliquidTool, CancelOrderParams, CancelAllOrdersParams, UpdateLeverageParams,
)
from tools.controller.execution_context import ActionExecutionContext


def _async(value):
    async def _coro(*a, **k):
        return value
    return _coro()


class _FakeExchange:
    def __init__(self):
        self.cancel_calls = []
        self.cancel_all_calls = []
        self.leverage_calls = []

    def cancel(self, coin, order_id):
        self.cancel_calls.append((coin, order_id))
        return {"status": "ok"}

    def cancel_all_orders(self, coin=None):
        self.cancel_all_calls.append(coin)
        return {"status": "ok"}

    def update_leverage(self, leverage, coin, is_cross):
        self.leverage_calls.append((leverage, coin, is_cross))
        return {"status": "ok"}


def _make_tool(monkeypatch, exchange=None):
    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    tool.db = None
    creds = types.SimpleNamespace(
        can_trade=lambda: True,
        trading_limits=types.SimpleNamespace(max_leverage=50),
    )
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "rate_limit", lambda *a, **k: _async(None))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(creds))
    ex = exchange or _FakeExchange()
    monkeypatch.setattr(tool, "_get_exchange_client", lambda: _async((ex, None)))
    return tool, ex


def _clear_trading_env(monkeypatch):
    for k in ("CRYPTO_TRADE_LIVE_ENABLED", "HYPERLIQUID_TRADING_ENABLED",
              "HYPERLIQUID_TRADE_MAX_USD", "AUTONOMY_HALT"):
        monkeypatch.delenv(k, raising=False)


def _halt(monkeypatch):
    monkeypatch.setattr(
        "core.config_policy.AutonomyConfig.autonomy_halted", staticmethod(lambda: True))


# ---- (a) master switch off -> all three verbs blocked before the venue -----------

@pytest.mark.asyncio
async def test_cancel_order_blocked_when_live_trading_off(monkeypatch):
    _clear_trading_env(monkeypatch)
    tool, ex = _make_tool(monkeypatch)
    res = await tool.cancel_order(CancelOrderParams(coin="ETH", order_id=1))
    assert res["success"] is False
    assert ex.cancel_calls == []


@pytest.mark.asyncio
async def test_cancel_all_orders_blocked_when_live_trading_off(monkeypatch):
    _clear_trading_env(monkeypatch)
    tool, ex = _make_tool(monkeypatch)
    res = await tool.cancel_all_orders(CancelAllOrdersParams())
    assert res["success"] is False
    assert ex.cancel_all_calls == []


@pytest.mark.asyncio
async def test_update_leverage_blocked_when_live_trading_off(monkeypatch):
    _clear_trading_env(monkeypatch)
    tool, ex = _make_tool(monkeypatch)
    res = await tool.update_leverage(UpdateLeverageParams(coin="ETH", leverage=5))
    assert res["success"] is False
    assert ex.leverage_calls == []


@pytest.mark.asyncio
async def test_cancel_order_blocked_when_venue_flag_off(monkeypatch):
    # Master on, venue-specific switch off -> still blocked (per-venue independence).
    _clear_trading_env(monkeypatch)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    tool, ex = _make_tool(monkeypatch)
    res = await tool.cancel_order(CancelOrderParams(coin="ETH", order_id=1))
    assert res["success"] is False
    assert ex.cancel_calls == []


# ---- (b) switches on + autonomy HALTED, OWNER acting directly --------------------
# (execution_context is None -> the default when a verb is called without one, i.e.
# a direct/programmatic/CLI call, NOT an agent-loop turn.)

@pytest.mark.asyncio
async def test_cancel_order_succeeds_while_halted(monkeypatch):
    """A halted OWNER with an open order must still be able to cancel it by hand."""
    _clear_trading_env(monkeypatch)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    _halt(monkeypatch)
    tool, ex = _make_tool(monkeypatch)
    res = await tool.cancel_order(CancelOrderParams(coin="ETH", order_id=1))
    assert res["success"] is True
    assert ex.cancel_calls == [("ETH", 1)]


@pytest.mark.asyncio
async def test_cancel_all_orders_succeeds_while_halted(monkeypatch):
    _clear_trading_env(monkeypatch)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    _halt(monkeypatch)
    tool, ex = _make_tool(monkeypatch)
    res = await tool.cancel_all_orders(CancelAllOrdersParams())
    assert res["success"] is True
    assert ex.cancel_all_calls == [None]


# ---- (c) R16: switches on + HALTED + a genuine AGENT-LOOP turn -------------------
# This is the regression R16 closes: the carve-out must be OWNER-only. A plain,
# non-forged execution_context (role="orchestrator", no forged turn_kind) is NOT the
# owner acting directly — it's an agent-loop turn — so it must be refused exactly like
# any other trading verb while halted.

@pytest.mark.asyncio
async def test_cancel_order_refused_while_halted_for_a_genuine_agent_turn(monkeypatch):
    """R16: a genuine, non-forged main-agent turn (role='orchestrator') gets ZERO
    relaxation from the kill-switch — only a literal owner-direct call (no
    execution_context) does. Without this, an LLM-driven agent turn could keep
    mutating live orders after the owner pulled the kill-switch."""
    _clear_trading_env(monkeypatch)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    _halt(monkeypatch)
    tool, ex = _make_tool(monkeypatch)
    ctx = ActionExecutionContext(role="orchestrator")
    res = await tool.cancel_order(
        CancelOrderParams(coin="ETH", order_id=1), execution_context=ctx)
    assert res["success"] is False
    assert ex.cancel_calls == []
    assert "halt" in res["error"].lower()


@pytest.mark.asyncio
async def test_cancel_all_orders_refused_while_halted_for_a_genuine_agent_turn(monkeypatch):
    """R16: same as above for cancel_all_orders — this is the exact verb the review
    proved the regression against."""
    _clear_trading_env(monkeypatch)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    _halt(monkeypatch)
    tool, ex = _make_tool(monkeypatch)
    ctx = ActionExecutionContext(role="orchestrator")
    res = await tool.cancel_all_orders(CancelAllOrdersParams(), execution_context=ctx)
    assert res["success"] is False
    assert ex.cancel_all_calls == []
    assert "halt" in res["error"].lower()


# ---- update_leverage: NOT a position-management action -> blocked while halted ----

@pytest.mark.asyncio
async def test_update_leverage_blocked_by_h11_kill_switch_while_halted(monkeypatch):
    """A leverage change is a risk-POSTURE mutation, not a position-management one —
    it is blocked by the pre-existing H11 trade_turn_refusal kill-switch bar exactly
    like an order, for owner and agent turns alike. NOTE (Minor 3): trade_turn_refusal
    (called with the default risk_reducing=False for this verb) always refuses first
    when halted, so evaluate_live_mutation's own halt branch is never reached from
    THIS call site — asserted here on H11's distinct wording ("the order was not
    submitted", never produced by evaluate_live_mutation) so this test cannot pass by
    accident against the wrong gate. evaluate_live_mutation's halt branch for
    risk_reducing=False is covered directly (isolated from H11) by
    test_update_leverage_m10_gate_blocks_when_isolated_from_h11 below, and by
    tests/unit/tools/test_crypto_trade_gate.py's pure-gate tests."""
    _clear_trading_env(monkeypatch)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    _halt(monkeypatch)
    tool, ex = _make_tool(monkeypatch)
    res = await tool.update_leverage(UpdateLeverageParams(coin="ETH", leverage=5))
    assert res["success"] is False
    assert ex.leverage_calls == []
    assert "the order was not submitted" in res["error"].lower()  # H11's exact wording


@pytest.mark.asyncio
async def test_update_leverage_m10_gate_blocks_when_isolated_from_h11(monkeypatch):
    """Minor 3: proves the M10 defence-in-depth branch in update_leverage (
    evaluate_live_mutation(risk_reducing=False)) genuinely blocks on its own, by
    bypassing the upstream H11 trade_turn_refusal check so the call actually reaches
    it. Without this, that branch has no verb-level coverage at all (it's provably
    unreachable via the ordinary call path — see the test above)."""
    _clear_trading_env(monkeypatch)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    _halt(monkeypatch)
    # update_leverage does `from tools.crypto_trade_gate import trade_turn_refusal`
    # fresh on every call, so patching the source module's attribute is what's needed
    # to intercept it (patching a name inside tools.hyperliquid.service would not —
    # there is no such module-level name there to patch).
    monkeypatch.setattr("tools.crypto_trade_gate.trade_turn_refusal", lambda *a, **k: None)
    tool, ex = _make_tool(monkeypatch)
    res = await tool.update_leverage(UpdateLeverageParams(coin="ETH", leverage=5))
    assert res["success"] is False
    assert ex.leverage_calls == []
    assert "hyperliquid" in res["error"].lower()  # evaluate_live_mutation's distinct wording


@pytest.mark.asyncio
async def test_cancel_order_succeeds_when_switches_on_and_not_halted(monkeypatch):
    # Sanity/positive-control: the ordinary (not-halted) live path still reaches the
    # venue. This does NOT discriminate pre- vs post-M10 code on its own (both would
    # pass) — it exists to prove the fix didn't regress the common case; the negative
    # cases above (blocked-when-off, refused-for-agent-turn-while-halted) are what
    # discriminate the fix.
    _clear_trading_env(monkeypatch)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    tool, ex = _make_tool(monkeypatch)
    res = await tool.cancel_order(CancelOrderParams(coin="ETH", order_id=7))
    assert res["success"] is True
    assert ex.cancel_calls == [("ETH", 7)]


@pytest.mark.asyncio
async def test_update_leverage_succeeds_when_switches_on_and_not_halted(monkeypatch):
    # Sanity/positive-control — see note above.
    _clear_trading_env(monkeypatch)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    tool, ex = _make_tool(monkeypatch)
    res = await tool.update_leverage(UpdateLeverageParams(coin="ETH", leverage=5))
    assert res["success"] is True
    assert ex.leverage_calls == [(5, "ETH", True)]
