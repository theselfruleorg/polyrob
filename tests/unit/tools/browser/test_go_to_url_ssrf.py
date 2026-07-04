"""
SSRF guard tests for browser navigation (go_to_url / open_tab).

These tests target the smallest pure seam — the `_check_url_ssrf` helper that
runs an agent-supplied URL through the existing MCPURLValidator before any
`page.goto()`. Literal-IP URLs are used wherever possible so the tests need no
real DNS; the one public-host case is made hermetic by mocking the resolver.
"""

import os
from unittest.mock import patch

import pytest

from tools.browser.browser import Browser, _check_url_ssrf


# --- Helper-level tests (pure, no browser needed) ------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://127.0.0.1:8080",                      # loopback
        "http://10.0.0.5",                            # RFC1918 class A
        "http://192.168.1.1",                         # RFC1918 class C
        "http://172.16.0.1",                          # RFC1918 class B
        "http://[::1]/",                              # IPv6 loopback
    ],
)
def test_blocked_private_and_metadata_hosts(url):
    """Private/loopback/link-local/metadata literal-IP URLs are blocked."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("BROWSER_ALLOW_PRIVATE_URLS", None)
        err = _check_url_ssrf(url)
    assert err is not None, f"expected {url!r} to be blocked"


def test_public_literal_ip_allowed():
    """A public literal IP (no DNS needed) is allowed."""
    os.environ.pop("BROWSER_ALLOW_PRIVATE_URLS", None)
    assert _check_url_ssrf("http://93.184.216.34/") is None  # example.com's IP range


def test_public_hostname_allowed_with_mocked_dns():
    """A public hostname is allowed; DNS resolution is mocked to stay hermetic."""
    os.environ.pop("BROWSER_ALLOW_PRIVATE_URLS", None)
    # getaddrinfo(host, None) -> list of (family, type, proto, canonname, sockaddr)
    fake = [(2, 1, 6, "", ("93.184.216.34", 0))]
    with patch("socket.getaddrinfo", return_value=fake):
        assert _check_url_ssrf("https://example.com") is None


def test_env_optout_allows_private():
    """With BROWSER_ALLOW_PRIVATE_URLS=true the guard is disabled."""
    with patch.dict(os.environ, {"BROWSER_ALLOW_PRIVATE_URLS": "true"}, clear=False):
        assert _check_url_ssrf("http://127.0.0.1:8080") is None
        assert _check_url_ssrf("http://169.254.169.254/latest/meta-data/") is None


def test_file_scheme_not_handled_by_ssrf_helper():
    """The SSRF helper is about IP ranges; file:// stays handled by go_to_url's own reject.

    file:// has no host -> validator rejects it too, which is fine (defense in depth),
    but we only assert the helper returns *something truthy* (blocked) rather than
    silently passing a file URL through.
    """
    os.environ.pop("BROWSER_ALLOW_PRIVATE_URLS", None)
    assert _check_url_ssrf("file:///etc/passwd") is not None


# --- Integration: go_to_url short-circuits before page.goto --------------------


@pytest.mark.asyncio
async def test_go_to_url_blocks_metadata_without_navigating():
    """go_to_url must return a blocked-URL error and never call page.goto."""
    from tools.browser.actions import GoToUrlAction
    from unittest.mock import AsyncMock, MagicMock

    os.environ.pop("BROWSER_ALLOW_PRIVATE_URLS", None)

    # Build a Browser instance without running __init__ (avoids Playwright setup).
    browser = Browser.__new__(Browser)
    import logging
    browser.logger = logging.getLogger("test")

    page = MagicMock()
    page.goto = AsyncMock()
    browser_context = MagicMock()
    browser_context.get_current_page = AsyncMock(return_value=page)

    exec_ctx = MagicMock()
    exec_ctx.browser_context = browser_context

    params = GoToUrlAction(url="http://169.254.169.254/latest/meta-data/")
    result = await browser.go_to_url(params, exec_ctx)

    assert result.error is not None
    page.goto.assert_not_called()


@pytest.mark.asyncio
async def test_go_to_url_allows_public_and_navigates():
    """A public host passes the guard and reaches page.goto."""
    from tools.browser.actions import GoToUrlAction
    from unittest.mock import AsyncMock, MagicMock

    os.environ.pop("BROWSER_ALLOW_PRIVATE_URLS", None)

    browser = Browser.__new__(Browser)
    import logging
    browser.logger = logging.getLogger("test")

    response = MagicMock()
    response.ok = True
    response.status = 200
    page = MagicMock()
    page.goto = AsyncMock(return_value=response)
    page.url = "https://example.com"
    page.title = AsyncMock(return_value="Example")

    browser_context = MagicMock()
    browser_context.get_current_page = AsyncMock(return_value=page)
    browser_context._update_state = AsyncMock()
    browser_context.get_session = AsyncMock(return_value=None)

    exec_ctx = MagicMock()
    exec_ctx.browser_context = browser_context

    # Avoid the real page-load wait helper.
    browser._wait_for_page_load = AsyncMock()

    fake = [(2, 1, 6, "", ("93.184.216.34", 0))]
    with patch("socket.getaddrinfo", return_value=fake):
        params = GoToUrlAction(url="https://example.com")
        result = await browser.go_to_url(params, exec_ctx)

    page.goto.assert_called_once()
    assert result.error is None
