import types
from unittest.mock import AsyncMock

import pytest

from polyrob_markets.order_safety import order_accepted, order_notional
from tools.credential_cache import UserCredentialCacheMixin


def test_low_sell_price_cannot_understate_notional():
    assert order_notional(100, 0.45, 0.5) == 50
    with pytest.raises(ValueError, match="price band"):
        order_notional(10000, 0.001, 0.5)


def test_market_buy_counts_worst_slippage():
    assert order_notional(10, 120, 100) == 1200


@pytest.mark.parametrize("reference", [None, 0, float("nan"), float("inf")])
def test_unreadable_reference_cannot_disable_caps(reference):
    with pytest.raises((TypeError, ValueError)):
        order_notional(1, 100, reference)


def test_rejected_or_malformed_orders_are_not_successful():
    for result in ({"status": "ok"}, {"status": "err"},
                   {"status": "ok", "response": {"data": {"statuses": [{"error": "bad price"}]}}}):
        assert not order_accepted("hyperliquid", result)
    assert not order_accepted("polymarket", {"success": False, "orderID": "x"})
    assert order_accepted("polymarket", {"success": True, "orderID": "x", "status": "live"})


@pytest.mark.asyncio
async def test_revoked_credentials_invalidate_existing_signing_clients():
    tool = UserCredentialCacheMixin()
    tool._user_id = "owner"
    old = types.SimpleNamespace(enabled=True)
    tool._credentials_cache = {"owner": old}
    tool._exchange_clients = {"owner:mainnet": object()}
    tool._clob_clients = {"owner": object()}
    tool.db = types.SimpleNamespace(get_credentials=AsyncMock(return_value=None))
    assert await tool._get_user_credentials() is None
    assert not tool._exchange_clients and not tool._clob_clients


@pytest.mark.asyncio
async def test_changed_limits_are_read_on_the_next_call():
    tool = UserCredentialCacheMixin()
    tool._user_id = "owner"
    tool._credentials_cache = {"owner": types.SimpleNamespace(limit=1000)}
    current = types.SimpleNamespace(limit=1)
    tool.db = types.SimpleNamespace(get_credentials=AsyncMock(return_value=current))
    assert (await tool._get_user_credentials()).limit == 1
