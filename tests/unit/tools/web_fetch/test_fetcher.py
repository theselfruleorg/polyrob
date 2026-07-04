from urllib.parse import urlparse

import pytest

from tools.web_fetch.fetcher import safe_fetch, WebFetchError, FetchResult


# A fake aiohttp-ish response/session so tests never touch the network.
class _FakeResp:
	def __init__(self, status, headers, chunks):
		self.status = status
		self.headers = headers
		self._chunks = chunks
		self.content = self

	async def iter_chunked(self, n):
		for c in self._chunks:
			yield c

	async def __aenter__(self):
		return self

	async def __aexit__(self, *a):
		return False


class _FakeSession:
	def __init__(self, script):
		self._script = list(script)
		self.requested = []

	def get(self, url, allow_redirects, timeout, headers=None):
		self.requested.append(url)
		self.last_headers = headers
		status, hdrs, chunks = self._script.pop(0)
		return _FakeResp(status, hdrs, chunks)

	async def __aenter__(self):
		return self

	async def __aexit__(self, *a):
		return False

	async def close(self):
		pass


def _factory(script):
	def make(pinned_ip):
		return _FakeSession(script)
	return make


# Offline validator: approve public hosts, block loopback/metadata/private — no DNS.
class _FakeValidator:
	def validate_and_resolve(self, url):
		host = (urlparse(url).hostname or "").lower()
		if host in ("127.0.0.1", "localhost", "169.254.169.254") or host.startswith("10."):
			return (False, f"blocked host: {host}", None)
		return (True, None, "93.184.216.34")


@pytest.mark.asyncio
async def test_blocks_metadata_ip():
	# Uses the REAL validator (no factory): numeric IP needs no network DNS.
	with pytest.raises(WebFetchError):
		await safe_fetch("http://169.254.169.254/latest/meta-data/")


@pytest.mark.asyncio
async def test_blocks_redirect_to_localhost():
	script = [(302, {"Location": "http://127.0.0.1/secret", "Content-Type": "text/html"}, [])]
	with pytest.raises(WebFetchError):
		await safe_fetch("http://example.com/", validator=_FakeValidator(), session_factory=_factory(script))


@pytest.mark.asyncio
async def test_byte_cap_aborts():
	big = b"x" * 4096
	script = [(200, {"Content-Type": "text/html"}, [big, big, big])]
	with pytest.raises(WebFetchError):
		await safe_fetch("http://example.com/", max_bytes=5000, validate=False, session_factory=_factory(script))


@pytest.mark.asyncio
async def test_happy_path_returns_body():
	script = [(200, {"Content-Type": "text/html; charset=utf-8"}, [b"<html><body>hi</body></html>"])]
	res = await safe_fetch("http://example.com/", validate=False, session_factory=_factory(script))
	assert isinstance(res, FetchResult)
	assert res.status == 200 and b"hi" in res.body and "text/html" in res.content_type


@pytest.mark.asyncio
async def test_sends_user_agent_header():
	session = None

	def make(pinned_ip):
		nonlocal session
		session = _FakeSession([(200, {"Content-Type": "text/html"}, [b"<html><body>ok</body></html>"])])
		return session

	await safe_fetch("http://example.com/", validate=False, session_factory=make)
	assert session.last_headers and "User-Agent" in session.last_headers


@pytest.mark.asyncio
async def test_redirect_cap():
	script = [
		(302, {"Location": "http://example.org/a"}, []),
		(302, {"Location": "http://example.net/b"}, []),
		(302, {"Location": "http://example.com/c"}, []),
		(302, {"Location": "http://example.org/d"}, []),
		(302, {"Location": "http://example.net/e"}, []),
		(302, {"Location": "http://example.com/f"}, []),
	]
	with pytest.raises(WebFetchError):
		await safe_fetch("http://example.com/", max_redirects=2, validator=_FakeValidator(), session_factory=_factory(script))
