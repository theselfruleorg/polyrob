"""Item 7F — server-manager wiring for resource subscriptions.

subscribe_resource sends ``resources/subscribe`` to the client and installs the
client's notification handler; a simulated server-side
``notifications/resources/updated`` (routed through the installed handler) fires the
registered callback; unsubscribe sends ``resources/unsubscribe`` and halts dispatch.
Uses a fake client/connection (no real transport) per the MCP unit-test pattern.
"""
import pytest

from tools.mcp.server_manager import MCPServerManager, ServerConnection, ServerStatus
from tools.mcp.protocol import MCPClient, MCPNotification


class _FakeClient:
    """Records subscribe/unsubscribe calls; exposes the notification hook attr."""

    def __init__(self):
        self.subscribed = []
        self.unsubscribed = []
        self._resource_update_handler = None

    async def subscribe_resource(self, uri):
        self.subscribed.append(uri)
        return {"ok": True}

    async def unsubscribe_resource(self, uri):
        self.unsubscribed.append(uri)
        return {"ok": True}


def _mgr_with_fake():
    mgr = MCPServerManager()
    fake = _FakeClient()
    mgr.connections["srv"] = ServerConnection(
        name="srv", config=None, status=ServerStatus.CONNECTED, client=fake
    )
    return mgr, fake


@pytest.mark.asyncio
async def test_subscribe_sends_request_and_installs_handler():
    mgr, fake = _mgr_with_fake()
    fired = []
    res = await mgr.subscribe_resource("srv", "res://x", callback=lambda s, u: fired.append(u))
    assert res["success"] is True
    assert fake.subscribed == ["res://x"]
    assert callable(fake._resource_update_handler)  # routed back to the manager


@pytest.mark.asyncio
async def test_server_notification_fires_callback():
    mgr, fake = _mgr_with_fake()
    fired = []
    await mgr.subscribe_resource("srv", "res://x", callback=lambda s, u: fired.append(u))
    # Simulate the server pushing an update through the installed client handler.
    await fake._resource_update_handler("res://x")
    assert fired == ["res://x"]


@pytest.mark.asyncio
async def test_unsubscribe_sends_request_and_halts():
    mgr, fake = _mgr_with_fake()
    fired = []
    await mgr.subscribe_resource("srv", "res://x", callback=lambda s, u: fired.append(u))
    await mgr.unsubscribe_resource("srv", "res://x")
    assert fake.unsubscribed == ["res://x"]
    # After unsubscribe, an update dispatches to nobody.
    n = await mgr.handle_resource_updated("srv", "res://x")
    assert n == 0
    assert fired == []


@pytest.mark.asyncio
async def test_client_notification_loop_routes_updated():
    """MCPClient._handle_notification routes resources/updated to its handler."""
    client = MCPClient.__new__(MCPClient)
    import logging
    client.logger = logging.getLogger("mcp-client-test")
    seen = []

    async def handler(uri):
        seen.append(uri)

    client._resource_update_handler = handler
    note = MCPNotification(method="notifications/resources/updated", params={"uri": "res://y"})
    await client._handle_notification(note)
    assert seen == ["res://y"]
