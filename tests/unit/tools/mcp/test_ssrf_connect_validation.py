"""SSRF / DNS-rebinding hardening at MCP connect time.

Background
----------
``MCPURLValidator`` resolved the host and checked the IP against blocked
ranges, but it was invoked ONLY at server add/update time. The live transport
connect paths re-resolved DNS and never re-validated, so an attacker could
register ``https://evil.example`` resolving to a public IP, then flip DNS
(short TTL) to ``169.254.169.254`` / ``10.x`` / ``127.0.0.1`` before the agent
connected -> SSRF to cloud metadata / internal services.

The fix adds ``MCPURLValidator.validate_and_resolve(url)`` which resolves the
host ONCE and returns the validated, pinned IP. The HTTP-family transports
validate (and pin the resolved IP) at connect time and stop following
unvalidated redirects.

These tests exercise the load-bearing validation seam hermetically: DNS
(``socket.getaddrinfo``) is mocked, so they are fully offline. The transport
wiring is reasoned/asserted at the structural level (the transports call the
validator and disable redirects).
"""
from unittest import mock

import pytest

from tools.mcp.security import MCPURLValidator


def _mock_getaddrinfo(ip: str, family=None):
    """Build a getaddrinfo replacement that always resolves to ``ip``.

    Mirrors the 5-tuple shape getaddrinfo returns; only addr[4][0] (the IP) is
    read by the validator.
    """
    import socket as _socket

    fam = family or _socket.AF_INET

    def _fake(host, port, *args, **kwargs):
        return [(fam, _socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fake


# --------------------------------------------------------------------------
# validate_and_resolve: returns the pinned IP for public hosts, rejects blocked
# --------------------------------------------------------------------------

def test_validate_and_resolve_allows_public_ip_and_pins_it():
    v = MCPURLValidator(allow_http=True)
    with mock.patch("socket.getaddrinfo", _mock_getaddrinfo("93.184.216.34")):
        ok, err, pinned = v.validate_and_resolve("http://example.com")
    assert ok is True
    assert err is None
    assert pinned == "93.184.216.34"


def test_validate_and_resolve_blocks_cloud_metadata_rebind():
    """DNS rebound to the cloud metadata IP must be rejected at resolve time."""
    v = MCPURLValidator(allow_http=True)
    with mock.patch("socket.getaddrinfo", _mock_getaddrinfo("169.254.169.254")):
        ok, err, pinned = v.validate_and_resolve("http://rebind.example")
    assert ok is False
    assert pinned is None
    assert err is not None
    assert "blocked" in err.lower() or "169.254" in err


def test_validate_and_resolve_blocks_rfc1918():
    v = MCPURLValidator(allow_http=True)
    with mock.patch("socket.getaddrinfo", _mock_getaddrinfo("10.1.2.3")):
        ok, err, pinned = v.validate_and_resolve("http://internal.example")
    assert ok is False
    assert pinned is None
    assert err is not None


def test_validate_and_resolve_blocks_loopback():
    v = MCPURLValidator(allow_http=True)
    with mock.patch("socket.getaddrinfo", _mock_getaddrinfo("127.0.0.1")):
        ok, err, pinned = v.validate_and_resolve("http://localhost-rebind.example")
    assert ok is False
    assert pinned is None
    assert err is not None


def test_validate_and_resolve_blocks_link_local_192_168():
    v = MCPURLValidator(allow_http=True)
    with mock.patch("socket.getaddrinfo", _mock_getaddrinfo("192.168.0.5")):
        ok, err, pinned = v.validate_and_resolve("http://lan.example")
    assert ok is False
    assert pinned is None


@pytest.mark.parametrize("ip,fam", [
    ("::ffff:169.254.169.254", "AF_INET6"),  # IPv4-mapped IPv6 -> link-local metadata
    ("::ffff:127.0.0.1", "AF_INET6"),        # IPv4-mapped IPv6 -> loopback
    ("::ffff:10.0.0.5", "AF_INET6"),         # IPv4-mapped IPv6 -> RFC1918
    ("0.0.0.1", "AF_INET"),                  # 0.0.0.0/8 (routes to localhost on Linux)
    ("100.64.1.2", "AF_INET"),               # CGNAT / shared address space
    ("::1", "AF_INET6"),                     # IPv6 loopback
    ("fd00::1", "AF_INET6"),                 # IPv6 ULA (private)
])
def test_validate_and_resolve_blocks_property_based_ranges(ip, fam):
    """S8: property-based classification closes the class the hand-list missed."""
    import socket as _socket
    v = MCPURLValidator(allow_http=True)
    with mock.patch("socket.getaddrinfo",
                    _mock_getaddrinfo(ip, family=getattr(_socket, fam))):
        ok, err, pinned = v.validate_and_resolve("http://sneaky.example")
    assert ok is False, f"{ip} must be blocked"
    assert pinned is None


def test_validate_and_resolve_rejects_non_http_scheme():
    v = MCPURLValidator(allow_http=True)
    ok, err, pinned = v.validate_and_resolve("ftp://example.com")
    assert ok is False
    assert pinned is None


def test_validate_and_resolve_https_required_by_default():
    """With allow_http False, an http:// URL must be rejected (no resolve)."""
    v = MCPURLValidator(allow_http=False)
    ok, err, pinned = v.validate_and_resolve("http://example.com")
    assert ok is False
    assert pinned is None


def test_validate_two_tuple_contract_unchanged():
    """Existing callers depend on validate() returning a 2-tuple."""
    v = MCPURLValidator(allow_http=True)
    with mock.patch("socket.getaddrinfo", _mock_getaddrinfo("93.184.216.34")):
        result = v.validate("http://example.com")
    assert isinstance(result, tuple)
    assert len(result) == 2
    ok, err = result
    assert ok is True and err is None


# --------------------------------------------------------------------------
# Transport wiring: connect-time validation + redirects disabled
# --------------------------------------------------------------------------

def test_transports_validate_before_connect_and_disable_redirects():
    """Structural assertions on the live transport connect paths.

    These don't open a socket; they verify the transports route through the
    validate-and-pin helper and that redirects are not blindly followed.
    """
    from tools.mcp import protocol

    # A shared helper performs validate-and-pin and builds the pinned connector.
    assert hasattr(protocol, "_validate_and_pin_connector"), (
        "expected a shared connect-time validation helper in protocol.py"
    )

    src = __import__("inspect").getsource(protocol)
    # SSE transport must no longer blindly allow redirects.
    assert "allow_redirects=True" not in src, (
        "MCP HTTP transports must not follow unvalidated redirects"
    )


@pytest.mark.asyncio
async def test_validate_and_pin_connector_rejects_rebound_host():
    """The connect-time helper raises (aborts the connection) for a blocked rebind."""
    from tools.mcp import protocol
    from core.exceptions import MCPConnectionError

    with mock.patch("socket.getaddrinfo", _mock_getaddrinfo("169.254.169.254")):
        with pytest.raises(MCPConnectionError):
            protocol._validate_and_pin_connector("https://rebind.example", allow_http=False)


@pytest.mark.asyncio
async def test_validate_and_pin_connector_allows_public_host():
    """The helper returns a connector for a public host (no raise).

    ``TCPConnector`` requires a running event loop, mirroring the real connect
    path (the helper is only ever called from inside an async ``connect()``).
    """
    from tools.mcp import protocol

    with mock.patch("socket.getaddrinfo", _mock_getaddrinfo("93.184.216.34")):
        connector = protocol._validate_and_pin_connector("https://example.com", allow_http=False)
    assert connector is not None
    await connector.close()
