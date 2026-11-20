"""099 W6: the machine listings of live offers — HTTP 402, MCP and A2A.

One manifest, three projections (``core.commerce.listing``), one order path
(``modules.commerce.service.create_order``). An external caller never gets more
than the storefront: list offers, create an order, read ITS order.

* **HTTP** (listing ``http402``) — ``GET /api/offers`` (schema.org JSON-LD +
  plain JSON), ``GET /api/offers/{id}``, ``POST /api/offers/{id}/orders``
  (returns the order, a bearer ``order_token`` and the payment request: pay the
  exact raw amount on-chain, or answer the x402 challenge at
  ``/api/x402/requests/{request_id}``), ``GET /api/orders/{id}`` (the token as
  ``?token=…`` or ``Authorization: Bearer``), and the buyer's acts
  ``POST /api/orders/{id}/{cancel|accept|dispute}`` with ``Authorization: Bearer
  <order_token>`` (or ``{"token"}`` in the JSON body) — never the token in the URL of
  a state-changing act (L-8: proxies and access logs keep URLs).
* **MCP serve** (listing ``mcp``) — ``polyrob_list_offers``,
  ``polyrob_create_order``, ``polyrob_order_status``, ``polyrob_order_act`` for an
  authenticated caller.
* **A2A** (listing ``a2a``) — a live offer is an agent-card skill; a
  ``message/send`` whose data part is ``{"offer_id", "inputs"}`` creates an order
  (``{"order_id"}`` reads it, ``{"order_id", "act", "reason"}`` acts on it). No compute credit is charged: the order carries
  its own invoice.

The public routes ride the per-IP limiter of the public invoice endpoints.
"""
from __future__ import annotations

import hmac
import logging
import secrets
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(tags=["offers"])

HTTP, MCP, A2A = "http402", "mcp", "a2a"


def _live(listing: str):
    from core.commerce.store import live_offers
    return live_offers(listing=listing)


def _base(request: Optional[Request]) -> str:
    if request is None:
        return ""
    try:
        from api.base_url import base_url
        return base_url(request)
    except Exception:
        return str(request.base_url).rstrip("/")


def _limit(request: Request, name: str) -> None:
    try:
        from api.x402_endpoints import _enforce_public_invoice_rate_limit
        _enforce_public_invoice_rate_limit(request, name)
    except HTTPException:
        raise
    except Exception:
        logger.debug("offers: rate limiter unavailable", exc_info=True)


@router.get("/offers")
async def list_offers(request: Request):
    _limit(request, "offers")
    from core.commerce.listing import json_ld, public_offer
    base = _base(request)
    ms = _live(HTTP)
    return {"offers": [public_offer(m, base_url=base) for m in ms],
            "jsonld": [json_ld(m, base_url=base) for m in ms]}


@router.get("/offers/{offer_id}")
async def show_offer(offer_id: str, request: Request):
    _limit(request, "offers")
    from core.commerce.listing import json_ld, public_offer
    for m in _live(HTTP):
        if m.id == offer_id.lower():
            base = _base(request)
            return {**public_offer(m, base_url=base), "jsonld": json_ld(m, base_url=base)}
    raise HTTPException(status_code=404, detail="no such offer for sale")


class OrderBody(BaseModel):
    inputs: Dict[str, Any] = Field(default_factory=dict)
    asset: Optional[str] = None


@router.post("/offers/{offer_id}/orders")
async def create_http_order(offer_id: str, body: OrderBody, request: Request):
    _limit(request, "offer_orders")
    from modules.commerce.service import OrderRefused, create_order
    from api.dependencies import get_trusted_client_ip, rate_key_for_ip
    token = secrets.token_urlsafe(18)
    try:
        # L-19: the per-origin cap counts this client address (an HTTP token is new per order)
        order, pay = await create_order(offer_id, channel="http", buyer=token,
                                        inputs=body.inputs, asset=body.asset,
                                        source="http402", listing=HTTP,
                                        origin=rate_key_for_ip(get_trusted_client_ip(request)))
    except OrderRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    url = f"{_base(request)}/api/orders/{order.id}"
    return {"order_id": order.id, "status": order.status, "order_token": token,
            "status_url": url, "payment": pay,
            "acts": {"auth": "send the order_token as 'Authorization: Bearer <order_token>'",
                     **{a: f"{url}/{a}" for a in ("cancel", "accept", "dispute")}}}


def _bearer(request: Request) -> str:
    scheme, _, value = (request.headers.get("Authorization") or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def _need(token: Optional[str]) -> str:
    if not token:
        raise HTTPException(status_code=401, detail=(
            "send the order_token as 'Authorization: Bearer <order_token>'"))
    return token


def _http_order(order_id: str, token: str):
    """The buyer's own HTTP order, proven by its bearer token — else 404 (no oracle)."""
    from core.commerce import orders as ords
    o = ords.get(order_id)
    if o is None or o.channel != "http" or not hmac.compare_digest(str(o.buyer), str(token)):
        raise HTTPException(status_code=404, detail="no such order")
    return o


@router.get("/orders/{order_id}")
async def http_order_status(order_id: str, request: Request,
                            token: Optional[str] = Query(default=None)):
    """The read-only status: the token as ``?token=`` (a status link) or Bearer."""
    _limit(request, "offer_orders")
    from modules.commerce.service import buyer_status
    return buyer_status(_http_order(order_id, _need(_bearer(request) or token)))


async def _buyer_act(o, act: str, reason: str = ""):
    """Every seat's buyer act (HTTP, MCP, A2A): the seat has proven the buyer;
    ``service.buyer_act`` decides. ``(done, buyer-safe text)``."""
    from modules.commerce.service import buyer_act
    try:
        from core.container import DependencyContainer
        container = DependencyContainer.get_instance()
    except Exception:
        container = None     # the owner notice of a dispute rides the durable fallback
    return await buyer_act(o, act, reason=str(reason or ""), container=container)


class OrderActBody(BaseModel):
    reason: str = ""
    token: str = ""


@router.post("/orders/{order_id}/{act}")
async def http_order_act(order_id: str, act: str, request: Request,
                         body: Optional[OrderActBody] = None):
    """F10: the buyer's acts over HTTP — ``cancel`` (unpaid), ``accept`` (delivered)
    and ``dispute`` (delivered; body ``{"reason"}``) — the SAME ``buyer_act`` the
    chat ``/order <id> <act>`` runs. 409 with the reason when the state refuses.
    L-8: the token rides in ``Authorization: Bearer`` (or the body); a ``?token=``
    in the URL is refused (``accept`` closes the buyer's dispute right)."""
    _limit(request, "offer_orders")
    from core.commerce import orders as ords
    from modules.commerce.service import BUYER_ACTS, buyer_status
    if "token" in request.query_params:
        raise HTTPException(status_code=400, detail=(
            "do not put the order token in the URL; send it as "
            "'Authorization: Bearer <order_token>'"))
    if act not in BUYER_ACTS:
        raise HTTPException(status_code=404, detail="no such order act")
    o = _http_order(order_id, _need(_bearer(request) or (body.token if body else "")))
    done, text = await _buyer_act(o, act, body.reason if body else "")
    if not done:
        raise HTTPException(status_code=409, detail=text)
    return {**buyer_status(ords.get(o.id) or o), "message": text}


# -- MCP serve -------------------------------------------------------------------------

MCP_TOOLS: List[Dict[str, Any]] = [
    {"name": "polyrob_list_offers", "description": "List what this agent sells (live offers).",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "polyrob_create_order",
     "description": "Order a live offer; returns the order id and the exact payment request.",
     "inputSchema": {"type": "object", "required": ["offer_id"], "additionalProperties": False,
                     "properties": {"offer_id": {"type": "string"},
                                    "inputs": {"type": "object"},
                                    "asset": {"type": "string"}}}},
    {"name": "polyrob_order_status", "description": "Read one of YOUR orders.",
     "inputSchema": {"type": "object", "required": ["order_id"], "additionalProperties": False,
                     "properties": {"order_id": {"type": "string"}}}},
    {"name": "polyrob_order_act",
     "description": "Act on one of YOUR orders: cancel (unpaid), accept or dispute "
                    "(delivered; give a reason).",
     "inputSchema": {"type": "object", "required": ["order_id", "act"],
                     "additionalProperties": False,
                     "properties": {"order_id": {"type": "string"},
                                    "act": {"type": "string",
                                            "enum": ["cancel", "accept", "dispute"]},
                                    "reason": {"type": "string"}}}},
]


def mcp_tools() -> List[Dict[str, Any]]:
    """The offer tools, listed only while a live offer lists ``mcp`` (never raises)."""
    try:
        return MCP_TOOLS if _live(MCP) else []
    except Exception:
        return []


def _text(payload: Any, error: bool = False) -> Dict[str, Any]:
    import json
    return {"content": [{"type": "text", "text": payload if isinstance(payload, str)
                         else json.dumps(payload, default=str)}], "isError": error}


async def mcp_call(name: str, arguments: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    from core.commerce import orders as ords
    from core.commerce.listing import public_offer
    from modules.commerce.service import OrderRefused, buyer_status, create_order
    buyer = f"mcp:{user_id}"
    if name == "polyrob_list_offers":
        return _text({"offers": [public_offer(m) for m in _live(MCP)]})
    if name == "polyrob_create_order":
        try:
            order, pay = await create_order(str(arguments.get("offer_id") or ""), channel="mcp",
                                            buyer=buyer, inputs=arguments.get("inputs") or {},
                                            asset=arguments.get("asset"), source="mcp",
                                            listing=MCP)
        except OrderRefused as exc:
            return _text(str(exc), error=True)
        return _text({"order_id": order.id, "status": order.status, "payment": pay})
    o = ords.get(str(arguments.get("order_id") or ""))
    if o is None or o.channel != "mcp" or o.buyer != buyer:
        return _text("no such order of yours", error=True)
    if name == "polyrob_order_act":      # F10: the same buyer act as HTTP and chat
        done, text = await _buyer_act(o, str(arguments.get("act") or ""),
                                      arguments.get("reason") or "")
        if not done:
            return _text(text, error=True)
        return _text({**buyer_status(ords.get(o.id) or o), "message": text})
    return _text(buyer_status(o))


# -- A2A ----------------------------------------------------------------------------------

def a2a_skills() -> List[Any]:
    """Agent-card skills for live offers listed on ``a2a`` (never raises)."""
    try:
        from api.a2a.agent_card import AgentSkill
        from core.commerce.listing import a2a_skill
        return [AgentSkill(**a2a_skill(m)) for m in _live(A2A)]
    except Exception:
        logger.debug("a2a: offer skills unavailable", exc_info=True)
        return []


def a2a_pricing() -> List[Dict[str, Any]]:
    try:
        from core.commerce.listing import a2a_pricing as _p
        return _p(_live(A2A))
    except Exception:
        return []


def _data_part(params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from core.commerce.listing import a2a_order_part
    return a2a_order_part((params or {}).get("message"))


async def a2a_order(params: Dict[str, Any], user_id: str) -> Optional[Dict[str, Any]]:
    """The reply to an A2A order/status message, or ``None`` when it is not one."""
    data = _data_part(params)
    if data is None:
        return None
    from core.commerce import orders as ords
    from modules.commerce.service import OrderRefused, buyer_status, create_order
    buyer = f"a2a:{user_id}"
    if data.get("offer_id"):
        try:
            order, pay = await create_order(str(data["offer_id"]), channel="a2a", buyer=buyer,
                                            inputs=data.get("inputs") or {},
                                            asset=data.get("asset"), source="a2a", listing=A2A)
            out = {"order_id": order.id, "status": order.status, "payment": pay}
            text = f"Order {order.id} created; pay the request in the data part."
        except OrderRefused as exc:
            out, text = {"error": str(exc)}, f"Order refused: {exc}"
    else:
        o = ords.get(str(data.get("order_id")))
        if o is None or o.channel != "a2a" or o.buyer != buyer:
            out, text = {"error": "no such order of yours"}, "No such order of yours."
        elif data.get("act"):                # F10: the same buyer act as HTTP and chat
            done, text = await _buyer_act(o, str(data["act"]), data.get("reason") or "")
            out = ({**buyer_status(ords.get(o.id) or o), "message": text} if done
                   else {"error": text})
        else:
            out, text = buyer_status(o), f"Order {o.id}: {o.status}"
    return {"kind": "message", "messageId": uuid.uuid4().hex, "role": "agent",
            "parts": [{"kind": "text", "text": text}, {"kind": "data", "data": out}]}


__all__ = ["MCP_TOOLS", "a2a_order", "a2a_pricing", "a2a_skills", "mcp_call", "mcp_tools",
           "router"]
