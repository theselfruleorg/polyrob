"""Money › Invoices: the agent's offers and their orders (113 W6, G-2).

``GET  /api/webgate/offers`` — every offer of the owner (``core.commerce.store``) with
what it sold (``core.commerce.orders.sold``), and the newest orders; the rows the
``/offer`` and ``/offer orders`` lines print (``modules.commerce.owner_verb``).
``POST /api/webgate/offers/{id}/{pause|resume}`` — the owner's word on one offer, run as
the line ``/offer pause|resume <id>`` through ``console_commands.run_console_line``: the
owner-verb plane, so the SAME gates as typing it in the chat box or on Telegram.

Only pause and resume are here. They undo each other and grant nothing. Setup, go live
and resolve grant authority or name money, so they stay on the cards and the verb
(``/offer setup|live|resolve``), and retire, which cancels unpaid orders for good, stays on
the verb. Nothing here spends, signs or changes a cap.

The wallet owner only (``pages._wallet_owner_id``, which takes the tenant from
``_effective_user_id``). A commerce store that will not read is ``readable: false``
with the reason, never "no offers". Contributed to the Money destination.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.event_kinds import CONSOLE_OFFER_VERB
from webview import webgate
from webview.audit import console_write

logger = logging.getLogger(__name__)

router = APIRouter()

VERBS = ("pause", "resume")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ORDER_LIMIT = 30


def _offer_row(o, sold) -> Dict[str, Any]:
    manifest = o.manifest if isinstance(o.manifest, dict) else {}
    return {"id": o.id, "state": o.state, "version": o.version,
            "title": str(manifest.get("title") or ""), "sold": sold,
            "updated_at": o.updated_at}


def _order_row(r) -> Dict[str, Any]:
    return {"id": r.id, "offer_id": r.offer_id, "status": r.status,
            "amount_usd": r.amount_usd, "channel": r.channel, "error": r.error,
            "created_at": r.created_at}


@router.get("/api/webgate/offers")
async def api_offers(request: Request):
    from core.commerce import orders as ords
    from core.commerce import store as st
    from webview.pages import _wallet_owner_id
    uid = str(_wallet_owner_id(request))
    try:
        offers = [_offer_row(o, ords.sold(o.id)) for o in st.list_offers(uid)]
        rows = ords.list_orders(user_id=uid, limit=ORDER_LIMIT + 1)
    except Exception as e:  # noqa: BLE001 — an unreadable store is not "no offers"
        logger.warning("offers: the commerce store could not be read", exc_info=True)
        return JSONResponse({"readable": False, "error": f"{type(e).__name__}: {str(e)[:160]}"})
    return JSONResponse({"readable": True, "offers": offers,
                         "orders": [_order_row(r) for r in rows[:ORDER_LIMIT]],
                         "orders_truncated": len(rows) > ORDER_LIMIT, "user_id": uid})


@router.post("/api/webgate/offers/{offer_id}/{verb}", dependencies=webgate.MUTATION_DEPS)
async def api_offer_verb(request: Request, offer_id: str, verb: str):
    from webview.pages import _wallet_owner_id
    uid = str(_wallet_owner_id(request))
    if verb not in VERBS or not _ID.match(str(offer_id).lower()):
        return JSONResponse({"ok": False, "error": "bad_verb"}, status_code=404)
    from webview.console_commands import run_console_line
    from webview.server import _in_process_task_agent
    line = f"/offer {verb} {str(offer_id).lower()}"
    reply = await run_console_line(_in_process_task_agent(), "money", uid, line)
    ok = str(reply or "").lstrip().startswith("✅")
    console_write(CONSOLE_OFFER_VERB, user_id=uid,
                  attrs={"offer_id": str(offer_id).lower(), "verb": verb, "ok": ok})
    return JSONResponse({"ok": ok, "message": str(reply or "")},
                        status_code=200 if ok else 409)


from webview.contributions import register_console_router  # noqa: E402

register_console_router(router, destination="money", source="webview.commerce_routes")
