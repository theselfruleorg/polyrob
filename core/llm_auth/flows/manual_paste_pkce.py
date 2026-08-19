"""Authorization-code + PKCE with a MANUAL code paste (proposal 024, L2).

The third redirect shape, and the one Anthropic's OAuth uses: instead of
redirecting to a loopback port, the provider redirects to its own hosted
callback page, which DISPLAYS the authorization code for the user to copy back
into the terminal.

That makes it the only PKCE variant usable on a headless box — no listener, no
open port, and the browser can be on a completely different machine from the
agent. Where a provider supports it, prefer it over ``loopback_pkce``.

Unlike the loopback flow this is NOT gated on ``POLYROB_LOCAL``: nothing is
bound, nothing listens, and the code arrives by the operator's own hand.
"""
from __future__ import annotations

import hmac
import secrets
import time
import urllib.parse
from typing import Any, Callable, Optional

from core.llm_auth.flows.base import (
    FlowError,
    FlowResult,
    HttpPost,
    post_form,
    result_from_token_payload,
)
from core.llm_auth.flows.loopback_pkce import make_pkce_pair


def run_manual_paste_pkce_flow(
    oauth: Any,
    *,
    http_post: Optional[HttpPost] = None,
    on_prompt: Optional[Callable[[str], None]] = None,
    read_code: Optional[Callable[[], str]] = None,
    open_browser: Optional[Callable[[str], Any]] = None,
    **_ignored,
) -> FlowResult:
    """Build the authorize URL, take the pasted code, exchange it for tokens.

    ``on_prompt`` receives the authorize URL and ``read_code`` returns what the
    user pasted — both injected so this module never touches stdin/stdout and
    stays testable. ``**_ignored`` absorbs the flow kwargs the other flows take
    (``timeout``, ``local_mode``) so the registry can dispatch uniformly.
    """
    if not getattr(oauth, "auth_url", ""):
        raise FlowError("oauth block has no auth_url (browser authorize endpoint)")
    if not getattr(oauth, "redirect_uri", ""):
        raise FlowError(
            "redirect_mode 'manual' needs an explicit redirect_uri — the "
            "provider's hosted callback page that displays the code"
        )
    if read_code is None:
        raise FlowError("no code reader supplied (caller must collect the pasted code)")

    post = http_post or post_form
    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(32)

    params = {
        "response_type": "code",
        "client_id": oauth.client_id,
        "redirect_uri": oauth.redirect_uri,
        "scope": " ".join(oauth.scopes or ()),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = oauth.auth_url + ("&" if "?" in oauth.auth_url else "?") + \
        urllib.parse.urlencode(params)

    if on_prompt is not None:
        on_prompt(authorize_url)
    if open_browser is not None:
        try:
            open_browser(authorize_url)
        except Exception:
            pass          # printing the URL is the contract; opening it is a nicety

    pasted = (read_code() or "").strip()
    if not pasted:
        raise FlowError("no code entered — nothing was stored")

    # Providers commonly render the callback value as "<code>#<state>" so the
    # user pastes both in one go. Split it and VERIFY the state — skipping that
    # because the transport is a human copy/paste would drop the same CSRF
    # defence the loopback flow enforces.
    code, _, pasted_state = pasted.partition("#")
    code = code.strip()
    pasted_state = pasted_state.strip()
    if pasted_state and not hmac.compare_digest(pasted_state, state):
        raise FlowError("state mismatch — that code came from a different request")
    if not code:
        raise FlowError("no authorization code in the pasted value")

    fields = {
        "grant_type": "authorization_code",
        "client_id": oauth.client_id,
        "code": code,
        "redirect_uri": oauth.redirect_uri,
        "code_verifier": verifier,
        "state": state,
    }
    kwargs = {}
    ua = getattr(oauth, "token_user_agent", "")
    if ua:
        kwargs["headers"] = {"User-Agent": ua}
    payload = post(oauth.token_url, fields, **kwargs) if kwargs else post(oauth.token_url, fields)
    return result_from_token_payload(payload, now=time.time())
