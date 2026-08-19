"""RFC 8628 device-authorization flow (proposal 024, L2).

**The default flow.** Rob #1 is a headless VPS reached over SSH, where a
loopback browser redirect is worthless — nothing on that box can open a browser
and nothing on your laptop can reach its 127.0.0.1. Device code works
identically on a server and on a laptop: it prints a URL and a short code, you
authorize on whatever device has a browser, and the box polls for the token.

``OAuthSpec.auth_url`` is read as the **device authorization endpoint** for this
grant (RFC 8628 §3.1), not as a browser authorize URL — that is what a device
flow's first leg posts to.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from core.llm_auth.flows.base import (
    DEFAULT_FLOW_TIMEOUT_SEC,
    FlowError,
    FlowResult,
    HttpPost,
    post_form,
    result_from_token_payload,
)

DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

#: RFC 8628 §3.5 poll interval when the server does not specify one.
DEFAULT_POLL_INTERVAL_SEC = 5.0

#: Floor on the poll interval. A server that returns interval=0 (or a hostile
#: one that returns a negative) must not turn this into a busy-loop against the
#: token endpoint.
MIN_POLL_INTERVAL_SEC = 1.0


def run_device_code_flow(
    oauth: Any,
    *,
    http_post: Optional[HttpPost] = None,
    on_prompt: Optional[Callable[[Dict[str, Any]], None]] = None,
    timeout: float = DEFAULT_FLOW_TIMEOUT_SEC,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    **_ignored,
) -> FlowResult:
    """Run the device flow against *oauth* (an ``OAuthSpec``) and return tokens.

    ``on_prompt`` receives the device-authorization response so the CALLER owns
    presentation — this module never prints. That keeps it usable from the CLI,
    the REPL and a test alike, and keeps a verification URL out of any log this
    module might otherwise write.

    ``http_post``/``sleep``/``now`` are injected so the whole flow, including
    the backoff arms, is testable without a network or real elapsed time.
    """
    post = http_post or post_form
    fields = {"client_id": oauth.client_id, "scope": " ".join(oauth.scopes or ())}

    # OpenAI's device-auth leg takes a JSON body, not the form encoding RFC 8628
    # specifies. Declared per provider rather than sniffed, so a 400 from a
    # form-only endpoint can never be mistaken for a JSON one.
    if getattr(oauth, "device_auth_style", "form") == "json":
        if http_post is not None:
            device = post(oauth.auth_url, fields)
        else:
            from core.llm_auth.flows.base import post_json
            device = post_json(oauth.auth_url, fields)
    else:
        device = post(oauth.auth_url, fields)
    if device.get("error"):
        raise FlowError(
            f"device authorization refused: {device['error']}"
            + (f" ({device['error_description']})" if device.get("error_description") else "")
        )
    device_code = device.get("device_code")
    user_code = device.get("user_code")
    verification_uri = device.get("verification_uri") or device.get("verification_url")
    if not device_code or not user_code or not verification_uri:
        raise FlowError(
            "device authorization response missing device_code/user_code/"
            "verification_uri — endpoint may not implement RFC 8628"
        )

    if on_prompt is not None:
        on_prompt(device)

    interval = _interval_from(device)
    started = now()
    # The server's own expiry caps ours whenever it is the tighter of the two:
    # polling past it can only ever return expired_token.
    deadline = started + timeout
    try:
        if device.get("expires_in") is not None:
            deadline = min(deadline, started + float(device["expires_in"]))
    except (TypeError, ValueError):
        pass

    while True:
        remaining = deadline - now()
        if remaining <= 0:
            raise FlowError(
                "timed out waiting for authorization — run the connect command "
                "again and finish in the browser before the code expires"
            )
        sleep(min(interval, remaining))

        payload = post(oauth.token_url, {
            "client_id": oauth.client_id,
            "device_code": device_code,
            "grant_type": getattr(oauth, "token_grant_type", "") or DEVICE_CODE_GRANT,
        })
        error = payload.get("error")
        if not error:
            return result_from_token_payload(payload, now=now())

        if error == "authorization_pending":
            continue                       # the user simply hasn't finished yet
        if error == "slow_down":
            # RFC 8628 §3.5: add 5s to the interval, and keep polling.
            interval += 5.0
            continue
        if error == "expired_token":
            raise FlowError("the device code expired before it was authorized")
        if error == "access_denied":
            raise FlowError("authorization was denied")
        raise FlowError(
            f"token request failed: {error}"
            + (f" ({payload['error_description']})" if payload.get("error_description") else "")
        )


def _interval_from(device: Dict[str, Any]) -> float:
    try:
        interval = float(device.get("interval", DEFAULT_POLL_INTERVAL_SEC))
    except (TypeError, ValueError):
        interval = DEFAULT_POLL_INTERVAL_SEC
    return max(MIN_POLL_INTERVAL_SEC, interval)
