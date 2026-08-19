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


# --- NAT64 (RFC 6052) synthesized-address tests ---------------------------------
#
# On an IPv6-only/NAT64 network, getaddrinfo() for a plain IPv4 host/literal
# returns the well-known-prefix synthesized address 64:ff9b::<embedded-ipv4>
# instead of the IPv4 itself (e.g. 64:ff9b::5db8:d822 for 93.184.216.34). Before
# the fix, none of the IPv4 property checks (is_private etc.) apply to that IPv6
# address, so it fell through to "not obviously blocked" -> a public IPv4 got
# wrongly rejected as unrecognized, AND (the real risk direction) nothing
# decoded the embedded IPv4 to check it either. Mocking getaddrinfo keeps this
# hermetic — no real NAT64 resolver needed to exercise the code path.

def test_nat64_synthesized_public_ip_allowed():
    """A NAT64-synthesized address wrapping a PUBLIC IPv4 is allowed."""
    os.environ.pop("BROWSER_ALLOW_PRIVATE_URLS", None)
    # 64:ff9b::5db8:d822 decodes to 93.184.216.34 (example.com's IP range).
    fake = [(10, 1, 6, "", ("64:ff9b::5db8:d822", 0, 0, 0))]
    with patch("socket.getaddrinfo", return_value=fake):
        assert _check_url_ssrf("https://example.com") is None


def test_nat64_synthesized_private_ip_still_blocked():
    """A NAT64-synthesized address wrapping a PRIVATE IPv4 is still blocked —
    NAT64 wrapping must never bypass the SSRF guard."""
    os.environ.pop("BROWSER_ALLOW_PRIVATE_URLS", None)
    # 64:ff9b::a00:5 decodes to 10.0.0.5 (RFC1918).
    fake = [(10, 1, 6, "", ("64:ff9b::a00:5", 0, 0, 0))]
    with patch("socket.getaddrinfo", return_value=fake):
        err = _check_url_ssrf("https://internal.example")
    assert err is not None


def test_nat64_synthesized_metadata_ip_still_blocked():
    """A NAT64-synthesized address wrapping the cloud metadata IP is blocked."""
    os.environ.pop("BROWSER_ALLOW_PRIVATE_URLS", None)
    # 64:ff9b::a9fe:a9fe decodes to 169.254.169.254 (cloud metadata).
    fake = [(10, 1, 6, "", ("64:ff9b::a9fe:a9fe", 0, 0, 0))]
    with patch("socket.getaddrinfo", return_value=fake):
        err = _check_url_ssrf("https://metadata.example")
    assert err is not None


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
