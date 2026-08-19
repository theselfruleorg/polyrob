"""Flow selection, connect/disconnect, and refresh (proposal 024, L2)."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

from core.llm_auth.errors import LLMAuthError
from core.llm_auth.flows.base import (
    FlowError,
    FlowResult,
    HttpPost,
    oauth_enabled,
    exchange_token,
    post_form,
    require_oauth_spec_obj,
    result_from_token_payload,
)
from core.llm_auth.flows.device_code import run_device_code_flow
from core.llm_auth.flows.loopback_pkce import run_loopback_pkce_flow
from core.llm_auth.flows.manual_paste_pkce import run_manual_paste_pkce_flow


class UnsupportedGrantError(LLMAuthError):
    """The provider declares a grant no flow implements."""


#: grant string -> flow callable. `device_code` is the DEFAULT and the one that
#: works on a headless box; `authorization_code`/`pkce` need a local browser.
_FLOWS: Dict[str, Callable[..., FlowResult]] = {
    "device_code": run_device_code_flow,
    "authorization_code": run_loopback_pkce_flow,
    "pkce": run_loopback_pkce_flow,
    "loopback_pkce": run_loopback_pkce_flow,
}

#: In-process refresh serialization, one lock per provider.
#:
#: This mirrors the discipline in tools/oauth/manager.py (per-key lock + an
#: identity guard on the observed token) rather than importing it: that manager
#: is agent-tier and serializes a different store, and core must not import
#: upward. Cross-PROCESS safety is not this lock's job — it comes from the
#: flock the auth store already takes around every mutation.
_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(provider: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(provider)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[provider] = lock
        return lock


def get_flow(grant: str, redirect_mode: str = "loopback") -> Callable[..., FlowResult]:
    """The flow callable for *grant*.

    For the authorization-code family, ``redirect_mode`` picks HOW the code
    comes back: a loopback listener (local machines only) or a manual paste from
    the provider's hosted callback page (works anywhere, including over SSH).
    """
    key = (grant or "device_code").strip().lower()
    flow = _FLOWS.get(key)
    if flow is None:
        raise UnsupportedGrantError(
            f"unsupported oauth grant {grant!r} (supported: {sorted(_FLOWS)})"
        )
    if flow is run_loopback_pkce_flow and (redirect_mode or "").strip().lower() == "manual":
        return run_manual_paste_pkce_flow
    return flow


def connect(provider: str, oauth: Any, *, store=None,
            http_post: Optional[HttpPost] = None,
            on_prompt=None, env=None, **flow_kwargs) -> FlowResult:
    """Run *provider*'s declared flow and persist the result to the auth store.

    ``oauth`` (an ``OAuthSpec``) is passed IN rather than looked up here. This
    module is core-tier and the provider registry lives in ``modules.llm`` —
    resolving it here would be an upward import the layering ratchet forbids
    (``core <- modules <- agents <- tools <- cli``). The CLI resolves the spec
    and hands it down, which is the right direction anyway: core owns the
    protocol, the surface owns provider discovery.

    Returns the FlowResult so a caller can report expiry/scope. The token itself
    is written to the store and is never returned as a printable string (see
    ``FlowResult.__repr__``).
    """
    if not oauth_enabled(env):
        raise FlowError(
            "OAuth connect is disabled — set LLM_OAUTH_ENABLED=true to enable "
            "it. It is off by default on every deployment, including local, "
            "because connecting a subscription seat is a deliberate act with a "
            "terms-of-service dimension."
        )
    oauth = require_oauth_spec_obj(oauth, provider)
    flow = get_flow(getattr(oauth, "grant", "device_code"),
                    getattr(oauth, "redirect_mode", "loopback"))

    result = flow(oauth, http_post=http_post, on_prompt=on_prompt, **flow_kwargs)

    store = _store(store)
    entry = result.to_entry()
    exchange_url = getattr(oauth, "token_exchange_url", "")
    if exchange_url:
        # The OAuth token is not the inference credential (Copilot). Exchange
        # it, store the SHORT-LIVED result as the access_token, and keep the raw
        # OAuth token so refresh can re-exchange — the exchange is not an OAuth
        # refresh grant, so `refresh_token` alone would not be enough.
        exchanged = exchange_token(exchange_url, result.access_token)
        entry = exchanged.to_entry()
        entry["exchange_source_token"] = result.access_token
        if result.refresh_token:
            entry["refresh_token"] = result.refresh_token
        if exchanged.scope:
            entry["api_base_url"] = exchanged.scope   # enterprise/proxied host
        result = exchanged
    store.set_provider(provider, entry)
    return result


def disconnect(provider: str, *, store=None) -> bool:
    """Forget *provider*'s stored credential. True if one was removed.

    Local only — this does not revoke the grant at the provider. Say so at the
    call site; a user who believes "remove" revoked access is worse off than one
    who knows to also revoke it in their account settings.
    """
    return _store(store).remove_provider(provider)


def refresh_if_needed(provider: str, oauth: Any, *, store=None,
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
    store = _store(store)
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
        with _lock_for(provider):
            current = store.get_provider(provider) or {}
            source = current.get("exchange_source_token")
            if not source:
                return None
            exchanged = exchange_token(
                getattr(oauth_spec, "token_exchange_url", ""), source)
            merged = dict(current)
            merged.update(exchanged.to_entry())
            merged["exchange_source_token"] = source
            if exchanged.scope:
                merged["api_base_url"] = exchanged.scope
            store.set_provider(provider, merged)
            return exchanged
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
    with _lock_for(provider):
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
        merged = dict(current)
        merged.update(result.to_entry())
        store.set_provider(provider, merged)
        return result


def _store(store):
    if store is not None:
        return store
    from core.llm_auth.store import get_auth_store
    return get_auth_store()
