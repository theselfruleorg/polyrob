"""Stateless, SSRF-safe single-page fetch core (no browser, no Chromium).

Security model (see docs/plans/2026-06-29-web-fetch-tier1-IMPLEMENTATION-PLAN.md):
- Auto-redirects are OFF; every hop is re-validated.
- Each hop is validated with MCPURLValidator.validate_and_resolve(), which returns a
  pinned IP; the connection is pinned to that IP (Host/SNI preserved) so a DNS rebind
  between validation and connect cannot redirect the socket internally.
- Hard caps on redirects, total time, and response bytes (read incrementally so a
  decompression bomb is aborted before it is fully buffered).
"""

import asyncio
import socket
import ssl
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import aiohttp
import certifi
from aiohttp.abc import AbstractResolver

from tools.mcp.security import get_url_validator

_REDIRECT_STATUS = {301, 302, 303, 307, 308}

# An honest, disclosed bot UA. Many sites (e.g. Wikipedia) reject requests with no
# User-Agent; the "Mozilla/5.0 (compatible; ...)" form is the widely-accepted shape.
_DEFAULT_HEADERS = {
	"User-Agent": "Mozilla/5.0 (compatible; polyrob-web-fetch/1.0; +https://github.com/theselfruleorg)",
	"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
}


class WebFetchError(Exception):
	"""Raised when a fetch is blocked, oversized, or exceeds the redirect cap."""


@dataclass
class FetchResult:
	final_url: str
	status: int
	content_type: str
	body: bytes


class _PinnedResolver(AbstractResolver):
	"""Force a single host to resolve to a pre-validated IP (defeats DNS rebinding).

	aiohttp connects to the ``host`` field (our pinned IP) but uses ``hostname`` for
	TLS SNI and keeps the URL's host for the Host header, so pinning is transparent.
	"""

	def __init__(self, hostname: str, pinned_ip: str) -> None:
		self._hostname = hostname
		self._ip = pinned_ip

	async def resolve(self, host, port=0, family=socket.AF_INET):
		ip = self._ip if host == self._hostname else host
		return [{"hostname": host, "host": ip, "port": port,
		         "family": family, "proto": 0, "flags": 0}]

	async def close(self) -> None:  # pragma: no cover - trivial
		return None


def _default_session_factory(pinned_ip: Optional[str], hostname: Optional[str]):
	ssl_ctx = ssl.create_default_context(cafile=certifi.where())
	if pinned_ip and hostname:
		connector = aiohttp.TCPConnector(resolver=_PinnedResolver(hostname, pinned_ip), ssl=ssl_ctx)
	else:
		connector = aiohttp.TCPConnector(ssl=ssl_ctx)
	return aiohttp.ClientSession(connector=connector)


async def safe_fetch(
	url: str,
	*,
	max_bytes: int = 10_485_760,
	max_redirects: int = 5,
	timeout_sec: float = 15.0,
	validate: bool = True,
	validator=None,
	session_factory: Optional[Callable] = None,
) -> FetchResult:
	"""Fetch ``url`` with per-hop SSRF validation, IP pinning, no auto-redirects, and caps.

	Args:
		validate: when False, skip SSRF validation entirely (single-user/local only).
		validator: an object exposing ``validate_and_resolve(url) -> (ok, err, pinned_ip)``.
			Defaults to the shared MCP URL validator (allow_http=True). Injectable for tests.
		session_factory: ``factory(pinned_ip) -> async-context-session`` (injectable for tests).
	"""
	if validate and validator is None:
		validator = get_url_validator(allow_http=True)

	current = url
	for _hop in range(max_redirects + 1):
		pinned_ip: Optional[str] = None
		if validate:
			# validate_and_resolve does a BLOCKING socket.getaddrinfo; offload it to a
			# thread so a slow/sinkhole DNS can't freeze the whole event loop (every
			# other session/request on this worker) for the resolver timeout.
			ok, err, pinned_ip = await asyncio.get_running_loop().run_in_executor(
				None, validator.validate_and_resolve, current)
			if not ok:
				raise WebFetchError(f"blocked URL ({current}): {err}")
		hostname = urlparse(current).hostname

		if session_factory is not None:
			session_cm = session_factory(pinned_ip)
		else:
			session_cm = _default_session_factory(pinned_ip, hostname)

		async with session_cm as session:
			timeout = aiohttp.ClientTimeout(total=timeout_sec)
			async with session.get(current, allow_redirects=False, timeout=timeout, headers=_DEFAULT_HEADERS) as resp:
				if resp.status in _REDIRECT_STATUS:
					location = resp.headers.get("Location")
					if not location:
						raise WebFetchError(f"redirect with no Location ({current})")
					current = urljoin(current, location)
					continue
				ctype = resp.headers.get("Content-Type", "application/octet-stream")
				buf = bytearray()
				async for chunk in resp.content.iter_chunked(8192):
					buf.extend(chunk)
					if len(buf) > max_bytes:
						raise WebFetchError(f"response exceeds {max_bytes} bytes ({current})")
				return FetchResult(final_url=current, status=resp.status,
				                   content_type=ctype, body=bytes(buf))
	raise WebFetchError(f"too many redirects (>{max_redirects})")
