"""The buyer seat (099 W3, §3.4): a deterministic storefront — no model, no tools.

A person the agent does not know may BUY; nobody gets steering. Three verbs,
answered by code from the confirmed manifests and the order rows:

* ``/offers`` (or Telegram's ``/start``) — the catalog of live offers listed on
  this surface;
* ``/buy <offer> field=value …`` — an order and its exact payment request;
* ``/order <id> [cancel | dispute <reason>]`` — the buyer's own order only.

Rules:

* **Public channels only.** A surface answers only while at least one LIVE offer
  lists ``native:<surface>`` (the switch is the owner's live listing — no flag).
  Email is never a buying channel (owner, 2026-11-03) and never reaches here.
* **No model reads buyer text here**, so a buyer cannot prompt-inject the
  storefront; buyer text reaches a model only inside the fulfillment task,
  fenced as DATA (``modules.commerce.fulfillment``).
* **Bounded:** per-sender rate limit; an over-limit or malformed line is
  answered briefly or dropped, never escalated.

Returns ``None`` for anything that is not a storefront line, so the caller's
normal routing (and its drop of strangers) is unchanged.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict, deque
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

VERBS = ("/offers", "/buy", "/order")
#: Commands per sender per window; beyond it the storefront stays silent.
RATE_MAX = 8
RATE_WINDOW_S = 60.0
_RATE: "OrderedDict[Tuple[str, str], deque]" = OrderedDict()


def _limited(surface: str, sender: str, now: float) -> bool:
    key = (surface, sender)
    q = _RATE.setdefault(key, deque())
    _RATE.move_to_end(key)
    while q and now - q[0] > RATE_WINDOW_S:
        q.popleft()
    if len(q) >= RATE_MAX:
        return True
    q.append(now)
    while len(_RATE) > 5000:
        _RATE.popitem(last=False)
    return False


def _verb(text: str, surface: str) -> Optional[str]:
    t = (text or "").strip()
    if not t.startswith("/"):
        return None
    head = t.split()[0].lower().split("@", 1)[0]
    if head == "/start" and surface == "telegram":
        return "/offers"
    return head if head in VERBS else None


def is_open(surface: str) -> bool:
    """True while a live offer lists ``native:<surface>``. Fail-closed."""
    from core.commerce.offer import native_surfaces
    if surface not in native_surfaces():
        return False
    try:
        from core.commerce.store import live_offers
        return bool(live_offers(listing=f"native:{surface}"))
    except Exception:
        logger.debug("storefront: live offers unreadable (closed)", exc_info=True)
        return False


def parse_fields(words: List[str]) -> Dict[str, str]:
    """``k=v`` words; a word without ``=`` continues the previous value (free text)."""
    out: Dict[str, str] = {}
    last = None
    for w in words:
        k, sep, v = w.partition("=")
        if sep and k and k.replace("_", "").isalnum() and k[0].isalpha():
            last = k.lower()
            out[last] = v
        elif last is not None:
            out[last] = f"{out[last]} {w}".strip()
        else:
            raise ValueError(f"I could not read {w!r} — use field=value")
    return out


def catalog_text(surface: str) -> str:
    from core.commerce.store import live_offers
    offers = live_offers(listing=f"native:{surface}")
    if not offers:
        return "Nothing is for sale here right now."
    lines = ["For sale:"]
    for m in offers:
        lines.append(f"• {m.title} — {m.price_label()}\n  {m.summary}")
        fields = " ".join(f"{i.name}=…" for i in m.inputs if i.required)
        lines.append(f"  Order: /buy {m.id} {fields}".rstrip())
    lines.append("Your orders: /order <id>")
    return "\n".join(lines)


async def _buy(surface: str, buyer: str, words: List[str], db=None) -> str:
    from modules.commerce.service import OrderRefused, create_order
    if not words:
        return "Usage: /buy <offer> field=value … — /offers lists what is for sale."
    offer_id = words[0].lower()
    from core.commerce.store import live_version
    m = live_version(offer_id)
    if m is None or f"native:{surface}" not in m.listings:
        return f"{offer_id} is not for sale here. /offers lists what is."
    try:
        fields = parse_fields(words[1:])
        order, pay = await create_order(offer_id, channel=surface, buyer=buyer, inputs=fields,
                                        source=f"native:{surface}", listing=f"native:{surface}",
                                        db=db)
    except (OrderRefused, ValueError) as exc:
        return f"❌ {exc}"
    if pay is None:
        return (f"Order {order.id} received. This offer is priced per order: you will get a "
                f"payment request here once it is quoted. /order {order.id} shows it.")
    return (f"Order {order.id}: {m.title}.\n{pay['pay_text']}\n"
            f"Pay the EXACT amount; it settles on its own. /order {order.id} shows the status.")


async def _order(surface: str, buyer: str, words: List[str], container=None,
                 db=None) -> str:
    from core.commerce import orders as ords
    from modules.commerce.fulfillment import public_status
    if not words:
        mine = ords.list_orders(channel=surface, buyer=buyer, limit=5)
        if not mine:
            return "You have no orders here. /offers lists what is for sale."
        return "Your orders:\n" + "\n".join(f"• {o.id} — {o.offer_id} — {o.status}" for o in mine)
    o = ords.get(words[0])
    if o is None or o.channel != surface or o.buyer != buyer:
        return "No such order of yours."
    act = words[1].lower() if len(words) > 1 else ""
    if act == "cancel":
        from modules.commerce.service import cancel_order
        return (await cancel_order(o, db=db))[1]
    if act == "dispute":
        reason = " ".join(words[2:]).strip()[:400] or "(no reason given)"
        if not ords.dispute(o.id, reason):
            return f"Order {o.id} is {o.status}; only a delivered order can be disputed."
        from modules.commerce.service import owner_notice
        await owner_notice(container, o.user_id,
                           f"⚠️ Order {o.id} DISPUTED by the buyer: {reason}\n"
                           f"Resolve: /offer resolve {o.id} accept|refund")
        return f"Order {o.id} is disputed; the owner has been told."
    st = public_status(o)
    lines = [f"Order {o.id}: {st['status']}", f"Offer: {o.offer_id}"]
    if o.amount_usd:
        lines.append(f"Price: ${o.amount_usd}")
    if st.get("receipt"):
        r = st["receipt"]
        lines.append(f"Receipt: {r.get('sha256', '')[:16]}… proof {r.get('proof')}")
    if st.get("note"):
        lines.append(st["note"])
    return "\n".join(lines)


async def storefront_reply(container: Any, surface: str, sender: str, text: str, *,
                           db=None, now: Optional[float] = None) -> Optional[str]:
    """The reply to a storefront line, or ``None`` when *text* is not one (or the
    storefront is closed on *surface*, or the sender is over the rate limit)."""
    surface = str(surface or "").lower()
    verb = _verb(text, surface)
    if verb is None or not sender or not is_open(surface):
        return None
    if _limited(surface, str(sender), time.time() if now is None else float(now)):
        return None
    words = (text or "").strip().split()[1:]
    try:
        if verb == "/offers":
            return catalog_text(surface)
        if verb == "/buy":
            return await _buy(surface, str(sender), words, db=db)
        return await _order(surface, str(sender), words, container=container, db=db)
    except Exception:
        logger.warning("storefront: %s failed", verb, exc_info=True)
        return "Sorry — that could not be done right now. Try again later."


__all__ = ["VERBS", "catalog_text", "is_open", "parse_fields", "storefront_reply"]
