"""H14 (2026-09-23): the pinned resolver must match the host aiohttp resolves.

aiohttp resolves the IDNA (punycode) form of the host; the old resolvers compared
it to the Unicode ``urlparse().hostname``, missed, and fell through to a FRESH DNS
lookup — a rebinding hole. The shared resolver compares wire forms and RAISES on
any other host. All tests are offline: the end-to-end one serves on 127.0.0.1
and the resolver is the only thing that knows where "bücher.test" lives.
"""
import socket

import pytest

from core.security.pinned_resolver import (
    PinnedResolver, host_key, is_ip_literal, url_host_key,
)


def test_host_key_is_the_wire_form():
    assert host_key("Bücher.DE.") == "xn--bcher-kva.de"
    assert url_host_key("https://bücher.de:8443/x") == "xn--bcher-kva.de"
    assert host_key("[::1]") == "::1"
    assert host_key("") == ""
    assert is_ip_literal("93.184.216.34") and is_ip_literal("[::1]")
    assert not is_ip_literal("example.com")


@pytest.mark.asyncio
async def test_unicode_pin_answers_the_punycode_lookup_with_the_pinned_ip():
    r = PinnedResolver.for_url("https://bücher.de/", "93.184.216.34")
    out = await r.resolve("xn--bcher-kva.de", 443)
    assert out[0]["host"] == "93.184.216.34"
    assert out[0]["flags"] == socket.AI_NUMERICHOST


@pytest.mark.asyncio
async def test_any_other_host_raises_and_never_looks_up(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("the pinned resolver must never do a real lookup")
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    r = PinnedResolver("bücher.de", "93.184.216.34")
    with pytest.raises(OSError):
        await r.resolve("evil.example", 443)
    with pytest.raises(OSError):
        await r.resolve("xn--bcher-kva.de.evil", 443)


def test_web_fetch_and_x402_and_mcp_use_the_shared_resolver():
    from tools.mcp.protocol import _PinnedResolver as mcp_r
    from tools.web_fetch.fetcher import _PinnedResolver as wf_r
    assert issubclass(mcp_r, PinnedResolver) and issubclass(wf_r, PinnedResolver)
    import inspect
    from tools.x402 import discovery
    src = inspect.getsource(discovery._httpx_fetch)
    assert "PinnedResolver" in src and "url_host_key" in src


@pytest.mark.asyncio
async def test_web_fetch_idn_url_connects_to_the_pinned_ip_end_to_end(monkeypatch):
    """A Unicode host whose validator answer is 127.0.0.1 (a stub standing in
    for "a public IP") must reach THAT address — proving the punycode lookup
    hit the pin rather than real DNS (which would fail: .test never resolves)."""
    from aiohttp import web

    from tools.web_fetch.fetcher import safe_fetch

    def _no_dns(*a, **k):
        raise AssertionError("real DNS lookup attempted")
    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)

    async def _ok(request):
        return web.Response(text="pinned", headers={"Content-Encoding": "identity"})

    app = web.Application()
    app.router.add_get("/", _ok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    seen = []

    class _Validator:
        def validate_and_resolve(self, url):
            seen.append(url)
            return True, None, "127.0.0.1"

    try:
        res = await safe_fetch(f"http://bücher.test:{port}/", validator=_Validator(),
                               timeout_sec=5)
    finally:
        await runner.cleanup()
    assert res.body == b"pinned"
    assert seen


@pytest.mark.asyncio
async def test_x402_probe_idn_url_uses_the_pin(monkeypatch):
    from aiohttp import web

    from tools.x402.discovery import _httpx_fetch

    def _no_dns(*a, **k):
        raise AssertionError("real DNS lookup attempted")
    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)

    async def _ok(request):
        return web.Response(status=402, text="{}")

    app = web.Application()
    app.router.add_get("/", _ok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        resp = await _httpx_fetch(f"http://bücher.test:{port}/", pinned_ip="127.0.0.1",
                                  timeout=5)
    finally:
        await runner.cleanup()
    assert resp.status_code == 402
