"""Hermetic transport tests: no real DNS, HTTP, or model calls."""
import asyncio
import socket
from types import SimpleNamespace

import pytest

from core.security.http_probe import http_status


@pytest.fixture
def wire(monkeypatch):
    import aiohttp
    calls, pins, responses = [], [], []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])

    class Response:
        def __init__(self, status, location=None):
            self.status = status
            self.headers = {"Location": location} if location else {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            self.connector = kwargs["connector"]

        async def __aenter__(self):
            resolver = self.connector._resolver
            pins.extend(await resolver.resolve(resolver.hostname, 443))
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def get(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            calls.append(url)
            return Response(*responses.pop(0))

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    return SimpleNamespace(calls=calls, pins=pins, responses=responses)


def test_transport_pins_validated_address(wire):
    wire.responses.append((200,))
    assert asyncio.run(http_status("https://example.com/check", 1)) == 200
    assert wire.pins[0]["host"] == "93.184.216.34"


@pytest.mark.parametrize("url", ["http://localhost/x", "http://169.254.169.254/x", "http://10.0.0.1", "https://user:password@example.com"])
def test_internal_or_credentialed_endpoints_refused_before_transport(wire, url, monkeypatch):
    # Numeric addresses must be resolved as themselves, not the public fixture.
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 80))])
    with pytest.raises(ValueError):
        asyncio.run(http_status(url, 1))
    assert not wire.calls


def test_redirect_to_internal_host_refused(wire):
    wire.responses.append((302, "http://localhost/secrets"))
    with pytest.raises(ValueError):
        asyncio.run(http_status("https://example.com", 1))
    assert wire.calls == ["https://example.com"]


def test_mixed_public_private_dns_is_refused(wire, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
        for ip in ["93.184.216.34", "10.0.0.1"]])
    with pytest.raises(ValueError):
        asyncio.run(http_status("https://example.com", 1))
    assert not wire.calls


def test_loopback_grant_is_exact_origin_not_a_redirect_bypass(wire):
    wire.responses.append((302, "http://127.0.0.1:9001/private"))
    with pytest.raises(ValueError):
        asyncio.run(http_status("http://127.0.0.1:9000", 1,
                               allowed_loopback_origins=["http://127.0.0.1:9000"]))
    assert wire.calls == ["http://127.0.0.1:9000"]
