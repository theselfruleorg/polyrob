"""S4 — per-hop SSRF guard on browser navigation (redirect-following).

go_to_url's _check_url_ssrf only validates the INITIAL url; a public URL that
302-redirects to a link-local / cloud-metadata address would otherwise be
followed by Playwright. BrowserContext._ssrf_route_guard runs per request (so it
re-fires on each redirect target when route interception is active) and aborts a
document navigation whose URL resolves to a blocked address.

These tests drive the guard directly with a fake Playwright route — no browser.
"""
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.browser.context import BrowserContext


def _guard():
    ctx = BrowserContext.__new__(BrowserContext)
    ctx.logger = logging.getLogger("test")
    ctx.session = None  # quiet __del__'s GC-time attribute access
    return ctx


def _route(url, resource_type="document"):
    route = MagicMock()
    route.request = MagicMock()
    route.request.url = url
    route.request.resource_type = resource_type
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()
    return route


@pytest.mark.asyncio
async def test_document_redirect_to_metadata_is_aborted():
    os.environ.pop("BROWSER_ALLOW_PRIVATE_URLS", None)
    route = _route("http://169.254.169.254/latest/meta-data/iam/security-credentials/")
    await _guard()._ssrf_route_guard(route)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_called()


@pytest.mark.asyncio
async def test_document_redirect_to_loopback_is_aborted():
    os.environ.pop("BROWSER_ALLOW_PRIVATE_URLS", None)
    route = _route("http://127.0.0.1:8080/admin")
    await _guard()._ssrf_route_guard(route)
    route.abort.assert_awaited_once()


@pytest.mark.asyncio
async def test_public_document_continues():
    os.environ.pop("BROWSER_ALLOW_PRIVATE_URLS", None)
    route = _route("https://example.com/")
    fake = [(2, 1, 6, "", ("93.184.216.34", 0))]
    with patch("socket.getaddrinfo", return_value=fake):
        await _guard()._ssrf_route_guard(route)
    route.continue_.assert_awaited_once()
    route.abort.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_type", ["image", "script", "fetch", "xhr", "stylesheet"])
async def test_private_subresource_is_blocked(resource_type):
    os.environ.pop("BROWSER_ALLOW_PRIVATE_URLS", None)
    route = _route("http://169.254.169.254/x.png", resource_type=resource_type)
    await _guard()._ssrf_route_guard(route)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_called()


@pytest.mark.asyncio
async def test_validator_failure_aborts_request():
    route = _route("https://example.com/", resource_type="fetch")
    with patch("tools.browser.browser._check_url_ssrf", side_effect=RuntimeError("broken validator")):
        await _guard()._ssrf_route_guard(route)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_called()
