"""F5 (P0-3): Hyperliquid place_market_order must exist + be registered.

The dead PlaceMarketOrderParams model existed with no action method. A market
order is implemented as a marketable IOC limit at mid +/- slippage.
"""
import pytest

pytest.importorskip("polyrob_markets")

import types
import pytest

from polyrob_markets.hyperliquid.service import HyperliquidTool, PlaceMarketOrderParams


def _async(value):
    async def _coro(*a, **k):
        return value
    return _coro()


class _FakeExchange:
    def __init__(self):
        self.calls = []

    def order(self, **kwargs):
        return self.post("/exchange", kwargs)

    def post(self, url, kwargs):
        self.calls.append(kwargs)
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}


def _make_tool(monkeypatch, mid=100.0, exchange=None, mid_success=True):
    tool = HyperliquidTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    tool.db = None
    creds = types.SimpleNamespace(
        trading_limits=types.SimpleNamespace(require_confirmation_above_usd=1_000_000.0),
    )
    from ._risk_fixtures import hyperliquid
    hyperliquid(tool, creds, monkeypatch)
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "rate_limit", lambda *a, **k: _async(None))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(creds))
    monkeypatch.setattr(tool, "_check_trading_limits", lambda *a, **k: _async((True, "OK")))
    price = {"success": mid_success, "mid_price": mid}
    if not mid_success:
        price = {"success": False, "error": "Coin 'ZZZ' not found"}
    monkeypatch.setattr(tool, "get_current_price", lambda *a, **k: _async(price))
    ex = exchange or _FakeExchange()
    monkeypatch.setattr(tool, "_get_exchange_client", lambda: _async((ex, None)))
    return tool, ex


@pytest.mark.asyncio
async def test_market_order_cannot_exceed_approved_usd_with_slippage(monkeypatch):
    tool, exchange = _make_tool(monkeypatch, mid=100)
    result = await tool.place_market_order(PlaceMarketOrderParams(
        coin="ETH", is_buy=True, size=1, slippage=.2, max_usd=100))
    assert not result["success"] and "approved max_usd" in result["error"]
    assert exchange.calls == []


@pytest.mark.asyncio
async def test_limit_sell_live_value_cannot_exceed_approved_usd(monkeypatch):
    from polyrob_markets.hyperliquid.service import PlaceLimitOrderParams
    tool, exchange = _make_tool(monkeypatch, mid=100)
    result = await tool.place_limit_order(PlaceLimitOrderParams(
        coin="ETH", is_buy=False, size=1, price=80, max_usd=80))
    assert not result["success"] and "approved max_usd" in result["error"]
    assert exchange.calls == []


def test_order_schema_requires_a_finite_explicit_usd_ceiling():
    from pydantic import ValidationError
    from polyrob_markets.hyperliquid.service import PlaceLimitOrderParams
    for cls, extra in ((PlaceMarketOrderParams, {}), (PlaceLimitOrderParams, {"price": 100})):
        with pytest.raises(ValidationError):
            cls(coin="ETH", is_buy=True, size=1, **extra)
        for bad in (0, -1, float("nan"), float("inf")):
            with pytest.raises(ValidationError):
                cls(coin="ETH", is_buy=True, size=1, max_usd=bad, **extra)


@pytest.mark.asyncio
async def test_place_market_order_is_registered(monkeypatch):
    tool, _ = _make_tool(monkeypatch)
    res = await tool.execute_action(
        "place_market_order", {"coin": "ETH", "is_buy": True, "size": 0.1, "max_usd": 1000}
    )
    assert res.error is None or "Unknown tool" not in (res.error or "")
    assert res.success is True


@pytest.mark.asyncio
async def test_market_buy_uses_ioc_limit_within_slippage(monkeypatch):
    ex = _FakeExchange()
    tool, _ = _make_tool(monkeypatch, mid=100.0, exchange=ex)
    res = await tool.place_market_order(
        PlaceMarketOrderParams(max_usd=1000, coin="eth", is_buy=True, size=0.1, slippage=0.05)
    )
    assert res["success"] is True
    assert len(ex.calls) == 1
    call = ex.calls[0]
    assert call["is_buy"] is True
    assert call["sz"] == 0.1
    assert call["order_type"] == {"limit": {"tif": "Ioc"}}
    # Buy marketable limit = mid * (1 + slippage)
    assert call["limit_px"] == pytest.approx(105.0)
    assert call["name"] == "ETH"


@pytest.mark.asyncio
async def test_market_sell_prices_below_mid(monkeypatch):
    ex = _FakeExchange()
    tool, _ = _make_tool(monkeypatch, mid=100.0, exchange=ex)
    await tool.place_market_order(
        PlaceMarketOrderParams(max_usd=1000, coin="ETH", is_buy=False, size=0.5, slippage=0.1)
    )
    assert ex.calls[0]["limit_px"] == pytest.approx(90.0)


@pytest.mark.asyncio
async def test_market_order_refuses_without_live_mid(monkeypatch):
    ex = _FakeExchange()
    tool, _ = _make_tool(monkeypatch, mid_success=False, exchange=ex)
    res = await tool.place_market_order(
        PlaceMarketOrderParams(max_usd=1000, coin="ZZZ", is_buy=True, size=1.0)
    )
    assert res["success"] is False
    assert "price" in res["error"].lower()
    assert ex.calls == []  # never submit a market order without a price


@pytest.fixture(autouse=True)
def _enable_live_trading(monkeypatch):
    # These tests exercise the order-submission logic, so enable the T11 live
    # kill-switch with a huge cap. (Default posture is dry-run.)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADING_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADE_MAX_USD", "100000000")
    monkeypatch.setenv("HYPERLIQUID_TRADE_MAX_USD", "100000000")
