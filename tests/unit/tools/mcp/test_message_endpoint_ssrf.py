"""M06 (2026-09-23): a tenant MCP ``message_endpoint`` is held to the server's
origin, validated on add AND update, re-checked at connect/send, and an error
body is never reflected to the caller. Offline: DNS is mocked and no socket
opens."""
import socket
from types import SimpleNamespace
from unittest import mock

import pytest

from core.exceptions import MCPConnectionError, MCPProtocolError
from tools.mcp.protocol import (
    MCPRequest, MCPSSETransport, message_endpoint_refusal, resolve_message_endpoint,
)
from tools.mcp.security import MCPURLValidator
from tools.mcp.user_mcp_service import UserMCPService


def _dns(ip):
    def _fake(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    return _fake


SERVER = "https://mcp.example.com/sse"


@pytest.mark.parametrize("endpoint", [None, "", "/message", "message",
                                      "https://mcp.example.com/message",
                                      "https://MCP.example.com:443/m"])
def test_same_origin_or_relative_endpoints_pass(endpoint):
    assert message_endpoint_refusal(SERVER, endpoint) is None


@pytest.mark.parametrize("endpoint", [
    "https://169.254.169.254/latest/meta-data",
    "http://mcp.example.com/message",          # scheme differs
    "https://mcp.example.com:8443/message",    # port differs
    "https://evil.example/message",
    "//10.0.0.1/message",                      # scheme-relative → other host
    "https://user:pw@mcp.example.com/m",
    "file:///etc/passwd",
])
def test_cross_origin_endpoints_are_refused(endpoint):
    assert message_endpoint_refusal(SERVER, endpoint)


def test_ip_literal_must_equal_the_pinned_ip():
    srv = "https://93.184.216.34/sse"
    assert message_endpoint_refusal(srv, "/m", pinned_ip="93.184.216.34") is None
    assert message_endpoint_refusal(srv, "/m", pinned_ip="93.184.216.35")


def test_relative_endpoint_resolves_against_the_server():
    assert resolve_message_endpoint(SERVER, "/message") == "https://mcp.example.com/message"


class _DB:
    def __init__(self, existing=None):
        self.existing = existing
        self.updated = None

    async def get_server(self, user_id, name):
        return self.existing

    async def update_server(self, user_id, name, **updates):
        self.updated = updates
        return True


def _svc(db):
    svc = UserMCPService.__new__(UserMCPService)
    svc.db = db
    svc.validator = MCPURLValidator(allow_http=False)
    svc._check_rate_limit = lambda uid: None
    return svc


@pytest.mark.asyncio
async def test_add_refuses_a_cross_origin_endpoint():
    svc = _svc(_DB())
    with mock.patch("socket.getaddrinfo", _dns("93.184.216.34")):
        res = await svc.add_server("u1", "srv", SERVER, "sse", auth_method="none",
                                   message_endpoint="https://169.254.169.254/x")
    assert res.success is False
    assert "message_endpoint" in res.error


@pytest.mark.asyncio
async def test_update_refuses_a_cross_origin_endpoint_and_a_server_move():
    db = _DB(existing=SimpleNamespace(server_url=SERVER,
                                      message_endpoint="https://mcp.example.com/m"))
    svc = _svc(db)
    with mock.patch("socket.getaddrinfo", _dns("93.184.216.34")):
        with pytest.raises(ValueError):
            await svc.update_server("u1", "srv", message_endpoint="https://evil.example/m")
        # Moving server_url must not leave the old endpoint on another origin.
        with pytest.raises(ValueError):
            await svc.update_server("u1", "srv", server_url="https://other.example.com/sse")
        assert db.updated is None
        assert await svc.update_server("u1", "srv", message_endpoint="/message2")
    assert db.updated == {"message_endpoint": "/message2"}


@pytest.mark.asyncio
async def test_send_refuses_an_endpoint_off_the_pinned_origin():
    t = MCPSSETransport(SERVER, message_endpoint="https://10.0.0.1/m", validate_ssrf=True)
    t.session = object()   # "connected"; the refusal fires before any request
    with pytest.raises(MCPProtocolError, match="SSRF"):
        await t.send_message(MCPRequest(id=1, method="ping"))


@pytest.mark.asyncio
async def test_connect_refuses_an_endpoint_off_the_pinned_origin():
    t = MCPSSETransport(SERVER, message_endpoint="https://10.0.0.1/m", validate_ssrf=True)
    with mock.patch("socket.getaddrinfo", _dns("93.184.216.34")):
        with pytest.raises(MCPConnectionError, match="same origin"):
            await t.connect()


@pytest.mark.asyncio
async def test_send_error_does_not_reflect_the_response_body():
    class _Resp:
        status = 500

        async def text(self):
            return "SECRET-INTERNAL-BODY"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def post(self, *a, **k):
            return _Resp()

    t = MCPSSETransport(SERVER, validate_ssrf=True)
    t.session = _Session()
    with pytest.raises(MCPProtocolError) as exc:
        await t.send_message(MCPRequest(id=1, method="ping"))
    assert "SECRET-INTERNAL-BODY" not in str(exc.value)
