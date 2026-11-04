"""``/offer`` — the owner's offers and orders (099 W1/W4). Owner seats only.

The agent drafts an offer, provisions its resources and records a test; every
decision that grants authority is the OWNER's, on a card whose Confirm runs one
of these lines on the owner's seat (``actor="owner_seat"``):

* ``/offer setup <id> <budget_usd>`` — draft -> setup with ONE setup budget (U39);
* ``/offer live <id> <hash>`` — promote the playbook, confirm the manifest the card
  showed (the hash pins it) as the next live version, publish the listings;
* ``/offer resolve <order> accept|refund`` — a dispute or a failure (U40: a refund
  is the owner's, on the invoice).

Plain owner lines: ``/offer`` (list), ``show <id>``, ``pause|resume|retire <id>``,
``orders [id]``, ``quote <order> <usd>`` (price a quote-priced order).

Every card body is built from the VALIDATED manifest and the store — never from
text the model wrote.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, List, Optional

from core.commerce import orders as ords
from core.commerce import store as st

logger = logging.getLogger(__name__)

USAGE = ("Usage: /offer [list] | show <id> | pause|resume|retire <id> | orders [id] | "
         "quote <order> <usd> | resolve <order> accept|refund\n"
         "Say \"sell …\" in chat and I draft the offer, set it up and put it to you on a card.")


def live_card_body(o: st.Offer) -> str:
    """Everything the owner confirms, from the validated manifest + the store."""
    m = o.working()
    res = st.resources(o.id)
    lines = [f"{m.title} — {m.price_label()} ({m.pricing_kind}), pay with "
             f"{', '.join(a for a, _ in m.rails)}.",
             f"Delivers: {m.deliverable_kind} (proof: {m.proof}). Listed on: "
             f"{', '.join(m.listings)}.",
             f"Each paid order runs ONE goal with the playbook `{m.playbook}` and the tools "
             f"{', '.join(m.tools)}; it may spend at most ${m.spend_usd_per_order:g} on-chain "
             f"(LLM ≈ ${m.llm_usd_per_order:g}), only to the buyer's "
             f"`{m.role_field('recipient') or '(no recipient)'}` or these contracts:"]
    contracts = [r for r in res if r.kind == "contract"]
    lines += [f"  • {r.chain}:{r.address}" for r in contracts] or ["  • (none)"]
    caps = ", ".join(f"{k} {v}" for k, v in sorted((m.caps or {}).items()))
    lines.append(f"Limits: {caps}; deadline {m.deadline_min} min, {m.max_attempts} attempt(s); "
                 f"refunds are yours.")
    if o.test_evidence:
        ev = {k: str(v)[:120] for k, v in list(o.test_evidence.items())[:6]}
        lines.append(f"Test evidence: {ev}")
    else:
        lines.append("⚠️ No test was recorded.")
    lines.append("Confirm takes it live: buyers on those channels can order and pay, and each "
                 "paid order is fulfilled with no further tap.")
    return "\n".join(lines)


def setup_card_body(o: st.Offer, budget: float) -> str:
    m = o.working()
    return (f"{m.title}: before going live the agent sets up what it needs (contracts, "
            f"files, accounts) and tries to spend at most ${budget:g} on-chain, then records "
            f"a test and asks you to go live. The ${budget:g} is a stated limit, not "
            f"a wallet cap: each setup spend still passes the wallet's own caps and asks. "
            f"Nothing is sold until that second card.")


def _promote_playbook(playbook: str, user_id: str, skill_manager=None) -> Optional[str]:
    """Promote a pending playbook (if any) and return the sha256 of the active text."""
    from core.self_evolution import _resolve_skill_manager
    mgr = _resolve_skill_manager(skill_manager)
    if mgr is None:
        raise st.CommerceError("the skill manager is unavailable, so the playbook cannot be "
                               "checked")
    pending = {p.get("id") or p.get("skill_id") for p in (mgr.list_pending_skills(user_id) or [])}
    if playbook in pending:
        res = mgr.promote_pending_skill(playbook, user_id=user_id)
        if not getattr(res, "ok", False):
            raise st.CommerceError(f"the playbook {playbook} could not be promoted: "
                                   f"{'; '.join(getattr(res, 'errors', []) or [])}")
    text = mgr.active_skill_text(playbook, user_id=user_id)
    if not text:
        raise st.CommerceError(f"the playbook skill {playbook!r} does not exist — the agent "
                               f"must write it first")
    return hashlib.sha256(text.encode()).hexdigest()


async def _maybe_await(v):
    import inspect
    return await v if inspect.isawaitable(v) else v


async def _publish(o: st.Offer) -> List[str]:
    """Hand the confirmed version to every listing adapter that publishes."""
    from core.commerce.hooks import listing_adapters
    m = st.live_version(o.id, o.version)
    notes = []
    for kind, adapter in listing_adapters().items():
        if kind not in (m.listings if m else ()):
            continue
        try:
            ref = await _maybe_await(adapter.publish(m, offer_url=f"/api/offers/{m.id}"))
            notes.append(f"{kind}: published{f' ({ref})' if ref else ''}")
        except Exception as exc:
            logger.warning("listing %s publish failed for %s", kind, o.id, exc_info=True)
            notes.append(f"{kind}: NOT published ({exc})")
    return notes


async def _withdraw(o: st.Offer) -> None:
    from core.commerce.hooks import listing_adapters
    m = st.live_version(o.id, o.version) if o.version else None
    if m is None:
        return
    for kind, adapter in listing_adapters().items():
        if kind in m.listings:
            try:
                await _maybe_await(adapter.withdraw(m))
            except Exception:
                logger.warning("listing %s withdraw failed for %s", kind, o.id, exc_info=True)


def _list(user_id: str) -> str:
    offers = st.list_offers(user_id)
    if not offers:
        return "No offers yet. Say \"sell …\" in chat and I draft one."
    lines = ["Offers:"]
    for o in offers:
        sold = ords.sold(o.id)
        lines.append(f"• {o.id} — {o.state}{f' v{o.version}' if o.version else ''} — "
                     f"{o.manifest.get('title', '')} — {sold} sold")
    return "\n".join(lines)


def _show(user_id: str, oid: str) -> str:
    o = st.get(oid)
    if o is None or o.user_id != user_id:
        return f"No offer {oid}."
    head = f"Offer {o.id} — {o.state}" + (f", live v{o.version}" if o.version else "")
    return (f"{head}\nCard hash: {st.card_hash(o)}"
            + (f"\nSetup budget: ${o.setup_budget_usd:g} (a stated limit; the wallet's own "
               f"caps and asks bound each spend)" if o.setup_budget_usd is not None
               else "") + "\n" + live_card_body(o))


def _orders(user_id: str, oid: Optional[str]) -> str:
    rows = ords.list_orders(user_id=user_id, offer_id=oid, limit=15)
    if not rows:
        return "No orders."
    return "Orders:\n" + "\n".join(
        f"• {r.id} — {r.offer_id} — {r.status} — ${r.amount_usd or '?'} — {r.channel}"
        + (f" — {r.error}" if r.error else "") for r in rows)


async def offer_reply(user_id: Optional[str], args: List[str], *, container: Any = None,
                      db=None, skill_manager=None) -> str:
    """The ``/offer`` answer. ``user_id`` is the owner (the seat ran its own gate)."""
    if not user_id:
        return "Only the owner can run /offer."
    words = [str(a) for a in (args or []) if str(a).strip()]
    sub = words[0].lower() if words else "list"
    rest = words[1:]
    try:
        if sub == "list":
            return _list(user_id)
        if sub == "show" and rest:
            return _show(user_id, rest[0].lower())
        if sub == "orders":
            return _orders(user_id, rest[0].lower() if rest else None)
        if sub == "setup" and len(rest) >= 2:
            o = st.start_setup(rest[0].lower(), user_id, budget_usd=float(rest[1].lstrip("$")),
                               actor=st.OWNER_SEAT)
            return (f"✅ Offer {o.id} is in SETUP with a ${o.setup_budget_usd:g} setup budget "
                    f"(a stated limit; the wallet's own caps and asks bound each spend). The "
                    f"agent provisions it and asks you to go live.")
        if sub == "live" and len(rest) >= 2:
            cur = st.get(rest[0].lower())
            if cur is None or cur.user_id != user_id:
                return f"No offer {rest[0]}."
            sha = _promote_playbook(cur.working().playbook, user_id, skill_manager)
            o = st.go_live(cur.id, user_id, actor=st.OWNER_SEAT, expect_hash=rest[1],
                           playbook_sha=sha)
            notes = await _publish(o)
            return (f"✅ Offer {o.id} is LIVE (v{o.version})."
                    + (" " + "; ".join(notes) if notes else ""))
        if sub in ("pause", "resume", "retire") and rest:
            to = {"pause": st.PAUSED, "resume": st.LIVE, "retire": st.RETIRED}[sub]
            o = st.set_state(rest[0].lower(), user_id, to, actor=st.OWNER_SEAT)
            if to != st.LIVE:
                await _withdraw(o)
            else:
                await _publish(o)
            return f"✅ Offer {o.id} is {o.state.upper()}."
        if sub == "quote" and len(rest) >= 2:
            from modules.commerce.service import OrderRefused, quote_order, tell_buyer
            try:
                order, pay = await quote_order(rest[0], rest[1].lstrip("$"), user_id=user_id,
                                               db=db)
            except OrderRefused as exc:
                return f"❌ {exc}"
            sent = await tell_buyer(container, order, f"Your order {order.id} is quoted: "
                                                      f"{pay['pay_text']}")
            if sent:
                return (f"✅ Order {order.id} quoted at ${order.amount_usd}; the buyer was "
                        f"sent the request.")
            return (f"✅ Order {order.id} quoted at ${order.amount_usd}. The buyer was NOT "
                    f"sent it from here: they read the payment request with /order "
                    f"{order.id} (or their order status on {order.channel}).")
        if sub == "resolve" and len(rest) >= 2:
            return await _resolve(user_id, rest[0], rest[1].lower(), container)
    except (st.CommerceError, ords.OrderError, ValueError) as exc:
        return f"❌ {exc}"
    except st.CommerceUnreadable as exc:
        return (f"❌ The commerce store could not be read ({exc}). Nothing was changed — this "
                f"is not the same as \"no offers\".")
    return USAGE


async def _resolve(user_id: str, order_id: str, how: str, container) -> str:
    from modules.commerce.service import tell_buyer
    o = ords.get(order_id)
    if o is None or o.user_id != user_id:
        return f"No order {order_id}."
    if how == "accept":
        if not ords.accept(o.id):
            return f"Order {o.id} is {o.status}; only a delivered or disputed order is accepted."
        await tell_buyer(container, o, f"Order {o.id} is closed as delivered.")
        return f"✅ Order {o.id} accepted."
    if how == "refund":
        if not ords.fail(o.id, "owner: refund"):
            return f"Order {o.id} is {o.status}; it cannot be refunded from here."
        await tell_buyer(container, o, f"Order {o.id} will be refunded by the owner.")
        refund_to = None
        try:
            from core.commerce.store import live_version
            m = live_version(o.offer_id, o.offer_version)
            f = m.role_field("refund") if m else None
            refund_to = (o.inputs or {}).get(f) if f else None
        except Exception:
            refund_to = None
        return (f"✅ Order {o.id} is FAILED. Refund ${o.amount_usd} of invoice {o.request_id} "
                + (f"to the buyer's refund address {refund_to}" if refund_to
                   else "— the buyer gave no refund address; ask them on their channel")
                + " (never to the sending address: it is often an exchange).")
    return "Usage: /offer resolve <order> accept|refund"


__all__ = ["USAGE", "live_card_body", "offer_reply", "setup_card_body"]
