"""Loopback ``Host`` allowlist for the ``local`` console posture (H15, 2026-09-23).

The local console authenticates its owner and checks mutation origins. A Host
allowlist also blocks DNS rebinding: an attacker's origin can otherwise agree
with its Host header while resolving that hostname to the local listener.

The one thing a rebinding page cannot change is the NAME in ``Host``: it is the
attacker's domain, never ``localhost``. So at ``local`` posture this ASGI
wrapper — outermost, in front of Socket.IO AND FastAPI — accepts only a loopback
host (``127.0.0.1``, ``localhost``, ``[::1]``, any port) and answers anything else
with ``421 Misdirected Request`` (a WebSocket is closed before accept).

``own_ops``/``multitenant`` are untouched: they sit behind nginx with their own
public ``Host`` and authenticate every request. The one operator-chosen
exception at ``local`` posture is the host of ``WEBVIEW_PUBLIC_URL`` when
``WEBVIEW_ALLOW_LOCAL_POSTURE`` is set — the documented "I front this console
with my own auth layer" override (``posture_guard``), whose proxy forwards the
public ``Host``. No new flag.

A request with NO ``Host`` header passes: HTTP/1.1 browsers always send one, so
a Host-less request is a machine client (HTTP/1.0 tooling), not a rebinding page.
"""
import logging
import os
from typing import Iterable, Optional, Set
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: The names a loopback console answers to. ``::1`` is matched bracket-stripped.
LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})

_REFUSAL_BODY = (b"Misdirected request: this console runs at the 'local' posture "
                 b"and answers only to 127.0.0.1, localhost or [::1].")


def hostname_of(host_header: Optional[str]) -> str:
    """The lower-cased host NAME of a ``Host`` header value, port stripped.

    ``[::1]:5050`` -> ``::1``; ``LocalHost:5050`` -> ``localhost``;
    ``localhost.`` -> ``localhost``. A malformed value yields ``""``.
    """
    raw = (host_header or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            return ""
        rest = raw[end + 1:]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return ""
        return raw[1:end]
    if raw.count(":") == 1:
        name, _, port = raw.partition(":")
        if port and not port.isdigit():
            return ""
        raw = name
    elif raw.count(":") > 1:
        return ""  # an unbracketed IPv6 literal is not a valid Host
    return raw.rstrip(".")


def _operator_hosts(env) -> Set[str]:
    """Hosts the OPERATOR named for a local console behind their own auth."""
    try:
        from webview.posture_guard import _allow_override
        if not _allow_override(env):
            return set()
        public = str(env.get("WEBVIEW_PUBLIC_URL") or "").strip()
        name = (urlparse(public).hostname or "").lower() if public else ""
        return {name} if name else set()
    except Exception:
        logger.debug("host guard: operator host probe failed", exc_info=True)
        return set()


def local_host_allowed(host_header: Optional[str], env=None) -> bool:
    """May a ``local``-posture console answer a request carrying this ``Host``?"""
    if host_header is None or not str(host_header).strip():
        return True
    name = hostname_of(host_header)
    if not name:
        return False
    if name in LOOPBACK_HOSTNAMES:
        return True
    return name in _operator_hosts(os.environ if env is None else env)


def _header(headers: Iterable, key: bytes) -> Optional[str]:
    for k, v in headers or ():
        if k.lower() == key:
            try:
                return v.decode("latin-1")
            except Exception:
                return ""
    return None


def _is_local_posture() -> bool:
    try:
        from webview import webgate
        return webgate.posture() == "local"
    except Exception:
        # Fail CLOSED: an unreadable posture is treated as `local`, the posture
        # with no login, so the allowlist still applies.
        logger.debug("host guard: posture unreadable (treat as local)", exc_info=True)
        return True


class LocalHostGuard:
    """ASGI wrapper enforcing :func:`local_host_allowed` at ``local`` posture."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind in ("http", "websocket") and _is_local_posture():
            host = _header(scope.get("headers"), b"host")
            if not local_host_allowed(host):
                logger.warning("H15 host guard: refused %s %s with Host %r (local posture)",
                               kind, scope.get("path"), host)
                if kind == "http":
                    await send({"type": "http.response.start", "status": 421,
                                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                            (b"content-length", str(len(_REFUSAL_BODY)).encode())]})
                    await send({"type": "http.response.body", "body": _REFUSAL_BODY})
                else:
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)


__all__ = ["LOOPBACK_HOSTNAMES", "LocalHostGuard", "hostname_of", "local_host_allowed"]
