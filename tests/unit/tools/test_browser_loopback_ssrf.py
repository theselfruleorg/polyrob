"""WS-4: the browser SSRF guard permits the agent's own published sandbox ports.

`_check_url_ssrf` normally blocks all loopback/private/metadata hosts (default,
BROWSER_ALLOW_PRIVATE_URLS off). WS-4 adds a NARROW exception: a loopback URL on a
port the sandbox actually published is allowed, so the agent can HTTP-test its own
server. Everything else (other loopback ports, RFC1918, cloud metadata) stays blocked.
"""
import pytest

from tools.shell.loopback_allow import allow_loopback_ports, clear_loopback_ports


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("BROWSER_ALLOW_PRIVATE_URLS", raising=False)
    clear_loopback_ports()
    yield
    clear_loopback_ports()


def _check(url):
    from tools.browser.browser import _check_url_ssrf
    return _check_url_ssrf(url)


def test_published_loopback_port_is_allowed():
    allow_loopback_ports([49153])
    assert _check("http://127.0.0.1:49153/") is None  # None == allowed


def test_unpublished_loopback_port_still_blocked():
    allow_loopback_ports([49153])
    blocked = _check("http://127.0.0.1:9999/")
    assert blocked is not None  # a non-allowed loopback port is still SSRF-blocked


def test_cloud_metadata_still_blocked_even_with_allowlist():
    allow_loopback_ports([80, 49153])
    assert _check("http://169.254.169.254/latest/meta-data/") is not None


def test_public_url_unaffected():
    allow_loopback_ports([49153])
    # a normal public URL is allowed (guard only blocks private/metadata)
    assert _check("https://example.com/") is None
