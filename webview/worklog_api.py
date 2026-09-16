"""Work › Log — the classified activity stream, tenant-scoped (043 A16/§5).

``GET /api/webgate/log?class=&raw=&diagnostics=&limit=`` — read-only. It reads
the ONE activity stream :mod:`webview.activity` already builds (the per-session
feed plus the durable rows, backfilled by ``activity_backfill``), scopes it to
the caller's tenant, and stamps each event with the client-facing class
:func:`core.activity_class.classify` gives it. It records nothing.

Four honest-state rules this endpoint keeps:

1. **Tenant-scoped, fail-closed.** The tenant is
   :func:`webview.pages._effective_user_id`, which 403s an unbound own_ops
   console and a multitenant caller with no identity. Only events recorded for
   that tenant are returned — the global stream is never leaked whole.
2. **A source it cannot read is NAMED, never a silent drop.** When the read
   raises, the answer carries ``unreadable`` (the machine reason) with an empty
   list — the console renders that as a dashed entry, not as a day with nothing
   in it. An empty list with ``unreadable`` null is the genuine empty state.
3. **Diagnostics are off by default.** The low-signal engine kinds
   (:func:`core.activity_class.is_diagnostic`) are excluded unless
   ``diagnostics`` is set — the same default the switch on the page keeps.
4. **The raw record is opt-in.** The exact payload rides only when ``raw`` is
   set, so a summary-only consumer stays light and the payload is present the
   moment the page's per-row toggle asks for it.

``class`` filters to one member of :data:`core.activity_class.CLIENT_CLASSES`;
an unknown value is ignored (the whole list), never a 400 on a read.
"""
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from core.activity_class import CLIENT_CLASSES, classify, is_diagnostic

logger = logging.getLogger(__name__)

router = APIRouter()

_TRUTHY = {"1", "true", "yes", "on"}


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _recent_events(limit: int) -> List[Dict[str, Any]]:
    """The activity stream's own events, newest window first.

    Reuses the exact data path ``activity_backfill`` uses — the live hub ring
    buffer, falling back to a cold backfill over the stores — so the Log reads
    the same events the legacy ``/activity`` page did, with no second reader.
    May raise; the caller turns that into an honest ``unreadable`` answer.
    """
    from webview import activity
    hub = activity.get_hub()
    events = hub.recent(limit)
    if not events:
        events = activity._cold_backfill(limit)
    return events


@router.get("/api/webgate/log")
async def api_log(
    request: Request,
    class_: str = Query("", alias="class"),
    raw: str = "",
    diagnostics: str = "",
    limit: int = 200,
) -> JSONResponse:
    # Resolve the tenant FIRST and OUTSIDE any try — a 403 (unbound own_ops /
    # multitenant without identity) must propagate, never be swallowed into an
    # empty list.
    from webview.pages import _effective_user_id
    user_id = str(_effective_user_id(request))

    try:
        window = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        window = 200
    want_raw = _truthy(raw)
    want_diagnostics = _truthy(diagnostics)
    class_filter = class_ if class_ in CLIENT_CLASSES else ""

    try:
        events = _recent_events(window)
    except Exception as exc:
        logger.warning("activity log read failed for tenant %s", user_id, exc_info=True)
        return JSONResponse({
            "entries": [],
            "classes": list(CLIENT_CLASSES),
            "unreadable": f"{type(exc).__name__}: {exc}"[:200],
            "user_id": user_id,
        })

    entries: List[Dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        # Tenant isolation: only this caller's own events. An event with no
        # user_id belongs to no tenant and is not this caller's to read.
        if str(event.get("user_id") or "") != user_id:
            continue
        kind = str(event.get("kind") or "")
        diagnostic = is_diagnostic(kind)
        if diagnostic and not want_diagnostics:
            continue
        client_class = classify(kind)
        if class_filter and client_class != class_filter:
            continue
        entry: Dict[str, Any] = {
            "id": event.get("id"),
            "ts": event.get("ts"),
            "kind": kind,
            "cls": client_class,
            "diagnostic": diagnostic,
            "summary": str(event.get("summary") or ""),
        }
        if want_raw:
            entry["payload"] = event.get("payload")
        entries.append(entry)

    entries.sort(key=lambda item: item.get("ts") or 0.0, reverse=True)
    return JSONResponse({
        "entries": entries,
        "classes": list(CLIENT_CLASSES),
        "unreadable": None,
        "user_id": user_id,
    })
