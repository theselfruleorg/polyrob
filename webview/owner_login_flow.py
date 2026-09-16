"""Owner-login render helper + its two pure guards (030 S5 / 043), extracted per
the god-file ratchet.

EVERY render mints a FRESH CSRF nonce cookie + matching token — the failure
re-renders included (the 429/403/401 paths used to pass ``csrf_token=None``,
the template then omitted the hidden field, and every following POST failed
CSRF with 403 — one typo'd password bricked the form until a manual re-GET).
``csrf_token`` is None only when no JWT secret is configured, where CSRF
enforcement is skipped anyway (matching the no-auth posture).
"""
import os
from typing import Optional


def csrf_token_for(nonce: Optional[str]) -> Optional[str]:
    """Stateless double-submit token: HMAC(JWT_SECRET_KEY, nonce).

    The login POST is the one mutation ``webgate.csrf_guard`` cannot defend — it
    has no session yet, so there is no ambient cookie to protect. Hence this.

    None when no JWT secret is configured (local/dev without auth) — CSRF
    enforcement is skipped there, matching the no-auth posture.
    """
    secret = os.environ.get("JWT_SECRET_KEY")
    if not secret or not nonce:
        return None
    import hashlib
    import hmac as _hmac
    return _hmac.new(secret.encode(), f"owner-login:{nonce}".encode(),
                     hashlib.sha256).hexdigest()


def safe_return_to(raw) -> str:
    """Only same-origin relative paths — kills open redirects via return_to."""
    to = str(raw or "/")
    if not to.startswith("/") or to.startswith("//") or "\\" in to:
        return "/"
    return to


def render_owner_login(request, *, return_to: str = "/", error=None,
                       status_code: int = 200):
    import secrets as _secrets

    import webview.server as _srv

    nonce = _secrets.token_hex(16)
    response = _srv._templates.TemplateResponse(request, "owner_login.html", {
        "request": request, "return_to": return_to, "error": error,
        "csrf_token": csrf_token_for(nonce),
    }, status_code=status_code)
    response.set_cookie(
        "csrf_nonce", nonce, max_age=600, httponly=True, samesite="lax",
        secure=(os.environ.get("ENVIRONMENT", "production") == "production"), path="/owner-login",
    )
    return response
