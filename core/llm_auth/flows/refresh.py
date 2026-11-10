"""The token refresh: one call, one lock per provider, and the store the caller holds.

Separate from ``registry`` because the inference path calls it on every request: this
module imports no connect flow and no default store.
"""
from __future__ import annotations

import threading
import time
from contextlib import ExitStack
from typing import Any, Dict, Optional

from core.llm_auth.flows.base import (
    FlowResult,
    HttpPost,
    exchange_token,
    post_form,
    require_oauth_spec_obj,
    result_from_token_payload,
)

#: In-process refresh serialization, one lock per provider.
#:
#: This mirrors the discipline in tools/oauth/manager.py (per-key lock + an
#: identity guard on the observed token) rather than importing it: that manager
#: is agent-tier and serializes a different store, and core must not import
#: upward. Cross-PROCESS safety is the store's per-provider refresh lock file
#: (``AuthStore.refresh_lock``), taken inside this lock: a second process waits,
#: then finds a changed access token and never posts the used refresh token.
_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(provider: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(provider)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[provider] = lock
        return lock


def _hold_refresh_lock(stack: ExitStack, store: Any, provider: str) -> bool:
    """Enter the store's cross-process refresh lock; False on a timeout (the caller then
    returns None and reads the store again). A store with no such lock passes."""
    lock = getattr(store, "refresh_lock", None)
    if lock is None:
        return True
    return stack.enter_context(lock(provider)) is not False


def refresh_token(provider: str, oauth: Any, *, store: Any,
                  http_post: Optional[HttpPost] = None,
                  now: Optional[float] = None) -> Optional[FlowResult]:
    """Refresh *provider*'s token if it is at/near expiry. None if untouched.

    "Near" is the provider's OWN ``refresh_skew_sec`` — observed values differ
    by an order of magnitude across providers (minutes vs an hour), so it is a
    per-provider field rather than one global constant.

    Serialized per provider, with an identity guard: if the stored token changed
    while this call waited for the lock, another caller already refreshed it and
    this one returns None instead of burning a second (often single-use)
    refresh token.
    """
    now = time.time() if now is None else now

    entry = store.get_provider(provider)
    if not entry:
        return None
    # An exchanged credential (Copilot) renews by REDOING the exchange with the
    # retained OAuth token, not by an OAuth refresh grant.
    if entry.get("exchange_source_token"):
        expires_at = entry.get("expires_at")
        oauth_spec = require_oauth_spec_obj(oauth, provider)
        skew = float(getattr(oauth_spec, "refresh_skew_sec", 120) or 0)
        if expires_at is not None and now < float(expires_at) - skew:
            return None
        observed = entry.get("access_token")
        with _lock_for(provider), ExitStack() as stack:
            if not _hold_refresh_lock(stack, store, provider):
                return None
            current = store.get_provider(provider) or {}
            if current.get("access_token") != observed:
                return None                # another caller renewed it while we waited
            source = current.get("exchange_source_token")
            if not source:
                return None
            exchanged = exchange_token(
                getattr(oauth_spec, "token_exchange_url", ""), source)
            extra = {"exchange_source_token": source}
            if exchanged.scope:
                extra["api_base_url"] = exchanged.scope
            return _write_if_unchanged(store, provider, current.get("access_token"),
                                       {**exchanged.to_entry(), **extra}, exchanged)
    if not entry.get("refresh_token"):
        return None
    expires_at = entry.get("expires_at")
    if expires_at is None:
        return None                        # no expiry declared: nothing to pre-empt

    oauth = require_oauth_spec_obj(oauth, provider)
    skew = float(getattr(oauth, "refresh_skew_sec", 120) or 0)
    if now < float(expires_at) - skew:
        return None                        # still comfortably valid

    observed = entry.get("access_token")
    with _lock_for(provider), ExitStack() as stack:
        if not _hold_refresh_lock(stack, store, provider):
            return None
        current = store.get_provider(provider) or {}
        if current.get("access_token") != observed:
            return None                    # someone else refreshed it while we waited
        post = http_post or post_form
        payload = post(oauth.token_url, {
            "grant_type": "refresh_token",
            "client_id": oauth.client_id,
            "refresh_token": current["refresh_token"],
        })
        result = result_from_token_payload(payload, now=now)
        # A provider that rotates refresh tokens returns a new one; one that
        # doesn't returns none, and dropping the old one would strand the seat.
        if not result.refresh_token:
            result = FlowResult(
                access_token=result.access_token,
                refresh_token=current.get("refresh_token"),
                expires_at=result.expires_at,
                scope=result.scope or str(current.get("scope") or ""),
                token_type=result.token_type,
            )
        return _write_if_unchanged(store, provider, observed, result.to_entry(), result)


def _write_if_unchanged(store: Any, provider: str, observed: Any,
                        fields: Dict[str, Any], result: FlowResult) -> Optional[FlowResult]:
    """Merge *fields* into the entry under the store's flock, only while it still holds the
    token this refresh started from. The token POST ran outside the flock, so another
    process may have refreshed or re-connected meanwhile: its entry stands (None)."""
    def _merge(current):
        if current is None or current.get("access_token") != observed:
            return None
        return {**current, **fields}
    return result if store.update_provider(provider, _merge) is not None else None
