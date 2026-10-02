"""Money › Book: which tokens Rob trusts, and the owner's word on one (W1).

``GET  /api/webgate/tokens``          — ``core.wallet.token_trust.trust_view``
``POST /api/webgate/tokens/{action}`` — trust | untrust | writeoff | unquarantine

The same functions ``/wallet tokens``, ``/writeoff`` and ``/unquarantine`` call
on Telegram and the REPL, so the three seats cannot disagree. The browser asks
for a confirm before it posts; the server refuses anyone but the wallet owner
(``pages._wallet_owner_id``) and every writer re-checks the owner turn itself.
Contributed to the Money destination (``webview.contributions``).
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.event_kinds import CONSOLE_TOKEN_DECIDE
from webview import webgate
from webview.audit import console_write
from webview.copy import t

router = APIRouter()

ACTIONS = ("trust", "untrust", "writeoff", "unquarantine")


def _positions_db():
    from core.open_positions import open_positions_db_path
    from webview.pages import _data_dir
    return open_positions_db_path(_data_dir())


@router.get("/api/webgate/tokens")
async def api_tokens(request: Request):
    """Trusted tokens by source, the not-trusted list, and every quarantined /
    written-off holding. Owner-only read; an unreadable source is NAMED."""
    from core.wallet.token_trust import trust_view
    from webview.pages import _wallet_owner_id
    uid = _wallet_owner_id(request)
    body = trust_view(uid, positions_db=_positions_db())
    body["user_id"] = uid
    return JSONResponse(body)


@router.post("/api/webgate/tokens/{action}", dependencies=webgate.MUTATION_DEPS)
async def api_tokens_act(request: Request, action: str):
    """The owner's decision on ONE token or holding. Body:
    ``{"chain": …, "address": …, "symbol"?: …, "reason"?: …}``."""
    from core.wallet import token_trust as tt
    from webview.pages import _wallet_owner_id
    uid = _wallet_owner_id(request)
    if action not in ACTIONS:
        return JSONResponse({"ok": False, "message": t("money.tokens.bad_action")},
                            status_code=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    chain = str(body.get("chain") or "").strip()
    address = str(body.get("address") or "").strip()
    if not chain or not address:
        return JSONResponse({"ok": False, "message": t("money.tokens.need_address")},
                            status_code=400)
    ctx = tt.owner_seat_ctx(uid)
    db = _positions_db()
    if action == "trust":
        ok, msg = tt.trust(ctx, chain, address, str(body.get("symbol") or "") or None,
                           positions_db=db)
    elif action == "untrust":
        ok, msg = tt.untrust(ctx, chain, address, positions_db=db)
    elif action == "writeoff":
        ok, msg = tt.write_off(ctx, chain, address, reason=str(body.get("reason") or ""),
                               execute=True, positions_db=db)
    else:
        ok, msg = tt.unquarantine(ctx, chain, address, execute=True, positions_db=db)
    console_write(CONSOLE_TOKEN_DECIDE, user_id=uid,
                  attrs={"action": action, "chain": chain, "address": address,
                         "ok": bool(ok)})
    return JSONResponse({"ok": bool(ok), "message": msg})


from webview.contributions import register_console_router  # noqa: E402

register_console_router(router, destination="money", source="webview.tokens_routes")

__all__ = ["router", "ACTIONS"]
