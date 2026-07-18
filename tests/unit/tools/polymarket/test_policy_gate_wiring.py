"""F6 (N2): Polymarket trades must route through PolicyGate."""
import types
import pytest

from core.wallet.policy import PolicyGate
from tools.polymarket.service import PolymarketTool, PlaceLimitOrderParams


def _async(value):
    async def _coro(*a, **k):
        return value
    return _coro()


class _FakeClient:
    def __init__(self):
        self.calls = []

    def create_and_post_order(self, order_args):
        self.calls.append(order_args)
        return {"orderID": "o1", "status": "ok"}


def _tool(monkeypatch, gate):
    # The trade path legitimately requires the CLOB client; declare it available so
    # these tests exercise the PolicyGate logic regardless of which client package is
    # installed in the env (post py-clob-client-v2 migration).
    monkeypatch.setattr("tools.polymarket.service.CLOB_AVAILABLE", True)
    monkeypatch.setattr(
        "tools.polymarket.service.OrderArgs",
        lambda **kw: types.SimpleNamespace(**kw),
    )
    tool = PolymarketTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    tool.db = None
    creds = types.SimpleNamespace(
        demo_mode=False, enabled=True,
        trading_limits=types.SimpleNamespace(require_confirmation_above_usd=1_000_000.0),
    )
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "rate_limit", lambda *a, **k: _async(None))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(creds))
    monkeypatch.setattr(tool, "_check_trading_limits", lambda *a, **k: None)
    client = _FakeClient()
    monkeypatch.setattr(tool, "_get_authenticated_client", lambda: _async((client, None)))
    monkeypatch.setattr("core.wallet.factory.get_policy_gate", lambda: gate)
    return tool, client


@pytest.mark.asyncio
async def test_pm_order_denied_over_ceiling(monkeypatch):
    gate = PolicyGate(max_per_tx_usd=10.0)
    tool, client = _tool(monkeypatch, gate)
    res = await tool.place_limit_order(PlaceLimitOrderParams(
        market_id="m1", token_id="t1", side="buy", price=0.5, size_usd=100.0,
    ))
    assert res["success"] is False
    assert "policy" in res["error"].lower()
    assert client.calls == []


@pytest.mark.asyncio
async def test_pm_order_within_ceiling_records_audit(monkeypatch):
    gate = PolicyGate(max_per_tx_usd=10_000.0)
    tool, client = _tool(monkeypatch, gate)
    res = await tool.place_limit_order(PlaceLimitOrderParams(
        market_id="m1", token_id="t1", side="buy", price=0.5, size_usd=5.0,
    ))
    assert res["success"] is True
    assert len(client.calls) == 1
    audit = gate.audit_log
    assert len(audit) == 1
    assert audit[0]["venue"] == "polymarket"
    assert audit[0]["amount_usd"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_pm_order_refused_while_halted(monkeypatch):
    """H11: the owner kill-switch (autonomy_halted) refuses a polymarket order at the top
    of the verb — before the CLOB client is reached — even for a direct/CLI call."""
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    gate = PolicyGate(max_per_tx_usd=10_000.0)
    tool, client = _tool(monkeypatch, gate)
    res = await tool.place_limit_order(PlaceLimitOrderParams(
        market_id="m1", token_id="t1", side="buy", price=0.5, size_usd=5.0,
    ))
    assert res["success"] is False
    assert "halt" in res["error"].lower()
    assert client.calls == []
    assert gate.audit_log == []


@pytest.fixture(autouse=True)
def _enable_live_trading(monkeypatch):
    # These tests exercise the order-submission logic, so enable the T11 live
    # kill-switch with a huge cap. (Default posture is dry-run.)
    monkeypatch.setenv("CRYPTO_TRADE_LIVE_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADING_ENABLED", "true")
    monkeypatch.setenv("HYPERLIQUID_TRADING_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_TRADE_MAX_USD", "100000000")
    monkeypatch.setenv("HYPERLIQUID_TRADE_MAX_USD", "100000000")
