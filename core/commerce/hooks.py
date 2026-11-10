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
* **Thread channels** (109) — one per surface whose pack can read and answer
  public reply threads (``core.commerce.thread.ThreadChannel``).
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


_THREAD_CHANNELS: Dict[str, Any] = {}


def register_thread_channel(surface: str, channel: Any) -> None:
    """109: a surface's public threads (``core.commerce.thread.ThreadChannel``). A
    surface with none stays DM-only. Registered by the surface's pack."""
    key = str(surface or "").strip().lower()
    if not key or channel is None:
        raise ValueError("a thread channel needs a surface and an object")
    _THREAD_CHANNELS[key] = channel


def unregister_thread_channel(surface: str) -> None:
    _THREAD_CHANNELS.pop(str(surface).lower(), None)


def thread_channel(surface: str) -> Optional[Any]:
    return _THREAD_CHANNELS.get(str(surface or "").lower())


def thread_channels() -> Dict[str, Any]:
    return dict(_THREAD_CHANNELS)


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
           "register_thread_channel", "thread_channel", "thread_channels",
           "unregister_listing_adapter", "unregister_thread_channel"]
