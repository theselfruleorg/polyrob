"""Flow selection, connect/disconnect, and refresh."""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from core.llm_auth.errors import LLMAuthError
from core.llm_auth.flows.base import (
    FlowError,
    FlowResult,
    HttpPost,
    oauth_enabled,
    exchange_token,
    require_oauth_spec_obj,
)
from core.llm_auth.flows.device_code import run_device_code_flow
from core.llm_auth.flows.loopback_pkce import run_loopback_pkce_flow
from core.llm_auth.flows.manual_paste_pkce import run_manual_paste_pkce_flow
from core.llm_auth.flows.refresh import refresh_token


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
    """Refresh *provider*'s token in the default store, see :mod:`core.llm_auth.flows.refresh`."""
    return refresh_token(provider, oauth, store=_store(store), http_post=http_post, now=now)


def _store(store):
    if store is not None:
        return store
    from core.llm_auth.store import get_auth_store
    return get_auth_store()
