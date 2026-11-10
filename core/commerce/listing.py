"""Listing projections (099 §3.7): one manifest, many shapes. Pure functions.

Every projection reads ONLY the buyer-facing part of a confirmed manifest
(title, summary, description, price, rails, input schema, deliverable kind) —
never the playbook, the tools, the budgets or the resources.

* :func:`public_offer`  — the plain JSON the storefront, MCP and A2A answer with;
* :func:`json_ld`       — schema.org ``Product`` + ``Offer`` for ``GET /api/offers``;
* :func:`a2a_skill`     — one agent-card skill per live offer;
* :func:`input_schema`  — the JSON Schema a machine buyer fills.

Tier-0: imports nothing above ``core``.
"""
from __future__ import annotations

from typing import Any, Dict, List

from core.commerce.offer import OfferManifest


def input_schema(m: OfferManifest) -> Dict[str, Any]:
    props, required = {}, []
    for i in m.inputs:
        prop: Dict[str, Any] = {"type": i.type}
        if i.type == "string":
            prop["maxLength"] = i.max_length
        if i.format == "evm_address":
            prop["pattern"] = "^0x[0-9a-fA-F]{40}$"
        elif i.format:
            prop["format"] = {"email": "email", "url": "uri"}.get(i.format, i.format)
        if i.description:
            prop["description"] = i.description
        if i.role == "recipient":
            prop["description"] = (prop.get("description", "") + " (receives the delivery)").strip()
        if i.role == "refund":
            prop["description"] = (prop.get("description", "") + " (refunds go here)").strip()
        props[i.name] = prop
        if i.required:
            required.append(i.name)
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def public_offer(m: OfferManifest, *, base_url: str = "") -> Dict[str, Any]:
    out = {"id": m.id, "title": m.title, "summary": m.summary,
           "description": m.description, "price": m.price_label(),
           "pricing": {"kind": m.pricing_kind, "amount_usd": m.amount_usd},
           "pay_with": [a for a, _mode in m.rails], "delivers": m.deliverable_kind,
           "inputs": input_schema(m)}
    if base_url:
        out["order_url"] = f"{base_url.rstrip('/')}/api/offers/{m.id}/orders"
    return out


def json_ld(m: OfferManifest, *, base_url: str = "") -> Dict[str, Any]:
    offer: Dict[str, Any] = {"@type": "Offer", "availability": "https://schema.org/InStock"}
    if m.amount_usd:
        offer.update({"price": m.amount_usd, "priceCurrency": "USD"})
    if base_url:
        offer["url"] = f"{base_url.rstrip('/')}/api/offers/{m.id}"
    return {"@context": "https://schema.org", "@type": "Product", "productID": m.id,
            "name": m.title, "description": m.summary or m.description, "offers": offer}


def a2a_skill(m: OfferManifest) -> Dict[str, Any]:
    return {"id": f"offer:{m.id}", "name": m.title,
            "description": f"{m.summary} Price: {m.price_label()}. Send a data part "
                           f"{{\"offer_id\": \"{m.id}\", \"inputs\": {{…}}}} to order.",
            "tags": ["offer", m.deliverable_kind],
            "inputModes": ["application/json"], "outputModes": ["application/json"]}


def a2a_pricing(manifests: List[OfferManifest]) -> List[Dict[str, Any]]:
    return [{"offer_id": m.id, "skill": f"offer:{m.id}", "amount_usd": m.amount_usd,
             "pricing": m.pricing_kind, "assets": [a for a, _ in m.rails]} for m in manifests]


def a2a_order_part(message: Any) -> Any:
    """The order data part of an A2A ``message`` (``{"offer_id"|"order_id", …}``), or
    ``None``. The ONE predicate: the A2A RPC answers such a message as an order (no
    compute task), and the x402 middleware therefore does not bill it as one (110 W0)."""
    if not isinstance(message, dict):
        return None
    for part in message.get("parts") or ():
        if isinstance(part, dict) and part.get("kind") == "data" and isinstance(
                part.get("data"), dict):
            d = part["data"]
            if "offer_id" in d or "order_id" in d:
                return d
    return None


__all__ = ["a2a_order_part", "a2a_pricing", "a2a_skill", "input_schema", "json_ld",
           "public_offer"]
