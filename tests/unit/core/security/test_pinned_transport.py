"""067 P0.10: the pinned/bounded httpx transports live in core.

The A2A push webhook needs the SSRF pin and has nothing to do with payment, so
it imports the transport from ``core.security.pinned_transport``.
``tools/x402/net_guard.py`` keeps its names (bound to the x402 limits).
"""
import asyncio

import httpx
import pytest


def test_core_transport_pins_host_and_sni():
    from core.security.pinned_transport import PinnedAsyncTransport

    sent = {}

    class _Inner(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            sent["url"] = str(request.url)
            sent["host"] = request.headers.get("Host")
            sent["sni"] = request.extensions.get("sni_hostname")
            return httpx.Response(200, content=b"ok")

    t = PinnedAsyncTransport("hook.example.com", "93.184.216.34", inner=_Inner())
    resp = asyncio.run(t.handle_async_request(
        httpx.Request("POST", "https://hook.example.com/push")))
    assert resp.status_code == 200
    assert sent == {"url": "https://93.184.216.34/push",
                    "host": "hook.example.com", "sni": "hook.example.com"}
    with pytest.raises(httpx.ConnectError):
        asyncio.run(t.handle_async_request(
            httpx.Request("POST", "https://other.example.com/push")))


def test_core_transport_bounds_the_body():
    from core.security.pinned_transport import BoundedAsyncTransport

    class _Small(BoundedAsyncTransport):
        max_body_bytes = 4

    class _Inner(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, content=b"123456789")

    resp = asyncio.run(_Small(_Inner()).handle_async_request(
        httpx.Request("GET", "https://example.com/")))
    assert resp.content == b"1234"
    assert resp.extensions["polyrob_body_truncated"]


def test_net_guard_keeps_its_names_at_the_x402_bounds():
    from core.security import pinned_transport as core_t
    from tools.x402 import net_guard

    assert issubclass(net_guard.BoundedAsyncTransport, core_t.BoundedAsyncTransport)
    assert issubclass(net_guard.PinnedAsyncTransport, core_t.PinnedAsyncTransport)
    t = net_guard.BoundedAsyncTransport(inner=object())
    assert t.max_body_bytes == net_guard.MAX_X402_BODY_BYTES == core_t.MAX_BODY_BYTES
    assert t.timeout_sec == net_guard.X402_HTTP_TIMEOUT_SEC == core_t.HTTP_TIMEOUT_SEC


@pytest.mark.asyncio
async def test_a2a_push_transport_is_the_core_one(monkeypatch):
    from api.a2a import task_handler
    from core.security import url_policy
    from core.security.pinned_transport import PinnedAsyncTransport

    class _Validator:
        def __init__(self, **kw):
            pass

        def validate_and_resolve(self, url):
            return True, None, "93.184.216.34"

    monkeypatch.setattr(url_policy, "MCPURLValidator", _Validator)
    t = await task_handler._pinned_push_transport("https://hook.example.com/push")
    assert type(t) is PinnedAsyncTransport
    assert t._pinned_ip == "93.184.216.34"
    await t.aclose()


def test_the_a2a_push_path_does_not_import_the_x402_pack():
    from pathlib import Path

    from api.a2a import task_handler
    assert "tools.x402" not in Path(task_handler.__file__).read_text()
