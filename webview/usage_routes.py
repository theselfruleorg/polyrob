"""Money › Cash: what the agent's AI use cost (113 W6, G-3).

``GET /api/webgate/usage?days=N`` — ``core.status_economics.spend_view`` and its
ONE render ``spend_view_lines``, the same view and the same lines the ``/usage``
verb prints on Telegram and in the terminal (``surfaces/telegram/usage_ops.py``),
so the seats cannot disagree. A read: tenant-scoped through
``pages._effective_user_id``; an unreadable ledger is ``readable: false`` with the
reason, never a zero. Contributed to the Money destination
(``webview.contributions``).
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/webgate/usage")
async def api_usage(request: Request, days: str = "7"):
    from core.status_economics import spend_view, spend_view_lines
    from surfaces.telegram.usage_ops import parse_days
    from webview.pages import _data_dir, _effective_user_id
    user_id = str(_effective_user_id(request))
    n = parse_days([days])
    if n is None:
        return JSONResponse({"error": "bad_days", "detail": "bad_days"}, status_code=400)
    data_dir = _data_dir()
    try:
        view = spend_view(user_id, data_dir, now=time.time(), days=n)
    except Exception as e:  # noqa: BLE001 — an unreadable ledger is not $0
        logger.warning("usage: the AI cost records could not be read", exc_info=True)
        return JSONResponse({"readable": False, "days": n,
                             "error": f"{type(e).__name__}: {str(e)[:160]}"})
    try:
        from cron.digest import rail_names
        names = rail_names(user_id, data_dir)
    except Exception:  # a name is a label, not a number
        names = {}
    e = view["economics"]
    return JSONResponse({
        "readable": True, "days": view.get("days", n), "no_ledger": bool(view.get("no_ledger")),
        "runs_error": view.get("runs_error"), "calls": e.get("calls"), "cost_usd": e.get("cost_usd"),
        "lines": spend_view_lines(view, names=names),
    })


from webview.contributions import register_console_router  # noqa: E402

register_console_router(router, destination="money", source="webview.usage_routes")
