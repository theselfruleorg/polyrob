"""Regression: @url context-ref loader must use the SSRF-safe fetcher, not urlopen.

urlopen follows redirects + re-resolves DNS at connect time, so a 302 to
169.254.169.254 (cloud metadata) or a DNS-rebinding host defeated the one-shot
_is_safe_url pre-check. _load_url now routes through safe_fetch (per-hop
re-validation + IP pinning + no auto-redirects).
"""
import types

import pytest

import agents.task.agent.messages.context_references as cr
import tools.web_fetch.fetcher as fetcher
from tools.web_fetch.fetcher import WebFetchError


def test_load_url_routes_through_safe_fetch(monkeypatch):
    seen = {}

    async def fake_safe_fetch(url, **kw):
        seen["url"] = url
        return types.SimpleNamespace(body=b"hello world")

    monkeypatch.setattr(fetcher, "safe_fetch", fake_safe_fetch)
    assert cr._load_url("http://example.com/x") == "hello world"
    assert seen["url"] == "http://example.com/x"


def test_load_url_returns_none_when_fetcher_blocks(monkeypatch):
    async def blocked(url, **kw):
        raise WebFetchError("blocked URL (redirect to metadata)")

    monkeypatch.setattr(fetcher, "safe_fetch", blocked)
    assert cr._load_url("http://attacker.tld/redirect-to-metadata") is None


def test_load_url_does_not_use_urlopen(monkeypatch):
    import urllib.request

    def _boom(*a, **k):
        raise AssertionError("_load_url must not use urlopen (follows redirects)")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    async def fake(url, **kw):
        return types.SimpleNamespace(body=b"ok")

    monkeypatch.setattr(fetcher, "safe_fetch", fake)
    assert cr._load_url("http://example.com") == "ok"
