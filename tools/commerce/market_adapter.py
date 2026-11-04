"""099 W7: a generic MCP-marketplace listing adapter — no marketplace named in code.

An operator who enrolled the agent on a marketplace that speaks MCP (its server is
in the operator ``mcp.json`` overlay) describes it ONCE in the data home, in
``<data_home>/commerce_markets.json``::

    [{"name": "acme",                      # the listing kind is market:acme
      "server": "acme",                    # the MCP server name in mcp.json
      "publish_tool": "create_listing",    # called with {offer_arg: <public offer>}
      "withdraw_tool": "delete_listing",   # optional; called with {"offer_id": …}
      "orders_tool": "list_new_orders",    # optional; returns [{offer_id, inputs, buyer, ref}]
      "reply_tool": "send_payment_request",# optional; {"ref": …, "payment": …}
      "offer_arg": "offer"}]

A manifest lists ``market:acme``; go-live publishes the projection
(``core.commerce.listing.public_offer``: title, price, inputs — never the
playbook, the tools or the budgets). Each settlement tick pulls new marketplace
orders into ``create_order`` (channel ``market:acme``) and hands the buyer OUR
invoice; a marketplace "paid" claim counts for nothing — only our own on-chain
settlement pays an order.

⚠️ AP2-style mandates (an off-chain signature that moves value) are out of scope:
``wallet_sign`` refuses value-moving typed data, and this adapter signs nothing.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CONFIG_NAME = "commerce_markets.json"


def config_path(data_dir: Optional[str] = None) -> str:
    from core.runtime_paths import data_home_db_path
    return data_home_db_path(CONFIG_NAME, data_dir=data_dir)


def load_config(data_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """The operator's market rows; ``[]`` when the file is absent. A file that
    exists and cannot be read is LOGGED (an unreadable config is not "no markets")."""
    p = config_path(data_dir)
    if not os.path.exists(p):
        return []
    try:
        rows = json.loads(open(p, encoding="utf-8").read())
    except Exception:
        logger.warning("commerce: %s is unreadable — no marketplace is listed", p,
                       exc_info=True)
        return []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and r.get("name") and r.get("server") and r.get("publish_tool"):
            out.append(r)
        else:
            logger.warning("commerce: market row %r lacks name/server/publish_tool", r)
    return out


class MarketAdapter:
    """One configured marketplace, reached through the operator's MCP server."""

    def __init__(self, row: Dict[str, Any], call_tool):
        self.row = dict(row)
        self.kind = f"market:{str(row['name']).lower()}"
        self._call = call_tool   # async (server, tool, arguments) -> result

    async def _tool(self, name: Optional[str], arguments: Dict[str, Any]) -> Any:
        if not name:
            return None
        return await self._call(self.row["server"], name, arguments)

    async def publish(self, manifest, *, offer_url: str) -> Optional[str]:
        from core.commerce.listing import public_offer
        offer = public_offer(manifest)
        offer["order_url"] = offer_url
        res = await self._tool(self.row["publish_tool"],
                               {self.row.get("offer_arg", "offer"): offer})
        return str(res)[:200] if res is not None else None

    async def withdraw(self, manifest) -> None:
        await self._tool(self.row.get("withdraw_tool"), {"offer_id": manifest.id})

    async def sync_orders(self) -> List[Dict[str, Any]]:
        res = await self._tool(self.row.get("orders_tool"), {})
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except ValueError:
                return []
        if isinstance(res, dict):
            res = res.get("orders") or []
        return [o for o in (res or []) if isinstance(o, dict) and o.get("offer_id")]

    async def reply(self, ref: str, payload: Dict[str, Any]) -> None:
        await self._tool(self.row.get("reply_tool"), {"ref": ref, **payload})


def mcp_caller(container: Any):
    """``call(server, tool, arguments)`` over the operator's MCP tool, or None."""
    tool = None
    for name in ("mcp", "mcp_tool"):
        try:
            if container is not None and container.has_service(name):
                tool = container.get_service(name)
                break
        except Exception:
            tool = None
    mgr = getattr(tool, "server_manager", None)
    if mgr is None:
        return None

    async def _call(server: str, tool_name: str, arguments: Dict[str, Any]):
        return await mgr.execute_tool(server, tool_name, arguments, timeout=30)
    return _call


def register(container: Any, data_dir: Optional[str] = None) -> List[str]:
    """Register one adapter per configured market; returns their kinds."""
    from core.commerce.hooks import register_listing_adapter
    rows = load_config(data_dir)
    if not rows:
        return []
    call = mcp_caller(container)
    if call is None:
        logger.warning("commerce: %d market(s) configured but MCP is not running — none "
                       "is listed", len(rows))
        return []
    kinds = []
    for row in rows:
        a = MarketAdapter(row, call)
        register_listing_adapter(a)
        kinds.append(a.kind)
    return kinds


__all__ = ["MarketAdapter", "config_path", "load_config", "mcp_caller", "register"]
