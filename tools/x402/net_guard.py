"""Network hardening for the x402 PAYING verbs (H1b/N-D/N-E/N-F, audit 2026-08-22).

`tools/x402/discovery.py` — the read-only prober that can never pay — validates
the URL through the shared SSRF validator, pins the connection to the cleared IP,
refuses redirects, caps the read at 2 MiB and bounds the whole request with a
total timeout. `x402_quote` and `x402_fetch` — which take the same model-supplied
URL, issue the same server-side request, and (for fetch) return the response BODY
to the agent — had none of it.

This module is that hardening, factored so both verbs and the SDK leg use one
policy. The pinned/bounded transports themselves live in
``core/security/pinned_transport.py``; this module binds the x402 bounds to them. It does NOT re-implement the validator or the loopback exception; it
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

from core.security import pinned_transport as _core_transport

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


class _X402Bounds:
    """Bind the transport bounds to THIS module's names, read per request, so
    ``MAX_X402_BODY_BYTES`` / ``X402_HTTP_TIMEOUT_SEC`` stay the x402 policy
    (and the targets a test patches)."""

    @property
    def max_body_bytes(self) -> int:
        return MAX_X402_BODY_BYTES

    @property
    def timeout_sec(self) -> float:
        return X402_HTTP_TIMEOUT_SEC


# The transports live in core/security/pinned_transport.py (067 P0.10); these
# names keep every existing import and isinstance check working.
class BoundedAsyncTransport(_X402Bounds, _core_transport.BoundedAsyncTransport):
    """``core.security.pinned_transport.BoundedAsyncTransport`` at the x402 bounds."""


class PinnedAsyncTransport(_X402Bounds, _core_transport.PinnedAsyncTransport):
    """``core.security.pinned_transport.PinnedAsyncTransport`` at the x402 bounds."""


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
