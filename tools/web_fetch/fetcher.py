"""Stateless, SSRF-safe single-page fetch core (no browser, no Chromium).

Security model:
- Auto-redirects are OFF; every hop is re-validated.
- Each hop is validated with MCPURLValidator.validate_and_resolve(), which returns a
  pinned IP; the connection is pinned to that IP (Host/SNI preserved) so a DNS rebind
  between validation and connect cannot redirect the socket internally.
- Hard caps on redirects, total time (including DNS), and response bytes.
- Request identity encoding and disable automatic decompression; refuse servers
  that return compressed content, before any payload expansion.
"""

import asyncio
import ssl
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urljoin

import aiohttp
import certifi

from core.security.pinned_resolver import PinnedResolver, url_host_key

from tools.mcp.security import get_url_validator

_REDIRECT_STATUS = {301, 302, 303, 307, 308}

# An honest, disclosed bot UA. Many sites (e.g. Wikipedia) reject requests with no
# User-Agent; the "Mozilla/5.0 (compatible; ...)" form is the widely-accepted shape.
_DEFAULT_HEADERS = {
	"Accept-Encoding": "identity",
	"User-Agent": "Mozilla/5.0 (compatible; polyrob-web-fetch/1.0; +https://github.com/theselfruleorg)",
	"Accept": ("text/html,application/xhtml+xml,application/json;q=0.9,"
	           "application/xml;q=0.8,text/plain;q=0.8,*/*;q=0.5"),
}


class WebFetchError(Exception):
	"""Raised when a fetch is blocked, oversized, or exceeds the redirect cap."""


@dataclass
class FetchResult:
	final_url: str
	status: int
	content_type: str
	body: bytes


class _PinnedResolver(PinnedResolver):
	"""Force a single host to resolve to a pre-validated IP (defeats DNS rebinding).

	Thin name over the ONE shared resolver (``core.security.pinned_resolver``):
	the host is compared in its wire (IDNA) form and any other host RAISES —
	it never falls through to a fresh lookup (H14).
	"""


def _default_session_factory(pinned_ip: Optional[str], hostname: Optional[str]):
	ssl_ctx = ssl.create_default_context(cafile=certifi.where())
	if pinned_ip and not hostname:
		# A validated IP with no host to pin it to would mean an UNPINNED
		# connection. Fail closed.
		raise WebFetchError("cannot pin a URL with no usable host")
	if pinned_ip and hostname:
		connector = aiohttp.TCPConnector(resolver=_PinnedResolver(hostname, pinned_ip),
		                                 ssl=ssl_ctx, use_dns_cache=False)
	else:
		connector = aiohttp.TCPConnector(ssl=ssl_ctx)
	return aiohttp.ClientSession(connector=connector, auto_decompress=False)


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
	if max_bytes < 1 or max_redirects < 0 or timeout_sec <= 0:
		raise ValueError("fetch limits must be positive (redirect count may be zero)")
	try:
		return await asyncio.wait_for(_fetch_hops(
			url, max_bytes=max_bytes, max_redirects=max_redirects,
			timeout_sec=timeout_sec, validate=validate, validator=validator,
			session_factory=session_factory,
		), timeout=timeout_sec)
	except asyncio.TimeoutError as exc:
		raise WebFetchError("fetch exceeded its total time limit") from exc


async def _fetch_hops(url, *, max_bytes, max_redirects, timeout_sec,
                     validate, validator, session_factory):
	if validate and validator is None:
		validator = get_url_validator(allow_http=True)

	current = url
	for _hop in range(max_redirects + 1):
		pinned_ip: Optional[str] = None
		# WS-4 (compute posture): a NARROW exception for the agent's OWN sandbox server —
		# a loopback URL on a host port the sandbox actually published skips SSRF
		# validation and pins to 127.0.0.1. Empty unless the sandbox published a port
		# (posture >= 1); never matches RFC1918 or cloud metadata (not loopback).
		_loopback_ok = False
		try:
			from tools.shell.loopback_allow import is_loopback_allowed
			_loopback_ok = is_loopback_allowed(current)
		except Exception:
			_loopback_ok = False
		if validate and _loopback_ok:
			pinned_ip = "127.0.0.1"
		elif validate:
			# validate_and_resolve does a BLOCKING socket.getaddrinfo; offload it to a
			# thread so a slow/sinkhole DNS can't freeze the whole event loop (every
			# other session/request on this worker) for the resolver timeout.
			ok, err, pinned_ip = await asyncio.get_running_loop().run_in_executor(
				None, validator.validate_and_resolve, current)
			if not ok or not pinned_ip:
				raise WebFetchError(f"blocked URL ({current}): {err}")
		# The WIRE form of the host (IDNA), which is what aiohttp resolves.
		hostname = url_host_key(current)

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
				encoding = resp.headers.get("Content-Encoding", "identity").strip().lower()
				if encoding not in ("", "identity"):
					raise WebFetchError("server ignored identity encoding; compressed response refused")
				ctype = resp.headers.get("Content-Type", "application/octet-stream")
				buf = bytearray()
				async for chunk in resp.content.iter_chunked(8192):
					if len(chunk) > max_bytes - len(buf):
						raise WebFetchError(f"response exceeds {max_bytes} bytes ({current})")
					buf.extend(chunk)
				return FetchResult(final_url=current, status=resp.status,
				                   content_type=ctype, body=bytes(buf))
	raise WebFetchError(f"too many redirects (>{max_redirects})")
