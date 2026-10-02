"""Mounted inbound webhook server: GET (verify handshake) + POST (delegate to the
registered WebhookSurface). Surfaces register in container['webhook_surfaces']. Always
acks 200 fast on POST (the surface processes fail-open) so a platform never retry-storms.

064 F4: a surface may answer with a ``WebhookResponse`` (status, content type, body),
rendered verbatim; a dict answer keeps the ``{"ok": …}`` JSON. The request body is
capped (the surface's catalog ``body_cap_bytes``) BEFORE it is read whole — this route
is auth-exempt, so an unbounded body is an unauthenticated memory lever."""
import logging
from typing import Callable, Optional

from fastapi import APIRouter, Request, Response

logger = logging.getLogger(__name__)
router = APIRouter()

_container_provider: Optional[Callable[[], object]] = None


def set_container_provider(fn: Callable[[], object]) -> None:
    """api/app.py sets this so the router resolves the live container per request."""
    global _container_provider
    _container_provider = fn


def _container():
    return _container_provider() if _container_provider else None


def _render(out) -> Response:
    """A ``WebhookResponse`` verbatim; a dict as today's ``{"ok": …}`` JSON."""
    from core.surfaces.inbound_webhook import WebhookResponse
    if isinstance(out, WebhookResponse):
        return Response(status_code=out.status, media_type=out.content_type,
                        content=out.body)
    ok = bool((out or {}).get("ok"))
    return Response(status_code=200 if ok else 401, media_type="application/json",
                    content='{"ok": %s}' % ("true" if ok else "false"))


async def _read_capped(request: Request, cap: int) -> Optional[bytes]:
    """The request body, or None once it passes ``cap`` bytes (read stops there)."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > cap:
                return None
        except ValueError:
            return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/webhooks/{surface_id}")
async def verify(surface_id: str, request: Request):
    c = _container()
    surfaces = (c.get_service("webhook_surfaces") if c else None) or {}
    surface = surfaces.get(surface_id)
    if surface is None:
        return Response(status_code=404)
    challenge = surface.verify_challenge(dict(request.query_params))
    if challenge is None:
        return Response(status_code=403)
    from core.surfaces.inbound_webhook import WebhookResponse
    if isinstance(challenge, WebhookResponse):
        return _render(challenge)
    return Response(content=str(challenge), media_type="text/plain")


@router.post("/webhooks/{surface_id}")
async def receive(surface_id: str, request: Request):
    c = _container()
    surfaces = (c.get_service("webhook_surfaces") if c else None) or {}
    surface = surfaces.get(surface_id)
    if surface is None:
        return Response(status_code=404)
    from core.surfaces.inbound_webhook import body_cap_for
    body = await _read_capped(request, body_cap_for(surface_id))
    if body is None:
        logger.warning("%s webhook: request body over the cap — refused", surface_id)
        return Response(status_code=413, media_type="application/json",
                        content='{"ok": false}')
    task_agent = c.get_service("task_agent") if c else None
    if task_agent is None and c is not None and hasattr(c, "get_agent"):
        task_agent = c.get_agent("task_agent")
    out = await surface.handle_post(c, dict(request.headers), body, task_agent)
    return _render(out)
