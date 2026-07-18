"""F6 (N2): Hyperliquid trades must route through PolicyGate.

Previously policy.check/record were only called by the x402 tool, so real perp
trades had NO catastrophic ceiling / daily cap / audit. Wire the order paths.
"""
import types
import pytest

from core.wallet.policy import PolicyGate
from tools.hyperliquid.service import (
    HyperliquidTool, PlaceLimitOrderParams, PlaceMarketOrderParams, CancelOrderParams,
)


def _async(value):
    async def _coro(*a, **k):
        return value
    return _coro()


class _FakeExchange:
    def __init__(self):
        self.calls = []

    def order(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "ok"}


def _tool(monkeypatch, gate, mid=100.0):
    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    tool.db = None
    creds = types.SimpleNamespace(
        trading_limits=types.SimpleNamespace(require_confirmation_above_usd=1_000_000.0),
    )
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "rate_limit", lambda *a, **k: _async(None))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(creds))
    monkeypatch.setattr(tool, "_check_trading_limits", lambda *a, **k: _async((True, "OK")))
    monkeypatch.setattr(tool, "get_current_price", lambda *a, **k: _async({"success": True, "mid_price": mid}))
    ex = _FakeExchange()
    monkeypatch.setattr(tool, "_get_exchange_client", lambda: _async((ex, None)))
    monkeypatch.setattr("core.wallet.factory.get_policy_gate", lambda: gate)
    return tool, ex


@pytest.mark.asyncio
async def test_limit_order_denied_over_ceiling(monkeypatch):
    gate = PolicyGate(max_per_tx_usd=10.0)
    tool, ex = _tool(monkeypatch, gate)
    res = await tool.place_limit_order(
        PlaceLimitOrderParams(coin="ETH", is_buy=True, size=1.0, price=100.0)
    )
    assert res["success"] is False
    assert "policy" in res["error"].lower()
    assert ex.calls == []  # gate must block BEFORE submitting


@pytest.mark.asyncio
async def test_limit_order_refused_for_forged_turn(monkeypatch):
    """H11: a forged self-wake / delegation-result / leaf / autonomous turn must not
    place a live order — the trade methods must consult turn origin like x402 does."""
    from tools.controller.execution_context import ActionExecutionContext
    gate = PolicyGate(max_per_tx_usd=10_000.0)
    tool, ex = _tool(monkeypatch, gate)
    ctx = ActionExecutionContext()
    ctx.role = "leaf"  # a delegated/forged worker — must never move money
    res = await tool.place_limit_order(
        PlaceLimitOrderParams(coin="ETH", is_buy=True, size=0.01, price=100.0),
        execution_context=ctx,
    )
    assert res["success"] is False
    assert "forged" in res["error"].lower() or "owner must drive" in res["error"].lower()
    assert ex.calls == []  # refused BEFORE any submission


@pytest.mark.asyncio
async def test_limit_order_within_ceiling_records_audit(monkeypatch):
    gate = PolicyGate(max_per_tx_usd=10_000.0)
    tool, ex = _tool(monkeypatch, gate)
    res = await tool.place_limit_order(
        PlaceLimitOrderParams(coin="ETH", is_buy=True, size=0.01, price=100.0)
    )
    assert res["success"] is True
    assert len(ex.calls) == 1
    audit = gate.audit_log
    assert len(audit) == 1
    assert audit[0]["venue"] == "hyperliquid"
    assert audit[0]["amount_usd"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_market_order_denied_over_ceiling(monkeypatch):
    gate = PolicyGate(max_per_tx_usd=10.0)
    tool, ex = _tool(monkeypatch, gate, mid=100.0)
    res = await tool.place_market_order(
        PlaceMarketOrderParams(coin="ETH", is_buy=True, size=1.0)
    )
    assert res["success"] is False
    assert "policy" in res["error"].lower()
    assert ex.calls == []


@pytest.mark.asyncio
async def test_limit_order_refused_while_halted(monkeypatch):
    """H11: the owner kill-switch (autonomy_halted) refuses an order at the top of the
    verb — BEFORE any order value is computed or the venue is reached — even for a
    direct/CLI call (no execution_context). Parity with x402 spend."""
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    gate = PolicyGate(max_per_tx_usd=10_000.0)
    tool, ex = _tool(monkeypatch, gate)
    res = await tool.place_limit_order(
        PlaceLimitOrderParams(coin="ETH", is_buy=True, size=0.01, price=100.0)
    )
    assert res["success"] is False
    assert "halt" in res["error"].lower()
    assert ex.calls == []  # refused before any submission
    assert gate.audit_log == []  # nothing recorded


@pytest.mark.asyncio
async def test_cancel_order_refused_for_forged_turn(monkeypatch):
    """H11: mutating verbs beyond place_* (cancel/update_leverage/approve/revoke) are also
    gated — a forged/leaf turn cannot mutate the owner's live orders."""
    from tools.controller.execution_context import ActionExecutionContext
    gate = PolicyGate(max_per_tx_usd=10_000.0)
    tool, ex = _tool(monkeypatch, gate)
    ctx = ActionExecutionContext()
    ctx.role = "leaf"
    res = await tool.cancel_order(
        CancelOrderParams(coin="ETH", order_id=123), execution_context=ctx
    )
    assert res["success"] is False
    assert res.get("forged_turn_blocked") is True
    assert "forged" in res["error"].lower() or "owner must drive" in res["error"].lower()


@pytest.mark.asyncio
async def test_cancel_order_refused_while_halted(monkeypatch):
    """H11: the kill-switch also blocks a mutating verb (cancel_order)."""
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    gate = PolicyGate(max_per_tx_usd=10_000.0)
    tool, ex = _tool(monkeypatch, gate)
    res = await tool.cancel_order(CancelOrderParams(coin="ETH", order_id=123))
    assert res["success"] is False
    assert "halt" in res["error"].lower()


@pytest.mark.asyncio
async def test_concurrent_orders_cannot_both_pass_a_nearly_exhausted_cap(monkeypatch):
    """M4: check -> submit -> record is held under PolicyGate.reserve(), so two
    concurrent orders can't both read the same stale rolling-spend, both pass, and
    both record past the daily cap. With daily cap $10 and two concurrent $6 orders,
    exactly ONE may submit."""
    import asyncio
    gate = PolicyGate(max_per_tx_usd=10_000.0, daily_cap_usd=10.0)
    tool, ex = _tool(monkeypatch, gate)

    # Force a real await INSIDE the check->record span so, without the lock, both
    # coroutines would interleave and pass check before either records.
    real_get_client = tool._get_exchange_client

    async def _slow_client(*a, **k):
        await asyncio.sleep(0.01)
        return await real_get_client()

    # _tool() monkeypatched _get_exchange_client with a lambda returning a coroutine;
    # rebuild the slow variant around the same fake exchange.
    async def _slow(*a, **k):
        await asyncio.sleep(0.01)
        return (ex, None)
    monkeypatch.setattr(tool, "_get_exchange_client", _slow)

    p = PlaceLimitOrderParams(coin="ETH", is_buy=True, size=0.06, price=100.0)  # $6
    r1, r2 = await asyncio.gather(tool.place_limit_order(p), tool.place_limit_order(p))
    successes = [r for r in (r1, r2) if r.get("success")]
    denials = [r for r in (r1, r2) if not r.get("success")]
    assert len(successes) == 1, (r1, r2)
    assert len(denials) == 1 and "daily" in denials[0]["error"].lower()
    assert len(ex.calls) == 1  # only one order reached the venue
    assert len(gate.audit_log) == 1  # only one spend recorded


@pytest.fixture(autouse=True)
def _enable_live_trading(monkeypatch):
    # These tests exercise the order-submission logic, so enable the T11 live
    # kill-switch with a huge cap. (Default posture is dry-run.)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADING_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADE_MAX_USD", "100000000")
    monkeypatch.setenv("HYPERLIQUID_TRADE_MAX_USD", "100000000")
