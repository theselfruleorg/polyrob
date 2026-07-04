"""Item 7F — MCP resource subscription callback registry.

Keyed by (server, uri): subscribe registers a callback; dispatch (driven by a
server-side ``notifications/resources/updated``) fires every callback for that key;
unsubscribe halts it. Callbacks may be sync or async; one raising callback never
breaks the others (fail-open).
"""
import pytest

from tools.mcp.subscriptions import ResourceSubscriptionRegistry


@pytest.mark.asyncio
async def test_subscribe_then_dispatch_fires():
    reg = ResourceSubscriptionRegistry()
    fired = []
    reg.subscribe("srv", "res://a", lambda server, uri: fired.append((server, uri)))
    n = await reg.dispatch("srv", "res://a")
    assert n == 1
    assert fired == [("srv", "res://a")]


@pytest.mark.asyncio
async def test_async_callback_awaited():
    reg = ResourceSubscriptionRegistry()
    fired = []

    async def cb(server, uri):
        fired.append(uri)

    reg.subscribe("srv", "res://a", cb)
    await reg.dispatch("srv", "res://a")
    assert fired == ["res://a"]


@pytest.mark.asyncio
async def test_unsubscribe_halts_dispatch():
    reg = ResourceSubscriptionRegistry()
    fired = []
    cb = lambda server, uri: fired.append(uri)
    reg.subscribe("srv", "res://a", cb)
    reg.unsubscribe("srv", "res://a")
    assert reg.is_subscribed("srv", "res://a") is False
    n = await reg.dispatch("srv", "res://a")
    assert n == 0
    assert fired == []


@pytest.mark.asyncio
async def test_dispatch_unknown_key_is_noop():
    reg = ResourceSubscriptionRegistry()
    assert await reg.dispatch("srv", "res://missing") == 0


@pytest.mark.asyncio
async def test_one_raising_callback_does_not_break_others():
    reg = ResourceSubscriptionRegistry()
    fired = []

    def boom(server, uri):
        raise RuntimeError("callback bug")

    reg.subscribe("srv", "res://a", boom)
    reg.subscribe("srv", "res://a", lambda server, uri: fired.append(uri))
    n = await reg.dispatch("srv", "res://a")
    assert fired == ["res://a"]  # second callback still ran
    assert n == 2  # both attempted


def test_clear_server_removes_all_keys():
    reg = ResourceSubscriptionRegistry()
    reg.subscribe("srv", "res://a", lambda s, u: None)
    reg.subscribe("srv", "res://b", lambda s, u: None)
    reg.subscribe("other", "res://c", lambda s, u: None)
    reg.clear("srv")
    assert reg.is_subscribed("srv", "res://a") is False
    assert reg.is_subscribed("srv", "res://b") is False
    assert reg.is_subscribed("other", "res://c") is True
