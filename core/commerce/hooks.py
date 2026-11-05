"""The upward seams of commerce (099 §3): the fulfiller, the goal canceller, the listing adapters.

* **Fulfiller** — ``fn(order, manifest, claim_token) -> goal_id``. The settlement
  side (``modules/commerce``) cannot import the goal board (``agents/``), so
  ``polyrob.app`` registers the function that writes the fulfillment goal.
  No fulfiller registered = the order stays ``paid`` and the watch tick names it
  to the owner; nothing is dropped.
* **Goal canceller** — ``fn(goal_id) -> bool``. Same reason, other direction: an
  order that takes its goal back (a retry, a passed deadline) stops that goal.
  None registered = the goal runs on, but it can no longer spend, deliver or fail
  the order (the money kernel and ``order_deliver`` bind to the CURRENT goal).
* **Listing adapters** — one :class:`ListingAdapter` per listing kind (``a2a``,
  ``mcp``, ``http402``, ``market:<name>``, ``native:<surface>``). A projection
  that is served on request (A2A card, MCP, HTTP) needs no ``publish``; a
  marketplace pack publishes and syncs orders.

Tier-0: stdlib only.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)

_FULFILLER: Optional[Callable[..., Optional[str]]] = None


def register_fulfiller(fn: Optional[Callable[..., Optional[str]]]) -> None:
    global _FULFILLER
    if fn is not None and not callable(fn):
        raise TypeError("the fulfiller must be callable")
    _FULFILLER = fn


def fulfiller() -> Optional[Callable[..., Optional[str]]]:
    return _FULFILLER


_GOAL_CANCELLER: Optional[Callable[[str], bool]] = None


def register_goal_canceller(fn: Optional[Callable[[str], bool]]) -> None:
    global _GOAL_CANCELLER
    if fn is not None and not callable(fn):
        raise TypeError("the goal canceller must be callable")
    _GOAL_CANCELLER = fn


def goal_canceller() -> Optional[Callable[[str], bool]]:
    return _GOAL_CANCELLER


class ListingAdapter(Protocol):
    """One place an offer is listed. ``kind`` matches the manifest's listing entry."""

    kind: str

    def publish(self, manifest: Any, *, offer_url: str) -> Optional[str]:
        """Make the offer visible there; return a reference (or ``None``)."""

    def withdraw(self, manifest: Any) -> None:
        """Stop listing it (a paused or retired offer)."""

    def sync_orders(self) -> List[Dict[str, Any]]:
        """New external orders: ``[{"offer_id", "inputs", "buyer", "ref"}]``."""


_ADAPTERS: Dict[str, ListingAdapter] = {}


def register_listing_adapter(adapter: ListingAdapter) -> None:
    kind = str(getattr(adapter, "kind", "") or "").strip().lower()
    if not kind:
        raise ValueError("a listing adapter needs a kind")
    if kind in _ADAPTERS and _ADAPTERS[kind] is not adapter:
        logger.warning("listing adapter %s replaced", kind)
    _ADAPTERS[kind] = adapter


def unregister_listing_adapter(kind: str) -> None:
    _ADAPTERS.pop(str(kind).lower(), None)


def listing_adapters() -> Dict[str, ListingAdapter]:
    return dict(_ADAPTERS)


__all__ = ["ListingAdapter", "fulfiller", "goal_canceller", "listing_adapters",
           "register_fulfiller", "register_goal_canceller", "register_listing_adapter",
           "unregister_listing_adapter"]
