"""030 endpoint extracted from server.py per the god-file ratchet: the
telemetry fast-push (/api/internal/emit, localhost-only). It resolves server
singletons lazily at call time.

The preview serve-token mint that used to sit beside it was deleted (043 A30) —
it had no caller. See the note at the foot of this module."""
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from webview import webgate

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/internal/emit", dependencies=webgate.MUTATION_DEPS)
async def internal_emit(request: Request) -> Response:
    """Internal endpoint for direct event emission from telemetry service.

    SECURITY: Only accepts requests from localhost (127.0.0.1) — enforced here;
    the auth middleware lists this path as public FOR that reason (030 D12).
    """
    import webview.server as _srv

    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        logger.warning(f"Internal emit rejected from non-localhost: {client_host}")
        raise HTTPException(403, "Forbidden: localhost only")
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"Internal emit: invalid JSON body: {e}")
        raise HTTPException(400, "Invalid JSON body")
    session_id = body.get("session_id")
    event = body.get("event")
    if not session_id or not event:
        raise HTTPException(400, "Missing session_id or event")
    # Clients join the room named by the BARE clean id (join_session →
    # enter_room(sid, clean_id)); a "session:" prefix here would be a dead room.
    room = _srv.pm().clean_session_id(session_id)
    _srv._enrich_llm_event_with_cost(event)
    await _srv._sio.emit("feed_update", event, room=room)
    logger.debug(f"Internal emit: sent event to room {room}, _seq={event.get('_seq')}")
    return JSONResponse({"status": "ok", "room": room})


# 043 A30: the serve-token MINT endpoint is DELETED — nothing in
# `webview/static/` ever called it, so no preview iframe has ever carried a
# `?st=` token. `webview/serve_tokens.py` and the middleware's VERIFY half stay
# (server.py admits a valid `?st=` on a `/serve/` read), so a token handed out
# by some future seat still works; what is gone is an endpoint that minted a
# credential for nobody.
