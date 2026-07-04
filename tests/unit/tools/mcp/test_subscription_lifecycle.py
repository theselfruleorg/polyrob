"""H10 part 2: resource subscriptions leaked forever (clear() had no callers) and
silently stopped after any reconnect (a new MCPClient has no handler wired and the
server has no record of prior subscriptions). Subscriptions must survive a transient
disconnect and be re-established on reconnect, and be cleared on genuine removal.
"""
import asyncio
import logging
from unittest.mock import AsyncMock

from tools.mcp.subscriptions import ResourceSubscriptionRegistry
from tools.mcp.server_manager import MCPServerManager


def test_registry_uris_for_returns_subscribed_uris():
    reg = ResourceSubscriptionRegistry(logging.getLogger("t"))
    reg.subscribe("srv", "file:///a", lambda *a: None)
    reg.subscribe("srv", "file:///b", lambda *a: None)
    reg.subscribe("other", "file:///c", lambda *a: None)
    assert set(reg.uris_for("srv")) == {"file:///a", "file:///b"}
    assert reg.uris_for("missing") == []


def test_restore_subscriptions_rewires_handler_and_resends_subscribe():
    mgr = MCPServerManager.__new__(MCPServerManager)
    mgr.logger = logging.getLogger("t")
    mgr._subscriptions = ResourceSubscriptionRegistry(mgr.logger)
    mgr._subscriptions.subscribe("srv", "file:///a", lambda *a: None)

    # Fake connection whose client records what it was told to do on reconnect.
    client = AsyncMock()
    client._resource_update_handler = None
    connection = type("C", (), {})()
    connection.name = "srv"
    connection.client = client

    asyncio.run(mgr._restore_subscriptions(connection))

    # Handler re-wired and subscribe re-sent for the tracked uri.
    assert client._resource_update_handler is not None
    client.subscribe_resource.assert_awaited_once_with("file:///a")


def test_restore_is_noop_with_no_subscriptions():
    mgr = MCPServerManager.__new__(MCPServerManager)
    mgr.logger = logging.getLogger("t")
    mgr._subscriptions = ResourceSubscriptionRegistry(mgr.logger)
    client = AsyncMock()
    connection = type("C", (), {})()
    connection.name = "fresh"
    connection.client = client
    asyncio.run(mgr._restore_subscriptions(connection))
    client.subscribe_resource.assert_not_called()
