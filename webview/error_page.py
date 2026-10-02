"""The console's error page: one handler for every HTTP error (070 W0.18, E.34).

Starlette raises its OWN ``HTTPException`` for a path no route matches, and the
console's handler was registered for FastAPI's subclass only — so
``/no-such-page`` answered raw ``{"detail":"Not Found"}`` with no frame and no
way back. The handler now takes Starlette's class (FastAPI's subclass is still
caught through the hierarchy) and delegates here.

* An API path (``/api/…``) and a static asset answer JSON, as before: both
  ``detail`` and ``error`` carry the same string.
* A page answers ``error.html`` with a plain lead for its status class, a chat
  hint only on a chat path, and the raw detail behind "Show details".
"""
from __future__ import annotations

import os

from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: F401 — server.py registers on it

from webview.copy import t

#: Paths whose callers read JSON, never a page.
_JSON_PREFIXES = ("/api/", "/static/", "/socket.io")
#: Paths that are a chat: the chat hint applies.
_CHAT_PREFIXES = ("/session/", "/c/")


def lead_key(status_code: int) -> str:
    """The copy key of the lead sentence for *status_code*."""
    if status_code == 404:
        return "error.not_found"
    if status_code in (401, 403):
        return "error.forbidden"
    if status_code >= 500:
        return "error.server"
    return "error.other"


def error_context(request, status_code: int, detail) -> dict:
    """The template's context: the lead, the hints, and the raw detail."""
    path = request.url.path
    hints = []
    if path.startswith(_CHAT_PREFIXES):
        hints.append(t("error.hint_chat"))
    hints.append(t("error.hint_url"))
    return {
        "request": request,
        "lead": t(lead_key(status_code)),
        "hints": hints,
        "details": "" if detail is None else str(detail),
        "ws_url": os.environ.get("WEBVIEW_WS_URL", ""),
        "version": os.environ.get("WEBVIEW_VERSION", "") or _version(),
    }


def _version() -> str:
    from core.version import get_version
    return get_version()


def error_response(request, exc, templates):
    """JSON for an API or asset path; the error page for everything else."""
    status_code = exc.status_code
    detail = exc.detail
    headers = getattr(exc, "headers", None) or None
    if request.url.path.startswith(_JSON_PREFIXES):
        return JSONResponse({"detail": detail, "error": detail},
                            status_code=status_code, headers=headers)
    return templates.TemplateResponse(request, "error.html",
                                      error_context(request, status_code, detail),
                                      status_code=status_code, headers=headers)
