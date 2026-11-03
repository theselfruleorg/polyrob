"""401-retry-once for the MCP HTTP transports (T2.4; 098 TS-13).

``MCPTransport._send_with_auth_retry`` delegates here so ``protocol.py`` stays
under its line ceiling.

098 TS-13: the refresh callback gets the ``Authorization`` value the FAILED
request carried (``observed_auth``). ``OAuthManager.force_refresh`` uses it as
its identity guard, so two concurrent 401s on one token make ONE provider
refresh: the second caller sees the stored token no longer matches what it sent
and gets the new one back. A callback that takes no argument (every legacy and
test callback) is still called with no argument.
"""
from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Optional


def current_auth(transport: Any) -> Optional[str]:
    """The Authorization value the transport would send right now: the
    per-request ``headers`` merge wins, the session default is the fallback
    (the streamable transport bakes it into the aiohttp session)."""
    headers = getattr(transport, "headers", None)
    if headers:
        value = headers.get("Authorization")
        if value:
            return value
    session = getattr(transport, "session", None)
    try:
        value = session.headers.get("Authorization") if session is not None else None
    except Exception:
        value = None
    return value or None


def _takes_observed(callback: Callable[..., Any]) -> bool:
    try:
        params = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
               for p in params)


async def call_refresh(callback: Callable[..., Awaitable[Optional[str]]],
                       observed_auth: Optional[str]) -> Optional[str]:
    if _takes_observed(callback):
        return await callback(observed_auth)
    return await callback()


def apply_auth(transport: Any, new_auth: Optional[str]) -> None:
    if not new_auth:
        return
    headers = getattr(transport, "headers", None)
    if headers is not None:
        headers["Authorization"] = new_auth
    session = getattr(transport, "session", None)
    if session is not None:
        try:
            session.headers["Authorization"] = new_auth
        except Exception:
            pass


async def send_with_auth_retry(transport: Any, do_send: Callable[[], Awaitable[None]],
                               unauthorized: type) -> None:
    """See ``MCPTransport._send_with_auth_retry``."""
    # Read just before the send: do_send builds its headers before its first
    # await, so this is the value the request carried.
    sent_auth = current_auth(transport)
    try:
        await do_send()
        return
    except unauthorized as first:
        callback = getattr(transport, "_on_auth_refresh", None)
        if callback is None:
            raise first.as_protocol_error()
        transport.logger.warning(
            f"{transport.__class__.__name__}: got 401, refreshing auth and retrying once")
        try:
            new_auth = await call_refresh(callback, sent_auth)
        except Exception as refresh_exc:
            transport.logger.error(f"MCP auth refresh failed: {refresh_exc}")
            raise first.as_protocol_error()
        apply_auth(transport, new_auth)
        try:
            await do_send()
        except unauthorized as second:
            raise second.as_protocol_error()
