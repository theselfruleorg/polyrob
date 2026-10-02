"""The ONE pinned, bounded httpx transport for SSRF-validated outbound requests.

``pinned_resolver.py`` pins aiohttp; this module pins httpx. Every caller
validates a URL once (``url_policy.MCPURLValidator.validate_and_resolve``) and
then connects through :class:`PinnedAsyncTransport`, which sends to exactly the
validated IP while it keeps the original host identity (Host header and TLS SNI).

Moved here from ``tools/x402/net_guard.py`` (067 P0.10): the A2A push webhook
uses it and has nothing to do with payment. ``tools/x402/net_guard.py`` keeps its
names and binds its own x402 bounds to them.

httpx is imported at module scope (not lazily): a transport must be a REAL
``httpx.AsyncBaseTransport`` subclass at class-definition time, or httpx rejects
it at construction. httpx ships with the base ``openai`` dependency.
"""
from __future__ import annotations

import asyncio

import httpx

from core.security.pinned_resolver import host_key

#: Default raw-body bound (2 MiB), applied before httpx decodes the body.
MAX_BODY_BYTES = 2_097_152

#: Default total budget for one request, the body read included.
HTTP_TIMEOUT_SEC = 30.0


class BoundedAsyncTransport(httpx.AsyncBaseTransport):
    """Bound raw responses before SDK/HTTPX decoding, retaining every header.

    ``max_body_bytes`` and ``timeout_sec`` are read on each request, so a
    subclass may bind them to its own policy.
    """

    max_body_bytes: int = MAX_BODY_BYTES
    timeout_sec: float = HTTP_TIMEOUT_SEC

    def __init__(self, inner=None):
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        request.headers["Accept-Encoding"] = "identity"
        max_bytes = self.max_body_bytes

        async def bounded_response():
            response = await self._inner.handle_async_request(request)
            extensions = dict(response.extensions)
            body = bytearray()
            try:
                encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    raise ValueError("compressed response refused before decoding")
                async for chunk in response.stream:
                    room = max_bytes - len(body)
                    body.extend(chunk[:room])
                    if len(chunk) > room:
                        extensions["polyrob_body_truncated"] = True
                        break
            finally:
                await response.aclose()
            headers = response.headers.copy()
            # The bounded body may differ in length from the upstream framing.
            for name in ("Content-Length", "Transfer-Encoding", "Content-Encoding"):
                headers.pop(name, None)
            return httpx.Response(response.status_code, headers=headers,
                                  content=bytes(body), extensions=extensions)

        return await asyncio.wait_for(bounded_response(), timeout=self.timeout_sec)

    async def aclose(self):
        await self._inner.aclose()


class PinnedAsyncTransport(BoundedAsyncTransport):
    """httpx transport that connects to ``pinned_ip`` while keeping the original
    host identity.

    Validating a hostname and then letting the HTTP client re-resolve it leaves a
    DNS-rebinding window: the attacker answers publicly for the validation lookup
    and privately for the connection. Pinning closes it. The rewrite MUST preserve
    the ``Host`` header (virtual hosting) and ``extensions["sni_hostname"]`` (TLS
    SNI + certificate verification), or the fix trades SSRF for a broken TLS chain.
    """

    def __init__(self, hostname: str, pinned_ip: str, inner=None):
        self._hostname = hostname
        self._host_key = host_key(hostname)
        self._pinned_ip = pinned_ip
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport()
        if not self._pinned_ip or not self._host_key:
            raise ValueError("a pinned transport needs a host and an IP")

    async def handle_async_request(self, request):
        url = request.url
        try:
            wire_host = url.raw_host.decode("ascii")
        except Exception:
            wire_host = ""
        if wire_host == self._pinned_ip:
            # Already rewritten by this transport: the x402 SDK re-sends the
            # SAME request object on its paying leg, and the rewrite below
            # mutated it in place. Host and SNI were set on the first pass.
            return await super().handle_async_request(request)
        if host_key(wire_host) != self._host_key:
            # A request for any other host would go out UNPINNED — a fresh DNS
            # lookup is exactly the rebinding window this class exists to
            # close. Fail closed (net_guard Low, 2026-09-23).
            raise httpx.ConnectError(
                f"refusing unpinned request to {wire_host or url.host!r}",
                request=request)
        request.url = url.copy_with(host=self._pinned_ip)
        # httpx already built Host from the URL (with a non-default port); keep
        # it, and only fill it in when a caller removed it.
        if "Host" not in request.headers:
            request.headers["Host"] = wire_host
        request.extensions = dict(request.extensions or {})
        request.extensions["sni_hostname"] = wire_host
        return await super().handle_async_request(request)


__all__ = ["BoundedAsyncTransport", "HTTP_TIMEOUT_SEC", "MAX_BODY_BYTES",
           "PinnedAsyncTransport"]
