"""Authorization-code + PKCE flow over a loopback redirect (proposal 024, L2).

For providers that will not do device code. Strictly the second choice:
``device_code`` works on a headless box, this one cannot.

Hard rules, all of them load-bearing:

- **Refused unless ``POLYROB_LOCAL``.** A server must never open a redirect
  listener; on a multi-tenant box the "browser" that completes the flow is not
  the owner's.
- Bound to ``127.0.0.1`` on an **OS-assigned port** (never a fixed one, which
  another local process could squat to steal the code).
- **Single request, then the socket closes.** The server exists for exactly one
  callback and cannot be reused as an open listener.
- ``state`` is generated with ``secrets`` and **compared constant-time**; a
  mismatch aborts without exchanging anything.
- PKCE **S256** only — never ``plain``.
- Hard timeout, and the listener is torn down in a ``finally``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import secrets
import socket
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, Optional

from core.llm_auth.flows.base import (
    DEFAULT_FLOW_TIMEOUT_SEC,
    FlowError,
    FlowResult,
    HttpPost,
    post_form,
    result_from_token_payload,
)

_DONE_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>POLYROB</title>"
    b"<body style='font-family:system-ui;padding:3rem'>"
    b"<h1>Connected.</h1><p>You can close this tab and return to the terminal.</p>"
)
_FAIL_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>POLYROB</title>"
    b"<body style='font-family:system-ui;padding:3rem'>"
    b"<h1>Authorization failed.</h1><p>Check the terminal for details.</p>"
)


def make_pkce_pair() -> tuple:
    """(verifier, challenge) for PKCE S256 — RFC 7636 §4.1/§4.2."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Serves exactly one GET, records the query, and shuts the server down."""

    result: Dict[str, str] = {}

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        parsed = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        type(self).result = query
        ok = "code" in query and "error" not in query
        body = _DONE_PAGE if ok else _FAIL_PAGE
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Silence the default stderr access log — the request line carries the
        authorization CODE as a query parameter, and this class prints it
        unredacted by default."""


def run_loopback_pkce_flow(
    oauth: Any,
    *,
    http_post: Optional[HttpPost] = None,
    on_prompt: Optional[Callable[[str], None]] = None,
    timeout: float = DEFAULT_FLOW_TIMEOUT_SEC,
    local_mode: Optional[bool] = None,
    open_browser: Optional[Callable[[str], Any]] = None,
    **_ignored,
) -> FlowResult:
    """Run authorization-code + PKCE over an ephemeral loopback listener.

    ``on_prompt`` gets the authorize URL (the caller prints it; a browser is
    also opened when one is available). ``local_mode``/``open_browser`` are
    injected for tests.
    """
    if local_mode is None:
        try:
            from core.config_policy.policy import local_mode_enabled
            local_mode = local_mode_enabled()
        except Exception:
            local_mode = False
    if not local_mode:
        raise FlowError(
            "the loopback (browser redirect) flow is refused when POLYROB_LOCAL "
            "is unset — a server must not open a redirect listener. Use a "
            "device-code provider, or connect on your local machine and copy "
            "~/.polyrob/auth.json across."
        )
    if not getattr(oauth, "auth_url", ""):
        raise FlowError("oauth block has no auth_url (browser authorize endpoint)")

    post = http_post or post_form
    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(32)

    # Port 0 => the OS picks a free port. Binding a FIXED port would let any
    # other local process squat it first and receive the authorization code.
    server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    _CallbackHandler.result = {}
    try:
        server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/callback"
        authorize_url = oauth.auth_url + ("&" if "?" in oauth.auth_url else "?") + \
            urllib.parse.urlencode({
                "response_type": "code",
                "client_id": oauth.client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(oauth.scopes or ()),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            })

        if on_prompt is not None:
            on_prompt(authorize_url)
        if open_browser is not None:
            try:
                open_browser(authorize_url)
            except Exception:
                pass  # printing the URL is the contract; opening it is a nicety

        # ONE request, then the listener dies. handle_request() also honours
        # the socket timeout, so a callback that never arrives cannot hang.
        server.timeout = max(1.0, timeout)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        thread.join(timeout + 1.0)
        query = dict(_CallbackHandler.result)
    finally:
        _CallbackHandler.result = {}
        try:
            server.server_close()
        except Exception:
            pass

    if not query:
        raise FlowError("timed out waiting for the browser redirect")
    if query.get("error"):
        raise FlowError(
            f"authorization failed: {query['error']}"
            + (f" ({query['error_description']})" if query.get("error_description") else "")
        )
    # Constant-time: `state` is the CSRF defence, and comparing it with `!=`
    # leaks its prefix to anything that can time the callback.
    if not hmac.compare_digest(str(query.get("state", "")), state):
        raise FlowError("state mismatch — the redirect did not come from this request")
    code = query.get("code")
    if not code:
        raise FlowError("redirect carried no authorization code")

    payload = post(oauth.token_url, {
        "grant_type": "authorization_code",
        "client_id": oauth.client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    })
    return result_from_token_payload(payload, now=time.time())
