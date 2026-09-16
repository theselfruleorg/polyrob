"""Network hardening for the x402 PAYING verbs (H1b/N-D/N-E/N-F, audit 2026-08-22).

`tools/x402/discovery.py` — the read-only prober that can never pay — validates
the URL through the shared SSRF validator, pins the connection to the cleared IP,
refuses redirects, caps the read at 2 MiB and bounds the whole request with a
total timeout. `x402_quote` and `x402_fetch` — which take the same model-supplied
URL, issue the same server-side request, and (for fetch) return the response BODY
to the agent — had none of it.

This module is that hardening, factored so both verbs and the SDK leg use one
policy. It does NOT re-implement the validator or the loopback exception; it
delegates to the same ones web_fetch and discovery use.

Pure infrastructure — no action closures, so `from __future__` is safe here.
httpx is imported at module scope (not lazily): `PinnedAsyncTransport` must be a
REAL `httpx.AsyncBaseTransport` subclass at class-definition time, or httpx
rejects it at construction; a lazy import inside `__init__` cannot satisfy that.
httpx is already a hard dependency of `real_client.py`, so this adds no new
runtime dependency.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Shared response bound, enforced on raw bytes before HTTPX decoding and SDK buffering.
from tools.x402.discovery import MAX_PROBE_BYTES as MAX_X402_BODY_BYTES  # noqa: E402

#: Total request budget. httpx's 5s default is too tight for a settling payment
#: and unbounded is too loose; this is the one number both legs share.
X402_HTTP_TIMEOUT_SEC = 30.0


def _default_validator():
    """The SAME SSRF validator web_fetch and discovery use."""
    from tools.mcp.security import get_url_validator
    return get_url_validator(allow_http=True)


async def validate_x402_url(url: str, *, validator=None):
    """``(refusal, pinned_ip)`` — refusal is None when the URL may be contacted.

    Mirrors ``discovery._validate`` exactly, including the ONE loopback exception
    for the agent's own published sandbox port (never RFC1918, never metadata).
    ``validate_and_resolve`` does a blocking ``getaddrinfo``, so it is offloaded:
    a sinkholed DNS must not freeze the event loop for every other session.
    """
    try:
        from tools.shell.loopback_allow import is_loopback_allowed
        if is_loopback_allowed(url):
            return None, "127.0.0.1"
    except Exception:
        pass
    validator = validator if validator is not None else _default_validator()
    try:
        ok, err, pinned = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, validator.validate_and_resolve, url),
            timeout=X402_HTTP_TIMEOUT_SEC)
    except Exception as exc:  # a broken validator must not open the gate
        return f"blocked URL (validator error: {exc})", None
    return (None, pinned) if ok and pinned else (f"blocked URL ({err})", None)


class BoundedAsyncTransport(httpx.AsyncBaseTransport):
    """Bound raw responses before SDK/HTTPX decoding, retaining payment headers."""

    def __init__(self, inner=None):
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        request.headers["Accept-Encoding"] = "identity"

        async def bounded_response():
            response = await self._inner.handle_async_request(request)
            extensions = dict(response.extensions)
            body = bytearray()
            try:
                encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    raise ValueError("compressed x402 response refused before decoding")
                async for chunk in response.stream:
                    room = MAX_X402_BODY_BYTES - len(body)
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

        return await asyncio.wait_for(bounded_response(), timeout=X402_HTTP_TIMEOUT_SEC)

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
        self._pinned_ip = pinned_ip
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        url = request.url
        if self._pinned_ip and url.host == self._hostname:
            request.url = url.copy_with(host=self._pinned_ip)
            request.headers["Host"] = self._hostname
            request.extensions = dict(request.extensions or {})
            request.extensions["sni_hostname"] = self._hostname
        return await super().handle_async_request(request)



def _make_transport(url: str, pinned_ip: Optional[str]):
    """Always bound responses; additionally pin when validation resolved an IP."""
    if not pinned_ip:
        return BoundedAsyncTransport()
    host = urlparse(url).hostname
    if not host:
        raise ValueError("x402 URL requires a host")
    return PinnedAsyncTransport(host, pinned_ip)


def client_kwargs(url: str, pinned_ip: Optional[str]) -> dict:
    """The httpx.AsyncClient kwargs every x402 leg must use.

    ``follow_redirects=False`` is set EXPLICITLY rather than relying on httpx's
    default: the SDK re-sends the request after attaching payment, and a default
    is not a guarantee across versions.
    """
    kwargs: dict = {
        "timeout": httpx.Timeout(X402_HTTP_TIMEOUT_SEC),
        "follow_redirects": False,
    }
    transport = _make_transport(url, pinned_ip)
    if transport is not None:
        kwargs["transport"] = transport
    return kwargs
