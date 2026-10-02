"""The X pack's console routes (``PackSpec.console_routers``), mounted by the
console under ``/api/packs/x`` (``webview/pack_console.py``).

* ``GET /oauth/callback`` — PUBLIC (``pack.toml`` ``[console] public_paths``):
  X redirects the owner's browser here after ``/x login``. The route's proof is
  the single-use, owner-bound, 10-minute ``state`` (``x_login_flow``), not a
  console session — the tab X opens may have none. It writes ONE record (the
  token pair) — the documented exception to ``WEBVIEW_READ_ONLY``, which refuses
  mutating methods only. It answers a small self-contained HTML page (no
  script) and sends the owner a notice through the one owner rail.
* ``GET /oauth/status`` — owner-only (every other pack console route is): the
  secret-free X login state as JSON.

Owner-only routes a later change adds (a paste-session form, a live browser
panel) belong on this same router: they get the owner guard and the read-only
+ CSRF guards from the mount, with nothing to declare here.
"""
from __future__ import annotations

import html
import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;
padding:0 1rem;line-height:1.5;color:#1b1b1b}}h1{{font-size:1.25rem}}
p.detail{{color:#555}}</style></head>
<body><h1>{title}</h1><p>{body}</p>{detail}</body></html>
"""


def _page(title: str, body: str, detail: str = "", status: int = 200) -> HTMLResponse:
    extra = f'<p class="detail">{html.escape(detail)}</p>' if detail else ""
    resp = HTMLResponse(_PAGE.format(title=html.escape(title), body=html.escape(body),
                                     detail=extra), status_code=status)
    # The URL carried a one-time code: keep the page out of every cache.
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _container():
    """The console's dependency container, when it has one (else None: the
    owner rail still records a durable owner notice)."""
    try:
        from core.container import DependencyContainer
        return getattr(DependencyContainer, "_instance", None)
    except Exception:  # noqa: BLE001
        return None


async def _notify_owner(owner_user_id: str, text: str) -> None:
    """The one owner rail (``core.surfaces.user_delivery``) — the same path the
    console's background verbs use. Fail-open: the page already told the owner."""
    if not owner_user_id:
        return
    try:
        from core.surfaces.user_delivery import deliver_user_message
        await deliver_user_message(_container(), owner_user_id, text,
                                   source="security", priority="critical")
    except Exception:  # noqa: BLE001
        logger.warning("x login: owner notice failed", exc_info=True)


@router.get("/oauth/callback")
async def oauth_callback(request: Request, state: str = "", code: str = "",
                         error: str = "", error_description: Optional[str] = None):
    from polyrob_x.x_login_flow import complete_login, discard
    if error:
        # The owner declined (or X refused). Burn the state so the link is dead.
        owner = discard(state)
        reason = f"X answered: {error}" + (f" ({error_description})" if error_description else "")
        await _notify_owner(owner, f"X login NOT renewed — {reason}. Send /x login to try again.")
        return _page("X login not renewed", "X did not grant access. Send /x login to try "
                     "again.", reason, status=400)
    result = complete_login(state, code)
    if not result.ok:
        await _notify_owner(result.owner_user_id,
                            f"X login NOT renewed — {result.reason}.")
        return _page("X login not renewed", "The login could not be completed.",
                     result.reason, status=400)
    # 2026-10-03: the page said "Access token valid for 1h 59m" and the owner
    # read it as "log in again every 2 hours". X access tokens live 2 hours and
    # the agent renews them itself; the owner logs in again only when X refuses
    # the refresh token (the agent then says so).
    await _notify_owner(result.owner_user_id,
                        "X login renewed. You do not need to do it again: I renew the "
                        "2-hour access token myself. I will tell you if X ever refuses it"
                        + (f" (scope: {result.scope})." if result.scope else "."))
    return _page("X login renewed", "X login renewed — you can close this tab.",
                 "You do not need to log in again: the agent renews the 2-hour access "
                 "token itself, and tells you if X ever refuses it.")


@router.get("/oauth/status")
async def oauth_status():
    from polyrob_x.owner_verbs import status_snapshot
    return JSONResponse(status_snapshot())


__all__ = ["router"]
