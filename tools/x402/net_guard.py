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

#: The SHARED limit, not an enforcement mechanism — this module does not itself
#: cap any read; `client_kwargs()` below wires pinning/redirects/timeout only.
#: Same bound `discovery.probe_endpoint`'s streaming read uses. The cap is
#: actually applied at ONE call site: `tools/x402/real_client.py`'s
#: `fetch_with_payment` (the SDK paying leg), which TRUNCATES an oversized
#: response body — never discards the whole result — so a real settled
#: payment's spend-cap/idempotency record is never lost to an oversized body
#: (see that function's comment for the full reasoning). `x402_quote` and the
#: pre-flight probe inside `fetch_with_payment` currently carry no byte cap at
#: all — a known, separately-tracked gap (advisory legs only; the agent never
#: sees their raw body).
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
        ok, err, pinned = await asyncio.get_running_loop().run_in_executor(
            None, validator.validate_and_resolve, url)
    except Exception as exc:  # a broken validator must not open the gate
        return f"blocked URL (validator error: {exc})", None
    return (None, pinned) if ok else (f"blocked URL ({err})", None)


class PinnedAsyncTransport(httpx.AsyncBaseTransport):
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
        return await self._inner.handle_async_request(request)

    async def aclose(self):
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()


def _make_transport(url: str, pinned_ip: Optional[str]):
    """A pinning transport for *url*, or None when there is nothing to pin."""
    if not pinned_ip:
        return None
    host = urlparse(url).hostname
    if not host:
        return None
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
