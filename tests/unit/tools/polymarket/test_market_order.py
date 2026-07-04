"""T6 — Polymarket place_market_order: a marketable-priced order that reuses every
safety gate by delegating to place_limit_order. Mirrors the Hyperliquid pattern.
"""
import types
import pytest

from tools.polymarket.service import PolymarketTool, PlaceMarketOrderParams


def _async(value):
    async def _coro(*a, **k):
        return value
    return _coro()


def _tool(monkeypatch, price_result, captured):
    tool = PolymarketTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "get_current_price", lambda *a, **k: _async(price_result))

    async def _fake_limit(params):
        captured["price"] = params.price
        captured["side"] = params.side
        captured["size_usd"] = params.size_usd
        return {"success": True, "order_id": "o1"}
    monkeypatch.setattr(tool, "place_limit_order", _fake_limit)
    return tool


@pytest.mark.asyncio
async def test_market_buy_prices_above_mid(monkeypatch):
    captured = {}
    tool = _tool(monkeypatch, {"success": True, "price": 0.50}, captured)
    res = await tool.place_market_order(PlaceMarketOrderParams(
        market_id="m1", token_id="t1", side="BUY", size_usd=10.0))
    assert res["success"] is True
    assert captured["price"] > 0.50  # marketable buy crosses the spread upward
    assert captured["price"] <= 0.99


@pytest.mark.asyncio
async def test_market_sell_prices_below_mid(monkeypatch):
    captured = {}
    tool = _tool(monkeypatch, {"success": True, "price": 0.50}, captured)
    await tool.place_market_order(PlaceMarketOrderParams(
        market_id="m1", token_id="t1", side="SELL", size_usd=10.0))
    assert captured["price"] < 0.50
    assert captured["price"] >= 0.01


@pytest.mark.asyncio
async def test_market_order_refuses_without_price(monkeypatch):
    captured = {}
    tool = _tool(monkeypatch, {"success": False, "price": None}, captured)
    res = await tool.place_market_order(PlaceMarketOrderParams(
        market_id="m1", token_id="t1", side="BUY", size_usd=10.0))
    assert res["success"] is False
    assert "price" in res["error"].lower()
    assert captured == {}  # never reached the order path


@pytest.mark.asyncio
async def test_market_order_registered(monkeypatch):
    tool = PolymarketTool(config=types.SimpleNamespace(), container=None)
    names = {t.get("name") for t in tool.get_available_tools()}
    assert "place_market_order" in names
    res = await tool.execute_action("place_market_order", {
        "market_id": "m1", "token_id": "t1", "side": "BUY", "size_usd": 10.0})
    assert "Unknown tool" not in str(getattr(res, "error", "") or res)
