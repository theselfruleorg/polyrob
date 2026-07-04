"""T2 — service.py sources the CLOB client from the adapter and fails LOUD.

Replaces the archived `py_clob_client` import + silent `CLOB_CLIENT_AVAILABLE`
degrade with the single `clob_adapter` seam and a typed client-missing error.
"""
import types
import pytest

import tools.polymarket.service as svc
import tools.polymarket.clob_adapter as ad
from tools.polymarket.service import PolymarketTool, PlaceLimitOrderParams


def _async(value):
    async def _coro(*a, **k):
        return value
    return _coro()


def test_service_sources_client_symbols_from_adapter():
    # The trade client symbols must be the adapter's (single seam), not a private
    # legacy import. Both are None when the package is absent — identity proves the seam.
    assert svc.ClobClient is ad.ClobClient
    assert svc.OrderArgs is ad.OrderArgs
    assert hasattr(svc, "CLOB_AVAILABLE")


def test_legacy_py_clob_client_not_imported():
    import inspect
    source = inspect.getsource(svc)
    assert "from py_clob_client." not in source  # archived/non-functional


@pytest.mark.asyncio
async def test_place_limit_order_loud_when_client_missing(monkeypatch):
    monkeypatch.setattr(svc, "CLOB_AVAILABLE", False)
    tool = PolymarketTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    tool.db = None
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "rate_limit", lambda *a, **k: _async(None))

    res = await tool.place_limit_order(PlaceLimitOrderParams(
        market_id="m1", token_id="t1", side="buy", price=0.5, size_usd=5.0,
    ))
    assert res["success"] is False
    assert res.get("error_code") == "POLYMARKET_CLIENT_MISSING"
    assert "py-clob-client-v2" in (res.get("error", "") + res.get("suggestion", ""))
