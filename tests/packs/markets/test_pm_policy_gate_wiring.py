"""F6 (N2): Polymarket trades must route through PolicyGate."""
import pytest

pytest.importorskip("polyrob_markets")

import types
import pytest

from core.wallet.policy import PolicyGate
from polyrob_markets.polymarket.service import PolymarketTool, PlaceLimitOrderParams


def _async(value):
    async def _coro(*a, **k):
        return value
    return _coro()


class _FakeClient:
    def __init__(self):
        self.calls = []

    def create_order(self, order_args):
        return order_args

    def post_order(self, signed):
        return self._post("/order", signed)

    def _post(self, url, order_args):
        self.calls.append(order_args)
        return {"success": True, "orderID": "o1", "status": "live"}


def _tool(monkeypatch, gate):
    # The trade path legitimately requires the CLOB client; declare it available so
    # these tests exercise the PolicyGate logic regardless of which client package is
    # installed in the env (post py-clob-client-v2 migration). Both symbols live on
    # the (lazily-loading) adapter now; service.py imports them at use time.
    monkeypatch.setattr("polyrob_markets.polymarket.clob_adapter.CLOB_AVAILABLE", True)
    monkeypatch.setattr(
        "polyrob_markets.polymarket.clob_adapter.OrderArgs",
        lambda **kw: types.SimpleNamespace(**kw),
    )
    tool = PolymarketTool(config=types.SimpleNamespace(), container=None)
    tool._user_id = "u1"
    tool.db = None
    creds = types.SimpleNamespace(
        demo_mode=False, enabled=True,
        trading_limits=types.SimpleNamespace(require_confirmation_above_usd=1_000_000.0),
    )
    from ._risk_fixtures import polymarket
    polymarket(tool, creds, monkeypatch)
    monkeypatch.setattr(tool, "ensure_initialized", lambda: _async(None))
    monkeypatch.setattr(tool, "rate_limit", lambda *a, **k: _async(None))
    monkeypatch.setattr(tool, "_get_user_credentials", lambda: _async(creds))
    monkeypatch.setattr(tool, "_check_trading_limits", lambda *a, **k: None)
    monkeypatch.setattr(tool, "get_current_price", lambda *a, **k: _async({"success": True, "price": 0.5}))
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
    # notional + the market fee; this fake tool cannot read the fee, so the default applies
    from polyrob_markets.polymarket.fees import DEFAULT_FEE_RATE
    assert audit[0]["amount_usd"] == pytest.approx(5.0 * (1 + DEFAULT_FEE_RATE))


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
    assert "autonomy pause" in res["error"].lower()
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


@pytest.mark.asyncio
async def test_lost_order_response_blocks_retry(monkeypatch):
    from core.wallet import submission_journal as journal
    gate = PolicyGate(max_per_tx_usd=100)
    tool, client = _tool(monkeypatch, gate)
    calls = []
    def lose_reply(url, args):
        assert journal.unresolved()[0]["chain"] == "polymarket"
        calls.append(args)
        raise TimeoutError("reply lost after venue acceptance")
    monkeypatch.setattr(client, "_post", lose_reply)
    params = PlaceLimitOrderParams(market_id="m1", token_id="t1", side="buy", price=0.5, size_usd=5)
    assert not (await tool.place_limit_order(params))["success"]
    assert journal.unresolved()
    assert not (await tool.place_limit_order(params))["success"]
    assert len(calls) == 1
