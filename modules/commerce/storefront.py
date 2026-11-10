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

**Public threads (109).** :func:`thread_intake` reads replies under the agent's own
announcement posts on a surface whose pack registers a thread channel
(``core.commerce.hooks.thread_channel``) and answers ``/buy`` and ``/order`` lines
IN that thread, by the same code. The buyer is the platform-authenticated author;
one post makes at most one order; an edited post, a post older than its
announcement or a reply under any other root is ignored; a reply is written only
when the surface's write share allows it, else the post waits for the next poll.
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


async def _buy(surface: str, buyer: str, words: List[str], db=None, *,
               thread_ref: Optional[str] = None) -> str:
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
        order, pay = await create_order(
            offer_id, channel=surface, buyer=buyer, inputs=fields,
            source=f"native:{surface}:post:{thread_ref}" if thread_ref else f"native:{surface}",
            listing=f"native:{surface}", db=db, thread_ref=thread_ref)
    except (OrderRefused, ValueError) as exc:
        return f"❌ {exc}"
    if thread_ref:
        return thread_payment_text(order, m, pay)
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


# -- public threads (109) ------------------------------------------------------------

#: Posts handled per poll; the rest wait for the next tick.
THREAD_MAX_PER_POLL = 10
_THREAD_VERBS = ("/buy", "/order")


def thread_line(text: str) -> Optional[Tuple[str, List[str]]]:
    """``(verb, words)`` of a thread post, after the platform's leading ``@mentions``;
    ``None`` for anything else (it is never answered)."""
    words = (text or "").strip().split()
    while words and words[0].startswith("@"):
        words.pop(0)
    if not words:
        return None
    head = words[0].lower()
    return (head, words[1:]) if head in _THREAD_VERBS else None


def _human_amount(pay: Dict[str, Any]) -> str:
    from decimal import Decimal
    from core.payments.assets import resolve
    a = resolve(pay.get("asset"))
    try:
        if a is not None and pay.get("amount_raw"):
            amt = Decimal(int(pay["amount_raw"])) / (Decimal(10) ** a.decimals)
            return f"{amt.normalize():f} {a.symbol}"
    except Exception:
        pass
    return f"${pay.get('amount_usd')} in {pay.get('asset')}"


def thread_payment_text(order: Any, m: Any, pay: Optional[Dict[str, Any]]) -> str:
    """The in-thread answer to an order: everything needed to pay, nothing else."""
    if pay is None:
        return (f"Order {order.id}: {m.title} is priced per order; the price comes here. "
                f"/order {order.id} shows it.")
    import time as _t
    exp = pay.get("expires_at")
    when = _t.strftime("%H:%M UTC", _t.gmtime(float(exp))) if exp else "soon"
    return (f"Order {order.id}: pay exactly {_human_amount(pay)} on {pay.get('chain')} to "
            f"{pay.get('pay_to')} before {when}. Pay only to an address in a reply from "
            f"this account. /order {order.id} shows the status.")


def _roots(surface: str, path: Optional[str]) -> Dict[str, Tuple[str, float]]:
    """``{root post: (offer id, announced at)}`` of LIVE offers listed on *surface*."""
    from core.commerce import thread
    from core.commerce.store import live_offers
    live = {m.id for m in live_offers(listing=f"native:{surface}")}
    return {r["root_id"]: (r["offer_id"], float(r["created_at"] or 0))
            for r in thread.threads(surface=surface, path=path) if r["offer_id"] in live}


async def _answer_post(container: Any, ch: Any, post: Any, offer_id: str, verb: str,
                       words: List[str], db=None) -> None:
    from core.commerce import orders as ords
    surface = post.surface
    if verb == "/buy":
        if words and "=" not in words[0]:
            words = words[1:]           # "/buy <offer> …": the root names the offer
        if ords.by_thread(surface, post.post_id) is not None:
            return                      # already answered (a re-read post)
        try:
            text = await _buy(surface, post.author_id, [offer_id] + words, db=db,
                              thread_ref=post.post_id)
        except Exception:
            made = ords.by_thread(surface, post.post_id)
            if made is not None:        # the order exists: its answer must still go out
                ords.set_notice_pending(made.id, f"Order {made.id} is received. /order "
                                                 f"{made.id} shows how to pay.")
            raise
    else:
        text = await _order(surface, post.author_id, words, container=container, db=db)
    try:
        from core.commerce.store import owner_of
        ok = bool(await ch.reply(post.post_id, text, subject=post.post_id,
                                 user_id=owner_of(offer_id) or ""))
    except Exception:
        logger.warning("storefront: reply under %s failed", post.post_id, exc_info=True)
        ok = False
    if not ok:
        made = ords.by_thread(surface, post.post_id)
        if made is not None:
            ords.set_notice_pending(made.id, text)


async def thread_intake(container: Any, surface: str, *, db=None,
                        path: Optional[str] = None, now: Optional[float] = None) -> int:
    """Read and answer the new replies under *surface*'s announcements. Returns the
    number answered. Closed (and no platform read) unless a live offer lists the
    surface AND has an announcement. Never raises."""
    from core.commerce import thread
    from core.commerce.hooks import thread_channel
    ch = thread_channel(surface)
    if ch is None or not is_open(surface):
        return 0
    try:
        roots = _roots(surface, path)
        if not roots:
            return 0
        cur = thread.cursor(surface, path=path)
        since = cur["since_id"] if cur else None
        try:
            posts, newest = await ch.poll(since)
        except thread.ThreadUnavailable as exc:
            thread.save_cursor(surface, None, stop_reason=str(exc), path=path, now=now)
            return 0
        me = ch.agent_id()
        handled, answered, stop = since, 0, ""
        for post in posts or ():
            if answered >= THREAD_MAX_PER_POLL:
                stop = "more replies wait for the next poll"
                break
            root = roots.get(post.root_id)
            line = thread_line(post.text)
            if (root is None or line is None or post.edited or not post.author_id
                    or post.author_id == me or post.post_id == post.root_id
                    or (post.created_at and post.created_at < root[1])):
                handled = post.post_id
                continue
            if not ch.can_reply():
                stop = "the write share for this hour is used; replies wait"
                break
            if _limited(surface, post.author_id, time.time() if now is None else float(now)):
                handled = post.post_id
                continue
            try:
                await _answer_post(container, ch, post, root[0], line[0], line[1], db=db)
            except Exception:
                # One bad post never blocks the cursor behind it (each re-read costs
                # API credit). A made order keeps its answer for the notice retry.
                logger.warning("storefront: %s post %s failed", surface, post.post_id,
                               exc_info=True)
            handled, answered = post.post_id, answered + 1
        else:
            handled = newest or handled
        thread.save_cursor(surface, handled, stop_reason=stop, path=path, now=now)
        return answered
    except Exception:
        logger.warning("storefront: %s thread intake failed", surface, exc_info=True)
        return 0


__all__ = ["THREAD_MAX_PER_POLL", "VERBS", "catalog_text", "is_open", "parse_fields",
           "storefront_reply", "thread_intake", "thread_line", "thread_payment_text"]
