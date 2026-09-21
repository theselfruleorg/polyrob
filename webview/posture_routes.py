"""The account + admin page surfaces, registered BY POSTURE (043 W13).

`/signin` and the four `/admin*` pages were hand-registered routes in
`server.py` (alongside `/profile` and a `/settings` page that no longer exist),
and they disagreed with each other about what "you may not see this" looks
like:

- `/admin` answered a non-admin with **403 and an inline-`<script>alert()` page**
  while its three siblings answered a real `303` redirect — two denials, two
  shapes, one of them a CSP liability the console is trying to shed;
- the settings page was registered with a bare `@_fastapi.get`, outside the
  posture model its neighbours live in, so "which postures is this page part
  of?" had no answer to read. (That page was deleted in 043 phase 4; Agent ›
  Settings is the seat now.)

This module is that answer: ONE table (:data:`PAGES`) of
``path -> (template, postures, admin_only)``, mounted through one registrar. The
console's posture rule is unchanged and now uniform — **gating is by route
REGISTRATION**, so a page that is not part of a posture is absent (404), never
"present but denied"; and within multitenant, a non-admin reaching an admin page
is redirected home exactly like every sibling.

`server.py` keeps the routes whose bodies are real logic (`/`, `/owner-login`,
`/logout`) — the rows below are template renders with an auth-state context.

⚠️ **A page belongs here only while its data exists.** `/profile` was removed
(043 A20): `profile.html` drives `static/js/profile.js`, which fetches
`/api/payments/*`, and the payments router is not mounted in this app at all
(`server.py` sets `PAYMENT_ROUTER_MOUNTED = False` unconditionally). So every
panel on that page answered 404 — a screen that looks like an account page and
can state nothing about the account. Restoring it means mounting the payments
router first, then adding the row back.
"""
from typing import Dict, Optional, Tuple

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from webview import webgate

#: ``path -> (template, postures, admin_only)``. Adding a page is a row here,
#: not a new decorator: the posture tuple IS the visibility rule.
PAGES: Dict[str, Tuple[str, Tuple[str, ...], bool]] = {
    "/signin": ("signin.html", ("multitenant",), False),
    "/admin": ("admin/dashboard.html", ("multitenant",), True),
    "/admin/users": ("admin/users.html", ("multitenant",), True),
    "/admin/users/{user_id}": ("admin/user_detail.html", ("multitenant",), True),
    "/admin/activity": ("admin/activity.html", ("multitenant",), True),
}

#: Where a non-admin is sent. A redirect, not a 403 page: the caller is a
#: browser following a nav link, and the console never answers a human with an
#: inline script.
NON_ADMIN_REDIRECT = "/"


def _context(request: Request, *, is_admin: bool) -> dict:
    """The render context. Path params ride along by name (``/admin/users/{user_id}``
    renders ``target_user_id``) so a parameterised page needs no bespoke handler."""
    from utils.auth_utils import is_authenticated
    ctx = {
        "request": request,
        "is_authenticated": is_authenticated(request),
        "is_admin": is_admin,
    }
    target = request.path_params.get("user_id")
    if target is not None:
        ctx["target_user_id"] = target
    return ctx


def mount(app: FastAPI, templates, posture: Optional[str] = None) -> None:
    """Register the pages that belong to ``posture`` (default: the live one).

    ``templates`` is the caller's ``Jinja2Templates`` — the console resolves its
    asset base once in ``server.py`` and this module must render from the SAME
    one, never build a second.
    """
    current = posture or webgate.posture()
    for path, (template, postures, admin_only) in PAGES.items():
        if current not in postures:
            continue
        app.get(path, response_class=HTMLResponse)(
            _make_handler(templates, template, admin_only))


def _make_handler(templates, template: str, admin_only: bool):
    async def page(request: Request) -> Response:
        # ⚠️ The ROLE through the one predicate (webgate.request_is_admin), not
        # the cached boolean: a request identified on any path other than this
        # process's auth middleware carries a role and no flag, and this reader
        # would then redirect a real admin home with nothing said.
        is_admin = webgate.request_is_admin(request)
        if admin_only and not is_admin:
            return RedirectResponse(url=NON_ADMIN_REDIRECT, status_code=303)
        return templates.TemplateResponse(request, template,
                                          _context(request, is_admin=is_admin))
    return page


__all__ = ["PAGES", "NON_ADMIN_REDIRECT", "mount"]
