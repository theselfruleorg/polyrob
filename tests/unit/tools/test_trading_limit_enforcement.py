"""Regression: previously-unenforced trading safety caps now actually gate orders.

- Hyperliquid max_daily_loss_usd: reject new opening orders once the day's
  realized+unrealized loss reaches the cap (fail closed).
- Polymarket max_position_per_market_usd: reject a buy that would push cumulative
  per-market USD exposure over the cap (fail closed; sells exempt).
"""
import asyncio
import types

import pytest

from tools.hyperliquid.service import HyperliquidTool
from tools.polymarket.service import PolymarketTool


def _now_ms():
    import datetime as dt
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


def _hl(cap):
    svc = HyperliquidTool.__new__(HyperliquidTool)
    creds = types.SimpleNamespace(
        trading_limits=types.SimpleNamespace(max_daily_loss_usd=cap))
    return svc, creds


def _mk_async(value):
    async def _f(*a, **k):
        return value
    return _f


def test_hl_daily_loss_blocks_when_cap_reached():
    svc, creds = _hl(cap=100.0)
    svc.get_fills = _mk_async({"success": True, "fills": [
        {"time": _now_ms(), "closed_pnl": -120.0}]})
    svc.get_account_state = _mk_async({"success": True, "positions": [
        {"unrealized_pnl": -10.0}]})
    ok, msg = asyncio.run(svc._check_daily_loss(creds, reduce_only=False))
    assert ok is False and "Daily-loss cap" in msg


def test_hl_daily_loss_allows_when_under_cap():
    svc, creds = _hl(cap=1000.0)
    svc.get_fills = _mk_async({"success": True, "fills": [
        {"time": _now_ms(), "closed_pnl": -120.0}]})
    svc.get_account_state = _mk_async({"success": True, "positions": []})
    ok, _ = asyncio.run(svc._check_daily_loss(creds, reduce_only=False))
    assert ok is True


def test_hl_daily_loss_reduce_only_exempt():
    svc, creds = _hl(cap=1.0)  # tiny cap
    ok, _ = asyncio.run(svc._check_daily_loss(creds, reduce_only=True))
    assert ok is True


def test_hl_daily_loss_fails_closed_on_fetch_error():
    svc, creds = _hl(cap=100.0)
    svc.get_fills = _mk_async({"success": False, "error": "boom"})
    svc.get_account_state = _mk_async({"success": True, "positions": []})
    ok, msg = asyncio.run(svc._check_daily_loss(creds, reduce_only=False))
    assert ok is False and "refusing to open" in msg


def _pm(cap):
    svc = PolymarketTool.__new__(PolymarketTool)
    limits = types.SimpleNamespace(max_position_per_market_usd=cap)
    return svc, limits


def _order(market_id="mkt-1", size_usd=200.0, side="buy"):
    return types.SimpleNamespace(market_id=market_id, size_usd=size_usd, side=side)


def test_pm_position_cap_blocks_cumulative_buy():
    svc, limits = _pm(cap=500.0)
    svc.get_all_positions = _mk_async({"success": True, "positions": [
        {"market_id": "mkt-1", "value": 400.0}]})
    err = asyncio.run(svc._check_position_limit(limits, _order(size_usd=200.0)))
    assert err and "Per-market position cap" in err  # 400 + 200 > 500


def test_pm_position_cap_allows_under():
    svc, limits = _pm(cap=500.0)
    svc.get_all_positions = _mk_async({"success": True, "positions": [
        {"market_id": "mkt-1", "value": 100.0}]})
    err = asyncio.run(svc._check_position_limit(limits, _order(size_usd=200.0)))
    assert err is None  # 100 + 200 <= 500


def test_pm_position_cap_sell_exempt():
    svc, limits = _pm(cap=1.0)
    err = asyncio.run(svc._check_position_limit(limits, _order(size_usd=999.0, side="sell")))
    assert err is None


def test_pm_position_cap_fails_closed_on_fetch_error():
    svc, limits = _pm(cap=500.0)
    svc.get_all_positions = _mk_async({"success": False, "error": "boom"})
    err = asyncio.run(svc._check_position_limit(limits, _order()))
    assert err and "refusing to open" in err
