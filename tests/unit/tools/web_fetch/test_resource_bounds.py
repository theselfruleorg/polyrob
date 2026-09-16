"""Fetch deadlines and byte limits apply before untrusted content is expanded."""
import asyncio
import time

import pytest

from tools.web_fetch.fetcher import WebFetchError, safe_fetch
from tests.unit.tools.web_fetch.test_fetcher import _FakeResp, _FakeSession, _factory


@pytest.mark.asyncio
async def test_compressed_response_refused_before_body_read():
    class Response(_FakeResp):
        async def iter_chunked(self, n):
            pytest.fail("compressed payload must not be read or expanded")
            yield b""

    class Session(_FakeSession):
        def get(self, *args, **kwargs):
            assert kwargs['headers']['Accept-Encoding'] == 'identity'
            return Response(200, {'Content-Encoding': 'gzip'}, [])

    with pytest.raises(WebFetchError, match='compressed response refused'):
        await safe_fetch('https://example.com', validate=False,
                         session_factory=lambda ip: Session([]))


@pytest.mark.asyncio
async def test_default_transport_disables_automatic_decompression(monkeypatch):
    import tools.web_fetch.fetcher as fetcher
    captured = {}
    monkeypatch.setattr(fetcher.aiohttp, 'TCPConnector', lambda **kw: object())
    monkeypatch.setattr(fetcher.aiohttp, 'ClientSession', lambda **kw: captured.update(kw))
    fetcher._default_session_factory(None, None)
    assert captured['auto_decompress'] is False


@pytest.mark.asyncio
async def test_dns_lookup_is_included_in_deadline():
    class SlowValidator:
        def validate_and_resolve(self, url):
            time.sleep(0.1)
            return True, None, '93.184.216.34'

    def forbidden(ip):
        pytest.fail('connection must not follow expired DNS validation')

    with pytest.raises(WebFetchError, match='total time limit'):
        await safe_fetch('https://example.com', timeout_sec=0.01,
                         validator=SlowValidator(), session_factory=forbidden)


@pytest.mark.asyncio
async def test_redirects_share_one_deadline_and_close_response():
    closed = []

    class Response(_FakeResp):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(True)

    class Session(_FakeSession):
        async def __aenter__(self):
            await asyncio.sleep(0.03)
            return self

        def get(self, *args, **kwargs):
            return Response(302, {'Location': '/next'}, [])

    with pytest.raises(WebFetchError, match='total time limit'):
        await safe_fetch('https://example.com', timeout_sec=0.05,
                         validate=False, session_factory=lambda ip: Session([]))
    assert closed == [True]


@pytest.mark.asyncio
async def test_validation_without_pinned_address_refused():
    class Validator:
        def validate_and_resolve(self, url):
            return True, None, None

    with pytest.raises(WebFetchError, match='blocked URL'):
        await safe_fetch('https://example.com', validator=Validator(),
                         session_factory=_factory([(200, {}, [b'ok'])]))
