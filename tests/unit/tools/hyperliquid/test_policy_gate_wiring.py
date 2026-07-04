"""F6 (N2): Hyperliquid trades must route through PolicyGate.

Previously policy.check/record were only called by the x402 tool, so real perp
trades had NO catastrophic ceiling / daily cap / audit. Wire the order paths.
"""
import types
import pytest

from core.wallet.policy import PolicyGate
from tools.hyperliquid.service import (
    HyperliquidTool, PlaceLimitOrderParams, PlaceMarketOrderParams,
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


@pytest.fixture(autouse=True)
def _enable_live_trading(monkeypatch):
    # These tests exercise the order-submission logic, so enable the T11 live
    # kill-switch with a huge cap. (Default posture is dry-run.)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADING_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADE_MAX_USD", "100000000")
    monkeypatch.setenv("HYPERLIQUID_TRADE_MAX_USD", "100000000")
