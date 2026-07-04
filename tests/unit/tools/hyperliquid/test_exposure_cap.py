"""F8 (P1-6): enforce max_total_exposure_usd on Hyperliquid opening orders.

The cap was defined on TradingLimits but never checked. Opening orders must
respect cumulative exposure; reduce-only (de-risking) orders are exempt; a
position-fetch failure fails CLOSED for opens (don't trade blind).
"""
import types
import pytest

from core.wallet.policy import PolicyGate
from tools.hyperliquid.service import HyperliquidTool, PlaceLimitOrderParams


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


def _tool(monkeypatch, *, exposure_cap, current_ntl=0.0, state_ok=True):
    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    tool.db = None
    creds = types.SimpleNamespace(
        trading_limits=types.SimpleNamespace(
            require_confirmation_above_usd=1_000_000.0,
            max_total_exposure_usd=exposure_cap,
        ),
    )
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "rate_limit", lambda *a, **k: _async(None))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(creds))
    monkeypatch.setattr(tool, "_check_trading_limits", lambda *a, **k: _async((True, "OK")))
    state = {"success": state_ok, "total_ntl_pos": current_ntl}
    monkeypatch.setattr(tool, "get_account_state", lambda *a, **k: _async(state))
    ex = _FakeExchange()
    monkeypatch.setattr(tool, "_get_exchange_client", lambda: _async((ex, None)))
    monkeypatch.setattr("core.wallet.factory.get_policy_gate",
                        lambda: PolicyGate(max_per_tx_usd=1_000_000.0))
    return tool, ex


@pytest.mark.asyncio
async def test_open_blocked_when_exposure_exceeds_cap(monkeypatch):
    tool, ex = _tool(monkeypatch, exposure_cap=1000.0, current_ntl=950.0)
    res = await tool.place_limit_order(
        PlaceLimitOrderParams(coin="ETH", is_buy=True, size=1.0, price=100.0)
    )  # +100 -> 1050 > 1000
    assert res["success"] is False
    assert "exposure" in res["error"].lower()
    assert ex.calls == []


@pytest.mark.asyncio
async def test_reduce_only_exempt_from_exposure_cap(monkeypatch):
    tool, ex = _tool(monkeypatch, exposure_cap=1000.0, current_ntl=5000.0)
    res = await tool.place_limit_order(
        PlaceLimitOrderParams(coin="ETH", is_buy=False, size=1.0, price=100.0, reduce_only=True)
    )
    assert res["success"] is True  # de-risking always allowed
    assert len(ex.calls) == 1


@pytest.mark.asyncio
async def test_open_fails_closed_when_state_unavailable(monkeypatch):
    tool, ex = _tool(monkeypatch, exposure_cap=1000.0, state_ok=False)
    res = await tool.place_limit_order(
        PlaceLimitOrderParams(coin="ETH", is_buy=True, size=0.01, price=100.0)
    )
    assert res["success"] is False
    assert ex.calls == []


@pytest.mark.asyncio
async def test_open_allowed_within_exposure_cap(monkeypatch):
    tool, ex = _tool(monkeypatch, exposure_cap=10_000.0, current_ntl=100.0)
    res = await tool.place_limit_order(
        PlaceLimitOrderParams(coin="ETH", is_buy=True, size=0.1, price=100.0)
    )
    assert res["success"] is True
    assert len(ex.calls) == 1


@pytest.fixture(autouse=True)
def _enable_live_trading(monkeypatch):
    # These tests exercise the order-submission logic, so enable the T11 live
    # kill-switch with a huge cap. (Default posture is dry-run.)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADING_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADE_MAX_USD", "100000000")
    monkeypatch.setenv("HYPERLIQUID_TRADE_MAX_USD", "100000000")
