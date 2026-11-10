"""099 W6: the machine listings of live offers — HTTP 402, MCP and A2A.

One manifest, three projections (``core.commerce.listing``), one order path
(``modules.commerce.service.create_order``). An external caller never gets more
than the storefront: list offers, create an order, read ITS order.

* **HTTP** (listing ``http402``) — ``GET /api/offers`` (schema.org JSON-LD +
  plain JSON), ``GET /api/offers/{id}``, ``POST /api/offers/{id}/orders``
  (returns the order, a bearer ``order_token`` and the payment request: pay the
  exact raw amount on-chain, or answer the x402 challenge at
  ``/api/x402/requests/{request_id}``), ``GET /api/orders/{id}?token=…``.
* **MCP serve** (listing ``mcp``) — ``polyrob_list_offers``,
  ``polyrob_create_order``, ``polyrob_order_status`` for an authenticated caller.
* **A2A** (listing ``a2a``) — a live offer is an agent-card skill; a
  ``message/send`` whose data part is ``{"offer_id", "inputs"}`` creates an order
  (``{"order_id"}`` reads it). No compute credit is charged: the order carries
  its own invoice.

The public routes ride the per-IP limiter of the public invoice endpoints.
"""
from __future__ import annotations

import hmac
import logging
import secrets
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
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
    token = secrets.token_urlsafe(18)
    try:
        order, pay = await create_order(offer_id, channel="http", buyer=token,
                                        inputs=body.inputs, asset=body.asset,
                                        source="http402", listing=HTTP)
    except OrderRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"order_id": order.id, "status": order.status, "order_token": token,
            "status_url": f"{_base(request)}/api/orders/{order.id}", "payment": pay}


@router.get("/orders/{order_id}")
async def http_order_status(order_id: str, token: str, request: Request):
    _limit(request, "offer_orders")
    from core.commerce import orders as ords
    from modules.commerce.fulfillment import public_status
    o = ords.get(order_id)
    if o is None or o.channel != "http" or not hmac.compare_digest(str(o.buyer), str(token)):
        raise HTTPException(status_code=404, detail="no such order")
    return public_status(o)


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
    from modules.commerce.fulfillment import public_status
    from modules.commerce.service import OrderRefused, create_order
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
    return _text(public_status(o))


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
    from modules.commerce.fulfillment import public_status
    from modules.commerce.service import OrderRefused, create_order
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
        else:
            out, text = dict(public_status(o)), f"Order {o.id}: {o.status}"
    return {"kind": "message", "messageId": uuid.uuid4().hex, "role": "agent",
            "parts": [{"kind": "text", "text": text}, {"kind": "data", "data": out}]}


__all__ = ["MCP_TOOLS", "a2a_order", "a2a_pricing", "a2a_skills", "mcp_call", "mcp_tools",
           "router"]
