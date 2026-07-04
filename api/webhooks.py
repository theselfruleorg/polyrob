"""Mounted inbound webhook server: GET (verify handshake) + POST (delegate to the
registered WebhookSurface). Surfaces register in container['webhook_surfaces']. Always
acks 200 fast on POST (the surface processes fail-open) so a platform never retry-storms."""
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
    return Response(content=str(challenge), media_type="text/plain")


@router.post("/webhooks/{surface_id}")
async def receive(surface_id: str, request: Request):
    c = _container()
    surfaces = (c.get_service("webhook_surfaces") if c else None) or {}
    surface = surfaces.get(surface_id)
    if surface is None:
        return Response(status_code=404)
    body = await request.body()
    task_agent = c.get_service("task_agent") if c else None
    if task_agent is None and c is not None and hasattr(c, "get_agent"):
        task_agent = c.get_agent("task_agent")
    out = await surface.handle_post(c, dict(request.headers), body, task_agent)
    status = 200 if out.get("ok") else 401
    return Response(status_code=status, media_type="application/json",
                    content='{"ok": %s}' % ("true" if out.get("ok") else "false"))
