"""Owner-login render helper (030 S5), extracted per the god-file ratchet.

EVERY render mints a FRESH CSRF nonce cookie + matching token — the failure
re-renders included (the 429/403/401 paths used to pass ``csrf_token=None``,
the template then omitted the hidden field, and every following POST failed
CSRF with 403 — one typo'd password bricked the form until a manual re-GET).
``csrf_token`` is None only when no JWT secret is configured, where CSRF
enforcement is skipped anyway (matching the no-auth posture).
"""
import os


def render_owner_login(request, *, return_to: str = "/", error=None,
                       status_code: int = 200):
    import secrets as _secrets

    import webview.server as _srv

    nonce = _secrets.token_hex(16)
    response = _srv._templates.TemplateResponse(request, "owner_login.html", {
        "request": request, "return_to": return_to, "error": error,
        "csrf_token": _srv._csrf_token_for(nonce),
    }, status_code=status_code)
    response.set_cookie(
        "csrf_nonce", nonce, max_age=600, httponly=True, samesite="lax",
        secure=(os.environ.get("ENVIRONMENT", "production") == "production"), path="/owner-login",
    )
    return response
