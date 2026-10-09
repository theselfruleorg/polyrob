import ipaddress

import pytest

from tools.browser._flags import allow_private_urls
from tools.web_fetch.tool import _allow_private_urls
from core.security.url_policy import MCPURLValidator


@pytest.mark.parametrize("value", ["maybe", "tru", "enabled", "no", "0", "false", ""])
def test_ssrf_bypasses_require_explicit_opt_in(monkeypatch, value):
    monkeypatch.setenv("BROWSER_ALLOW_PRIVATE_URLS", value)
    monkeypatch.setenv("WEB_FETCH_ALLOW_PRIVATE_URLS", value)
    assert not allow_private_urls()
    assert not _allow_private_urls()


@pytest.mark.parametrize("value", ["1", "true", " yes ", "ON"])
def test_ssrf_explicit_opt_in(monkeypatch, value):
    monkeypatch.setenv("BROWSER_ALLOW_PRIVATE_URLS", value)
    monkeypatch.setenv("WEB_FETCH_ALLOW_PRIVATE_URLS", value)
    assert allow_private_urls() and _allow_private_urls()


def test_deprecated_ipv6_site_local_is_not_public():
    assert MCPURLValidator._is_blocked_ip(ipaddress.ip_address("fec0::123"))
