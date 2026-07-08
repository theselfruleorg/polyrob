"""WS-4: web_fetch reaches the agent's own published sandbox ports, nothing else.

The fetch loop validates every hop with the SSRF validator (which blocks loopback).
WS-4 adds a narrow exception: a loopback URL on a sandbox-published port skips
validation and pins to 127.0.0.1. An unpublished loopback port still raises.
"""
import pytest

from tools.web_fetch.fetcher import safe_fetch as fetch, WebFetchError
from tools.shell.loopback_allow import allow_loopback_ports, clear_loopback_ports


class _FakeResp:
    def __init__(self):
        self.status = 200
        self.headers = {"Content-Type": "text/html"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def content(self):
        class _C:
            async def iter_chunked(self, n):
                yield b"<html>self-served</html>"
        return _C()

    def get(self, *a, **k):
        return self


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, *a, **k):
        return _FakeResp()


def _session_factory(pinned_ip):
    return _FakeSession()


@pytest.fixture(autouse=True)
def _clean():
    clear_loopback_ports()
    yield
    clear_loopback_ports()


@pytest.mark.asyncio
async def test_fetch_allows_published_loopback_port():
    allow_loopback_ports([49153])
    # validator would normally block loopback; the allowlist short-circuits it
    res = await fetch("http://127.0.0.1:49153/", session_factory=_session_factory)
    assert res.status == 200
    assert b"self-served" in res.body


@pytest.mark.asyncio
async def test_fetch_blocks_unpublished_loopback_port():
    allow_loopback_ports([49153])

    class _Deny:
        def validate_and_resolve(self, url):
            return (False, "loopback blocked", None)

    with pytest.raises(WebFetchError):
        await fetch("http://127.0.0.1:9999/", validator=_Deny(),
                    session_factory=_session_factory)
