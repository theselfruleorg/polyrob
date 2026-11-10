"""The order service (099 W4): one order, end to end, on the ONE invoice store.

::

    create_order ─ invoice (kind "offer_order") ─ buyer pays ─ settlement watcher
      └─ on_settled: order paid ─ start_fulfillment (the registered fulfiller
         writes ONE goal; the order row is the idempotency key)
           └─ the goal run ends in order_deliver ─ deliver(): proof checked,
              receipt written, buyer told
    tick(): stale claims, overdue runs (retry ≤ max_attempts, then failed + owner
            notice), acceptance windows, paid orders nobody started.

Rules held here:

* an order exists only for a LIVE offer, at the confirmed version's price, inside
  the offer's caps (per day, open per buyer, supply);
* ``paid`` only from a COMPLETED invoice — a test-network settlement never pays
  an order (the watcher returns before it reaches us);
* a failed order is the owner's to refund (U40); nothing here moves money out.

Async functions take the invoice ``db`` the watcher uses; store paths default to
the data home.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from core.commerce import orders as ords
from core.commerce import store as st
from core.commerce.offer import OfferError, OfferManifest, validate_inputs

logger = logging.getLogger(__name__)

ORDER_KIND = "offer_order"
#: How long an order's invoice stays payable.
INVOICE_HOURS = 2.0
DAY_S = 86400.0
#: A quote-priced order the owner never prices expires after this long.
QUOTE_TTL_S = 7 * DAY_S
#: After its invoice's window plus this, an unpaid order with no payable invoice expires.
UNPAID_GRACE_S = 600.0
#: Unpaid orders one offer takes from anonymous machine buyers (HTTP, MCP, A2A, a
#: marketplace) at once: past it the next machine order refuses, so same-price pending
#: invoices cannot use up the unique-amount space that chat buyers need too.
MAX_UNPAID_MACHINE_ORDERS = 25


class OrderRefused(ValueError):
    """An order the rules refuse; the message is safe to show the buyer."""


def _live(offer_id: str, path: Optional[str]) -> Tuple[st.Offer, OfferManifest]:
    o = st.get(offer_id, path=path)
    if o is None or o.state != st.LIVE or not o.version:
        raise OrderRefused(f"offer {offer_id!r} is not for sale")
    m = st.live_version(o.id, o.version, path=path)
    if m is None:
        raise OrderRefused(f"offer {offer_id!r} is not for sale")
    return o, m


def _check_caps(m: OfferManifest, channel: str, buyer: str, path: Optional[str],
                now: float) -> None:
    caps = dict(m.caps or {})
    # Supply counts only orders that took money: an unpaid order holds nothing, so
    # anonymous unpaid orders cannot show "sold out". `on_settled` checks again
    # when a payment lands (by payment time), so a race can never oversell.
    if caps.get("supply") and ords.sold(m.id, path=path) >= int(caps["supply"]):
        raise OrderRefused(f"{m.title} is sold out")
    if caps.get("per_day") and ords.paid_since(m.id, now - DAY_S, path=path) >= int(
            caps["per_day"]):
        raise OrderRefused(f"{m.title} has reached today's order limit; try tomorrow")
    if caps.get("open_per_buyer") and ords.open_for_buyer(
            m.id, channel, buyer, path=path) >= int(caps["open_per_buyer"]):
        raise OrderRefused("you already have an open order for this offer — finish or "
                           "cancel it first")


def _rail(m: OfferManifest, asset: Optional[str]) -> Tuple[str, str]:
    if asset:
        for a, mode in m.rails:
            if a == str(asset).lower():
                return a, mode
        raise OrderRefused(f"this offer is not payable in {asset}")
    return m.rails[0]


async def _invoice(order: ords.Order, m: OfferManifest, amount: str, asset: str, *,
                   db=None, path: Optional[str] = None,
                   cancel_on_fail: bool = True) -> Dict[str, Any]:
    from core.payments.assets import resolve
    from modules.x402 import invoicing
    a = resolve(asset)
    if a is None:
        raise OrderRefused(f"payment asset {asset} is not configured here")
    try:
        inv = await invoicing.create_payment_request(
            user_id=order.user_id, session_id="", amount_usd=float(Decimal(amount)),
            purpose=f"Order {order.id}: {m.title}"[:200], expiry_hours=INVOICE_HOURS,
            chain=a.chain, asset_id=a.asset_id, kind=ORDER_KIND,
            extra_metadata={"order_id": order.id, "offer_id": m.id}, db=db)
    except Exception as exc:
        # Any failure, not only a refused amount: an order left `awaiting_payment`
        # with no invoice could never be paid and would hold its buyer's slot.
        logger.warning("commerce: no invoice for order %s", order.id, exc_info=True)
        if cancel_on_fail:
            ords.cancel(order.id, path=path)
        raise OrderRefused(f"the payment request could not be made ({exc})") from None
    try:
        ords.attach_invoice(order.id, inv["request_id"], amount, path=path)
    except ords.OrderError as exc:
        # The order moved (a cancel raced the invoice): never leave its invoice payable.
        await _close_invoice(inv["request_id"], db=db)
        raise OrderRefused(str(exc)) from None
    return inv


async def _close_invoice(request_id: str, *, db=None) -> Optional[str]:
    """Close an order's UNPAID invoice (``pending -> expired``), so nobody can pay an
    order that is no longer for sale. The wake is marked delivered: the buyer was
    already told the order is cancelled, so no "expired unpaid" notice follows.

    Returns ``None`` when nothing is payable any more, else the invoice status that
    blocks the close (``settling`` = a payment is in flight, ``completed``…), or
    ``"unreadable"``."""
    from core.state.sql import acas_transition
    from modules.x402._db import resolve_db
    from modules.x402.invoice_status import X402_INVOICE, sql_now
    try:
        database = await resolve_db(db)
    except Exception:
        logger.warning("commerce: the invoice store is unavailable", exc_info=True)
        database = None
    if database is None:
        return "unreadable"
    if await acas_transition(database, "x402_payment_requests", key="id = ?",
                             key_params=(str(request_id),), machine=X402_INVOICE,
                             to="expired", from_states=("pending",),
                             set_cols={"updated_at": sql_now()}, json_col="metadata",
                             json_set={"wake_delivered": True, "wake_state": "delivered"}):
        return None
    row = await database.fetch_one("SELECT status FROM x402_payment_requests WHERE id = ?",
                                   (str(request_id),))
    if row is None or row["status"] == "expired":
        return None
    return str(row["status"])


async def cancel_order(order: ords.Order, *, db=None,
                       path: Optional[str] = None) -> Tuple[bool, str]:
    """The buyer cancels an UNPAID order: its invoice is closed first, so a payment
    can no longer arrive for a cancelled order. ``(cancelled, buyer-safe text)``."""
    if order.status not in (ords.QUOTED, ords.AWAITING):
        return False, (f"Order {order.id} is {order.status}; only an unpaid order can be "
                       f"cancelled.")
    if order.request_id:
        blocking = await _close_invoice(order.request_id, db=db)
        if blocking == "unreadable":
            return False, (f"Order {order.id} was not cancelled: its payment request could "
                           f"not be read. Nothing changed; try again later.")
        if blocking:
            return False, (f"Order {order.id} cannot be cancelled now: its payment is "
                           f"{blocking}. /order {order.id} shows the status.")
    if ords.cancel(order.id, path=path):
        return True, (f"Order {order.id} cancelled. Its payment request is closed — do not "
                      f"pay it.")
    cur = ords.get(order.id, path=path)
    return False, (f"Order {order.id} is {getattr(cur, 'status', 'gone')}; only an unpaid "
                   f"order can be cancelled.")


async def close_open_orders(offer_id: str, *, container=None, db=None,
                            path: Optional[str] = None) -> Tuple[int, int]:
    """A RETIRED offer sells nothing more: cancel every unpaid (``quoted`` /
    ``awaiting_payment``) order through :func:`cancel_order` (its invoice closed first)
    and tell its buyer. Paid orders are still fulfilled. ``(closed, not closed)``; one
    failure never stops the others."""
    closed = failed = 0
    for o in ords.list_orders(offer_id=offer_id, statuses=[ords.QUOTED, ords.AWAITING],
                              limit=10000, path=path):
        try:
            ok, _text = await cancel_order(o, db=db, path=path)
        except Exception:
            logger.warning("commerce: order %s of retired offer %s not cancelled", o.id,
                           offer_id, exc_info=True)
            ok = False
        if not ok:
            failed += 1
            continue
        closed += 1
        await tell_buyer(container, ords.get(o.id, path=path) or o, (
            f"Order {o.id} is cancelled: this offer was retired. Its payment request is "
            f"closed — do not pay it. Nothing was charged."))
    return closed, failed


def payment_view(inv: Dict[str, Any]) -> Dict[str, Any]:
    """What a buyer needs to pay: exact raw amount, token, chain, recipient, text."""
    from modules.x402.artifact import build_payment_artifact
    art = build_payment_artifact(inv)
    return {"request_id": inv.get("request_id"), "amount_usd": inv.get("amount_usd"),
            "asset": inv.get("asset_id"), "asset_address": inv.get("asset_address"),
            "amount_raw": str(inv.get("amount_raw") or ""), "chain": inv.get("chain"),
            "pay_to": inv.get("recipient"), "expires_at": inv.get("expires_at_epoch"),
            "pay_text": art.get("pay_text"), "pay_uri": art.get("pay_uri"),
            "x402_url": f"/api/x402/requests/{inv.get('request_id')}"}


async def create_order(offer_id: str, *, channel: str, buyer: str, inputs: Dict[str, Any],
                       asset: Optional[str] = None, source: Optional[str] = None, db=None,
                       listing: Optional[str] = None, path: Optional[str] = None,
                       now: Optional[float] = None,
                       thread_ref: Optional[str] = None) -> Tuple[ords.Order, Optional[Dict]]:
    """A buyer orders. Returns ``(order, payment view or None)`` — ``None`` for a
    quote-priced offer, whose price the owner sets with ``/offer quote``.
    Raises :class:`OrderRefused` with a buyer-safe sentence."""
    ts = time.time() if now is None else float(now)
    o, m = _live(str(offer_id).lower(), path)
    if listing and listing not in m.listings:
        raise OrderRefused(f"offer {m.id!r} is not sold here")
    channel = str(channel or "").strip().lower()
    from core.surfaces.catalog import forgeable_ids
    if channel in forgeable_ids() or channel.startswith("email"):
        raise OrderRefused("orders are not taken on a channel whose sender can be forged "
                           "(email)")
    buyer = str(buyer or "").strip()
    if not buyer:
        raise OrderRefused("an order needs a buyer reference")
    from core.commerce.offer import native_surfaces
    if (m.deliverable_kind == "file" and m.proof != "url"
            and (thread_ref or channel not in native_surfaces())):
        # A file reaches a buyer only as a chat attachment; a public thread or a machine
        # buyer would get a receipt and never the file.
        raise OrderRefused(f"{m.title} delivers a file, which only a direct chat can receive; "
                           f"order it in a direct message")
    try:
        clean = validate_inputs(m, inputs or {})
    except OfferError as exc:
        raise OrderRefused(str(exc)) from None
    _check_caps(m, channel, buyer, path, ts)
    if (not thread_ref and channel not in native_surfaces()
            and ords.count_unpaid(m.id, exclude_channels=native_surfaces(),
                                  path=path) >= MAX_UNPAID_MACHINE_ORDERS):
        raise OrderRefused(f"{m.title} has too many unpaid orders right now; try again later")
    asset_id, _mode = _rail(m, asset)
    order = ords.create(offer_id=m.id, offer_version=o.version, user_id=o.user_id,
                        channel=channel, buyer=buyer, inputs=clean, amount_usd=m.amount_usd,
                        spend_budget=m.spend_usd_per_order, source=source, path=path, now=ts,
                        thread_ref=thread_ref, asset=asset_id)
    if m.amount_usd is None:
        return order, None
    inv = await _invoice(order, m, m.amount_usd, asset_id, db=db, path=path)
    return ords.get(order.id, path=path), payment_view(inv)


async def quote_order(order_id: str, amount_usd: str, *, user_id: str, db=None,
                      path: Optional[str] = None) -> Tuple[ords.Order, Dict]:
    """The owner prices a quoted order; the buyer gets a payment request."""
    order = ords.get(order_id, path=path)
    if order is None or order.user_id != str(user_id):
        raise OrderRefused(f"no order {order_id}")
    if order.status != ords.QUOTED:
        raise OrderRefused(f"order {order_id} is {order.status}, not waiting for a quote")
    m = st.live_version(order.offer_id, order.offer_version, path=path)
    try:
        amt = Decimal(str(amount_usd)).quantize(Decimal("0.01"))
    except Exception:
        raise OrderRefused("the quote must be a dollar amount") from None
    if not Decimal("0.01") <= amt <= Decimal("10000"):
        raise OrderRefused("the quote must be between $0.01 and $10000")
    # A refused invoice keeps the order QUOTED (the owner may quote again); it is
    # never cancelled behind the buyer's back.
    # The rail the buyer chose when ordering (validated against this version then).
    asset = order.asset if order.asset in {a for a, _ in m.rails} else m.rails[0][0]
    inv = await _invoice(order, m, str(amt), asset, db=db, path=path, cancel_on_fail=False)
    return ords.get(order.id, path=path), payment_view(inv)


# -- settlement side (called by the settlement watcher) ------------------------------

async def on_settled(inv: Dict[str, Any], *, container=None, path: Optional[str] = None,
                     db=None) -> Optional[ords.Order]:
    """An ``offer_order`` invoice COMPLETED: pay the order and start fulfillment. An
    order invoice has no other settled state: ``X402_INVOICE`` has no edge into
    ``settled_no_tx``, only a machine payment's INSERT starts there, and an owner
    ``/settle`` completes it. A payment for an order that is no longer open (cancelled,
    expired) is money held against nothing: the owner is told to refund it."""
    order = ords.mark_paid(str(inv.get("request_id") or ""),
                           payment_tx=inv.get("transaction_hash"), path=path)
    if order is None:
        prior = ords.by_request(str(inv.get("request_id") or ""), path=path)
        if prior is None:
            await owner_notice(container, str(inv.get("user_id") or ""),
                               f"Invoice {inv.get('request_id')} was paid for an order this "
                               f"agent cannot find. The money is held; check /offer orders.")
        elif prior.status in (ords.CANCELLED, ords.EXPIRED):
            marked = await mark_refund_due(prior, db=db)
            await owner_notice(container, prior.user_id, (
                f"⚠️ Invoice {inv.get('request_id')} was PAID for order {prior.id}, which is "
                f"{prior.status.upper()}. Nothing is fulfilled and the money is held "
                f"({_refund_due_note(marked)}): {_refund_text(prior)}."))
            await tell_buyer(container, prior, (
                f"A payment arrived for order {prior.id}, which is {prior.status}. Nothing "
                f"will be delivered; the owner has been told and handles the refund."))
        return prior
    over = _over_cap_when_paid(order, path)
    if over:
        # More payments landed than the offer's caps allow: this one is never
        # fulfilled; it is the owner's to refund (U40), and both sides are told.
        ords.fail(order.id, f"{over} when the payment arrived: refund", path=path)
        failed = ords.get(order.id, path=path)
        await _failed_notices(container, failed, f"{over} when the payment arrived", db=db)
        return failed
    await tell_buyer(container, order, f"Payment received for order {order.id}. Working on "
                                       f"it now; /order {order.id} shows the status.")
    await start_fulfillment(order, container=container, path=path, notify_missing=True)
    return ords.get(order.id, path=path)


def _over_cap_when_paid(order: ords.Order, path: Optional[str]) -> Optional[str]:
    """Which cap this just-paid order is over (``None`` = inside them), ranked by
    payment time: the first payments the cap allows keep their place."""
    m = st.live_version(order.offer_id, order.offer_version, path=path)
    caps = dict(getattr(m, "caps", None) or {})
    if caps.get("supply") and ords.paid_before(order, path=path) >= int(caps["supply"]):
        return f"sold out (supply {int(caps['supply'])})"
    if caps.get("per_day") and ords.paid_before(
            order, since=float(order.paid_at or 0) - DAY_S, path=path) >= int(caps["per_day"]):
        return f"over the offer's {int(caps['per_day'])} orders a day"
    return None


async def on_expired(inv: Dict[str, Any], *, container=None,
                     path: Optional[str] = None) -> None:
    if ords.mark_expired(str(inv.get("request_id") or ""), path=path):
        order = ords.by_request(str(inv.get("request_id") or ""), path=path)
        if order is not None:
            await tell_buyer(container, order, f"Order {order.id} expired unpaid. Nothing "
                                               f"was charged.")


async def start_fulfillment(order: ords.Order, *, container=None,
                            path: Optional[str] = None, token: Optional[str] = None,
                            notify_missing: bool = False) -> bool:
    """Claim the order and hand it to the registered fulfiller (one goal)."""
    from core.commerce.hooks import fulfiller
    fn = fulfiller()
    if fn is None:
        # Not claimed: the order stays `paid` and the next process that has a
        # fulfiller (the autonomy runtime with goals on) starts it.
        if notify_missing:
            await owner_notice(container, order.user_id,
                               f"Order {order.id} is PAID but this process runs no "
                               f"fulfillment (goals off?). It starts when goals run.")
        return False
    m = st.live_version(order.offer_id, order.offer_version, path=path)
    if m is None:
        return False
    token = token or ords.claim_fulfillment(order.id, path=path)
    if not token:
        return False
    try:
        goal_id = fn(ords.get(order.id, path=path), m, token)
    except Exception as exc:
        logger.warning("commerce: the fulfiller refused order %s", order.id, exc_info=True)
        await owner_notice(container, order.user_id,
                           f"Order {order.id} is PAID but its fulfillment goal could not be "
                           f"written ({exc}). It is retried in a few minutes, up to "
                           f"{m.max_attempts} attempt(s); then the order fails and you "
                           f"refund it.")
        return False
    if goal_id:
        ords.set_goal(order.id, token, str(goal_id), path=path)
    return bool(goal_id)


# -- delivery (called by the order_deliver action) ------------------------------------

def check_proof(m: OfferManifest, proof: Dict[str, Any]) -> Optional[str]:
    import re
    p = dict(proof or {})
    if m.proof == "tx_receipt":
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", str(p.get("tx_hash") or "")):
            return "proof.tx_hash must be the 0x + 64 hex hash of the delivering transaction"
    elif m.proof == "file_hash":
        if not re.fullmatch(r"[0-9a-f]{64}", str(p.get("sha256") or "").lower()):
            return "proof.sha256 must be the file's sha256"
    elif m.proof == "url":
        if not re.fullmatch(r"https://\S+", str(p.get("url") or "")):
            return "proof.url must be an https URL"
    return None


async def deliver(order_id: str, proof: Dict[str, Any], *, user_id: str, container=None,
                  message: str = "", path: Optional[str] = None,
                  now: Optional[float] = None) -> ords.Order:
    order = ords.get(order_id, path=path)
    if order is None or order.user_id != str(user_id):
        raise OrderRefused(f"no order {order_id}")
    m = st.live_version(order.offer_id, order.offer_version, path=path)
    why = check_proof(m, proof)
    if why:
        raise OrderRefused(why)
    if m.proof == "tx_receipt":
        why = await _check_receipt(order, str(proof.get("tx_hash")), path)
        if why:
            raise OrderRefused(why)
    try:
        done = ords.deliver(order.id, proof, accept_now=m.acceptance_days == 0,
                            path=path, now=now)
    except ords.OrderError as exc:
        raise OrderRefused(str(exc)) from None
    link = _proof_link(done, proof, path)
    # A public thread gets only code-composed words: the run's free-form message is
    # model text that read buyer data, and nothing the model writes is posted under
    # the owner's account without the owner (it goes to the owner instead).
    public = bool(done.thread_ref)
    if public and message.strip():
        await owner_notice(container, done.user_id,
                           f"Order {done.id} delivered; the run's note (not posted publicly): "
                           f"{message.strip()[:500]}")
    text = (f"Order {done.id} is delivered. "
            + (message.strip()[:1500] + " " if message and not public else "")
            + (f"Proof: {link} " if link else "")
            + f"Receipt {done.receipt.get('sha256', '')[:16]}…; /order {done.id} shows it.")
    if m.acceptance_days:
        text += (f" If something is wrong, reply /order {done.id} dispute <reason> within "
                 f"{m.acceptance_days} day(s).")
    await tell_buyer(container, done, text, media=proof.get("media_paths"))
    return done


def _rpc_call(chain: str, method: str, params: list):
    """The chain's pinned RPC (a seam the tests replace)."""
    from core.wallet.receipt_resolver import _default_call
    return _default_call(chain, method, params)


async def _check_receipt(order: ords.Order, tx_hash: str, path: Optional[str]) -> Optional[str]:
    """109 W5: a tx proof must be backed by the chain, for THIS order:

    * a SUCCESSFUL transaction, sent by the agent's own wallet, to one of the
      order's pinned contract resources on that resource's chain;
    * when the offer has a recipient, the pinned contract emitted an event naming the
      order's recipient (a mint/transfer to them), so a public tx of someone else
      on the same contract does not pass;
    * the tx delivers no other order.

    An offer with no contract resource has no chain to read: nothing is checked.
    A receipt that cannot be read refuses (the run calls again), never passes."""
    import asyncio
    from core.wallet import chains
    other = ords.proof_used_by(tx_hash, except_id=order.id, path=path)
    if other:
        return f"{tx_hash} already delivered order {other}; one transaction delivers one order"
    contracts = [r for r in st.resources(order.offer_id, kind="contract", path=path)
                 if st.pinned_contract(order.offer_id, order.offer_version, str(r.chain),
                                       str(r.address), path=path) is not None]
    if not contracts:
        return None
    m = st.live_version(order.offer_id, order.offer_version, path=path)
    field = m.role_field("recipient") if m else None
    recipient = str((order.inputs or {}).get(field) or "").lower() if field else ""
    try:
        from core.wallet.factory import get_agent_wallet
        me = str(getattr(get_agent_wallet(), "address", "") or "").lower()
    except Exception:
        me = ""
    if not me:
        return "the agent wallet could not be read to check the delivery; try again"
    last = ""
    for chain in dict.fromkeys(str(r.chain) for r in contracts):
        pinned = {str(r.address).lower() for r in contracts if str(r.chain) == chain}
        row = chains.get(chain)
        try:
            if row is None or row.family != "evm":
                raise ValueError("not an EVM chain")
            cid = await asyncio.to_thread(_rpc_call, chain, "eth_chainId", [])
            if int(str(cid), 16) != row.chain_id:
                raise ValueError("the RPC answers for another chain")
            rec = await asyncio.to_thread(_rpc_call, chain, "eth_getTransactionReceipt",
                                          [tx_hash])
        except Exception as exc:
            return (f"the delivery transaction could not be read on {chain} "
                    f"({str(exc)[:120]}); call order_deliver again in a minute")
        if not rec:
            last = (f"{tx_hash} has no receipt on {chain} yet; wait for it to be mined and "
                    f"call order_deliver again")
            continue
        if str(rec.get("transactionHash") or "").lower() != tx_hash.lower():
            return "the receipt read back is for another transaction"
        if str(rec.get("status")) not in ("0x1", "1"):
            return f"{tx_hash} FAILED on {chain} (status {rec.get('status')}): nothing was delivered"
        if str(rec.get("to") or "").lower() not in pinned:
            return f"{tx_hash} is not a call to this offer's pinned contract on {chain}"
        if str(rec.get("from") or "").lower() != me:
            return f"{tx_hash} was not sent by this agent's wallet"
        if recipient:
            word = "0x" + recipient[2:].rjust(64, "0")
            named = any(str(lg.get("address") or "").lower() in pinned
                        and word in [str(t).lower() for t in (lg.get("topics") or [])[1:]]
                        for lg in rec.get("logs") or ())
            if not named:
                return (f"{tx_hash} emitted no event to the order's recipient {recipient}; "
                        f"nothing reached the buyer")
        return None
    return last or f"{tx_hash} was not found on the offer's chain"


def _proof_link(order: ords.Order, proof: Dict[str, Any], path: Optional[str]) -> str:
    """An explorer link for a tx proof on the chain of the offer's contract resource
    (or a url proof itself); "" when none is known. Generic per chain."""
    if proof.get("url"):
        return str(proof["url"])[:300]
    tx = str(proof.get("tx_hash") or "")
    if not tx:
        return ""
    try:
        from core.wallet.chains import explorer_url
        for r in st.resources(order.offer_id, kind="contract", path=path):
            url = explorer_url(str(r.chain or ""), "tx", tx)
            if url:
                return url
    except Exception:
        logger.debug("commerce: no explorer link for %s", order.id, exc_info=True)
    return tx


async def fail(order_id: str, reason: str, *, user_id: str, container=None,
               path: Optional[str] = None) -> ords.Order:
    order = ords.get(order_id, path=path)
    if order is None or order.user_id != str(user_id):
        raise OrderRefused(f"no order {order_id}")
    m = st.live_version(order.offer_id, order.offer_version, path=path)
    if order.status == ords.FULFILLING and order.attempts < (m.max_attempts if m else 1):
        ords.release_for_retry(order.id, path=path)
        await owner_notice(container, order.user_id,
                           f"Order {order.id} attempt {order.attempts} failed ({reason[:200]}); "
                           f"it is retried.")
        current = ords.get(order.id, path=path)
    elif ords.fail(order.id, reason, path=path):
        current = ords.get(order.id, path=path)
        await _failed_notices(container, current, reason)
    else:
        # Delivered, accepted, unpaid or already failed: nothing failed now, so
        # nobody is told "refund" and no goal is stopped.
        return ords.get(order.id, path=path)
    # Last: this can be the order's own run, and a cancel lands on its next await.
    _stop_goal(order.goal_id)
    return current


def _stop_goal(goal_id: Optional[str]) -> None:
    """Stop the goal an order just took back. It can no longer spend, deliver or fail
    the order, but it would run its other tools until it ended. Never raises."""
    if not goal_id or str(goal_id).startswith("claim:"):
        return                     # only a claim token: no goal was ever written
    from core.commerce.hooks import goal_canceller
    fn = goal_canceller()
    if fn is None:
        logger.warning("commerce: goal %s is left running; this process wires no canceller",
                       goal_id)
        return
    try:
        fn(str(goal_id))
    except Exception:
        logger.warning("commerce: goal %s could not be stopped", goal_id, exc_info=True)


async def mark_refund_due(order: Optional[ords.Order], *, db=None) -> bool:
    """The money of a paid order that will not be delivered is OWED BACK: its invoice
    moves ``completed -> refund_due`` (never income; listed by ``/invoices
    refund_due``, closed by ``/invoices refunded``). Idempotent. Never raises."""
    if order is None or not order.request_id:
        return False
    from core.state.sql import acas_transition
    from modules.x402._db import resolve_db
    from modules.x402.invoice_status import X402_INVOICE, sql_now
    try:
        database = await resolve_db(db)
        if database is None:
            return False
        ok = await acas_transition(database, "x402_payment_requests", key="id = ?",
                                   key_params=(str(order.request_id),), machine=X402_INVOICE,
                                   to="refund_due", from_states=("completed",),
                                   set_cols={"updated_at": sql_now()})
    except Exception:
        logger.warning("commerce: invoice %s of order %s could not be marked refund_due",
                       order.request_id, order.id, exc_info=True)
        return False
    if ok:
        from modules.x402.x402_integration import _emit_refund_due
        _emit_refund_due(str(order.request_id))
    return bool(ok)


def _refund_text(order: ords.Order) -> str:
    return (f"refund ${order.amount_usd} to the order's refund address, never to the sending "
            f"address, then record it with /invoices refunded {order.request_id}")


def _refund_due_note(marked: bool) -> str:
    """What the owner reads about the invoice: listed as owed only when it moved."""
    if marked:
        return "now listed under /invoices refund_due"
    return ("it could NOT be marked refund_due, so it still counts as income until it is "
            "marked")


async def _failed_notices(container, order: ords.Order, reason: str, *, db=None) -> None:
    marked = await mark_refund_due(order, db=db)
    await tell_buyer(container, order, f"Order {order.id} could not be fulfilled. The owner "
                                       f"has been told and handles the refund.")
    await owner_notice(container, order.user_id,
                       f"⚠️ Order {order.id} FAILED ({reason[:200]}). The buyer paid "
                       f"${order.amount_usd} on invoice {order.request_id} "
                       f"({_refund_due_note(marked)}): {_refund_text(order)}.")


# -- the tick ----------------------------------------------------------------------------

async def tick(*, container=None, path: Optional[str] = None,
               now: Optional[float] = None, db=None) -> Dict[str, int]:
    """Durable follow-up; safe to run in every watcher tick. Never raises."""
    ts = time.time() if now is None else float(now)
    out = {"reclaimed": 0, "retried": 0, "failed": 0, "accepted": 0, "started": 0,
           "expired": 0, "synced": 0, "notices": 0, "threads": 0}
    from core.commerce.hooks import fulfiller
    try:
        for o in (ords.stale_claims(ts, path=path) if fulfiller() else ()):
            m = st.live_version(o.offer_id, o.offer_version, path=path)
            if m is None or o.attempts >= m.max_attempts:
                why = "its fulfillment goal could not be written"
                if ords.fail(o.id, why, path=path):
                    out["failed"] += 1
                    await _failed_notices(container, ords.get(o.id, path=path), why, db=db)
                continue
            tok = ords.reclaim(o, path=path, now=ts)
            if tok and await start_fulfillment(o, container=container, path=path, token=tok):
                out["reclaimed"] += 1

        def _deadline(o):
            m = st.live_version(o.offer_id, o.offer_version, path=path)
            return 60.0 * (m.deadline_min if m else 30)
        for o in ords.overdue(_deadline, ts, path=path):
            m = st.live_version(o.offer_id, o.offer_version, path=path)
            if m and o.attempts < m.max_attempts and ords.release_for_retry(o.id, path=path):
                out["retried"] += 1
                _stop_goal(o.goal_id)
            elif ords.fail(o.id, "fulfillment deadline passed", path=path):
                out["failed"] += 1
                _stop_goal(o.goal_id)
                await _failed_notices(container, ords.get(o.id, path=path),
                                      "fulfillment deadline passed", db=db)
        for o in (ords.list_orders(statuses=[ords.PAID], limit=20, path=path)
                  if fulfiller() else ()):
            if await start_fulfillment(o, container=container, path=path):
                out["started"] += 1

        out["expired"] = await _expire_unpayable(container, path, ts, db)
        out["synced"] = await _sync_markets(path)
        out["notices"] = await _retry_notices(container, path, ts)
        out["threads"] = await _thread_intakes(container, path, ts)

        def _window(o):
            m = st.live_version(o.offer_id, o.offer_version, path=path)
            return DAY_S * (m.acceptance_days if m else 0)
        for o in ords.due_acceptance(_window, ts, path=path):
            if ords.accept(o.id, path=path):
                out["accepted"] += 1
    except st.CommerceUnreadable:
        logger.warning("commerce tick: the commerce store is unreadable", exc_info=True)
    except Exception:
        logger.warning("commerce tick failed", exc_info=True)
    return out


async def _expire_unpayable(container, path: Optional[str], ts: float, db=None) -> int:
    """Close the unpaid orders no payment can reach any more, so none holds its buyer's
    slot for ever: a quote never priced, or an awaiting order whose invoice is gone or
    closed (a crash between the invoice and the order, or inside a cancel), or one
    whose invoice completed on a TEST network (the watcher never pays an order from
    it). An order whose invoice is pending, settling or completed on an income chain
    is the settlement watcher's."""
    from modules.x402 import income_chains
    n = 0
    rows = ords.stale_unpaid(ts - QUOTE_TTL_S, ts - INVOICE_HOURS * 3600.0 - UNPAID_GRACE_S,
                             path=path)
    database = None
    if any(o.status == ords.AWAITING and o.request_id for o in rows):
        from modules.x402._db import resolve_db
        try:
            database = await resolve_db(db)
        except Exception:
            logger.warning("commerce: the invoice store is unavailable", exc_info=True)
        if database is None:
            for o in rows:
                if o.status == ords.AWAITING and o.request_id:
                    ords.skip_unpaid(o.id, path=path, now=ts)
            rows = [o for o in rows if o.status == ords.QUOTED or not o.request_id]
    for o in rows:
        test_paid = False
        if o.status == ords.AWAITING and o.request_id:
            try:
                row = await database.fetch_one(
                    "SELECT status, chain, asset_id FROM x402_payment_requests WHERE id = ?",
                    (str(o.request_id),))
            except Exception:
                logger.warning("commerce: invoice %s unreadable", o.request_id, exc_info=True)
                row = {"status": "unreadable"}  # unreadable is not "gone": never expire on it
            test_paid = (row is not None and row["status"] == "completed"
                         and not income_chains.is_income_chain(row["chain"], row["asset_id"]))
            if row is not None and row["status"] != "expired" and not test_paid:
                # Left to the watcher: to the back of the line, so the rows this step
                # can never expire do not hide the later ones for ever.
                ords.skip_unpaid(o.id, path=path, now=ts)
                continue
        if ords.expire(o.id, path=path, now=ts):
            n += 1
            why = "it was never priced" if o.status == ords.QUOTED else "unpaid"
            await tell_buyer(container, ords.get(o.id, path=path) or o,
                             f"Order {o.id} expired: a test-network payment does not buy."
                             if test_paid else
                             f"Order {o.id} expired ({why}). Nothing was charged.")
    return n


async def _thread_intakes(container, path: Optional[str], ts: float) -> int:
    """109: the public-thread intake of every surface whose pack registers a channel."""
    from core.commerce.hooks import thread_channels
    from modules.commerce.storefront import thread_intake
    n = 0
    for surface in thread_channels():
        n += await thread_intake(container, surface, path=path, now=ts)
    return n


#: Marketplace references this process already refused (and answered).
_REFUSED: "OrderedDict[str, None]" = OrderedDict()


async def _sync_markets(path: Optional[str]) -> int:
    """Pull new orders from every marketplace adapter; hand each buyer OUR invoice."""
    import inspect
    from core.commerce.hooks import listing_adapters
    n = 0
    for kind, adapter in listing_adapters().items():
        sync = getattr(adapter, "sync_orders", None)
        if not kind.startswith("market:") or sync is None:
            continue
        try:
            rows = sync()
            rows = await rows if inspect.isawaitable(rows) else rows
        except Exception:
            logger.warning("commerce: %s order sync failed", kind, exc_info=True)
            continue
        for r in rows or ():
            ref = str(r.get("ref") or "")
            if not ref:
                # No reference = no dedupe: it would make a new order every tick.
                logger.warning("commerce: %s sent an order row with no ref (skipped)", kind)
                continue
            source = f"{kind}:{ref}"
            if source in _REFUSED or ords.by_source(source, path=path) is not None:
                continue
            try:
                order, pay = await create_order(str(r.get("offer_id")), channel=kind,
                                                buyer=str(r.get("buyer") or ref or kind),
                                                inputs=dict(r.get("inputs") or {}),
                                                source=source, listing=kind, path=path)
                reply = {"order_id": order.id, "payment": pay}
                n += 1
            except OrderRefused as exc:
                reply = {"error": str(exc)}
                _REFUSED[source] = None          # answered once; never re-tried each tick
                while len(_REFUSED) > 5000:
                    _REFUSED.popitem(last=False)
            if getattr(adapter, "reply", None):
                try:
                    res = adapter.reply(ref, reply)
                    if inspect.isawaitable(res):
                        await res
                except Exception:
                    logger.warning("commerce: %s reply for %s failed", kind, ref, exc_info=True)
    return n


# -- notices -------------------------------------------------------------------------

async def owner_notice(container, user_id: str, text: str) -> None:
    if not user_id or not text:
        return
    try:
        import core.surfaces.user_delivery as _ud
        await _ud.deliver_user_message(container, user_id, text, source="commerce",
                                       session_id=None)
    except Exception:
        logger.debug("commerce: owner notice failed (fail-open)", exc_info=True)


async def tell_buyer(container, order: ords.Order, text: str, *, media=None) -> bool:
    """Tell the buyer on the channel they ordered from. Never raises.

    109: an order made in a public thread is answered IN that thread (the surface's
    thread channel); a notice the thread does not take now is held on the order and
    retried by :func:`tick`. Otherwise a chat buyer gets a push."""
    if order.thread_ref:
        return await _tell_thread(order, text)
    from core.commerce.offer import native_surfaces
    # A chat buyer is pushed to; a machine buyer (a2a, mcp, http, market) polls its order.
    if order.channel not in native_surfaces() or container is None:
        return False
    try:
        router = container.get_service("message_router")
        if router is None:
            return False
        return bool(await router.send_message(str(order.buyer), text,
                                              surface_id=order.channel,
                                              media=list(media) if media else None))
    except Exception:
        logger.debug("commerce: could not tell the buyer of %s", order.id, exc_info=True)
        return False


async def _tell_thread(order: ords.Order, text: str, *, path: Optional[str] = None,
                       held: bool = False) -> bool:
    """``held``: *text* is the order's held notice (a retry). A failed retry holds
    nothing new, so it can never overwrite a newer notice held meanwhile. A fresh
    notice that sends clears every held notice OLDER than it (it would be posted
    after it); one held while it was being sent stays."""
    from core.commerce.hooks import thread_channel
    ch = thread_channel(order.channel)
    ok = False
    started = time.time()
    try:
        if ch is not None and ch.can_reply():
            ok = bool(await ch.reply(str(order.thread_ref), text,
                                     subject=f"{order.thread_ref}:{order.status}",
                                     user_id=order.user_id))
    except Exception:
        logger.debug("commerce: thread notice for %s failed", order.id, exc_info=True)
    try:
        if ok and held:
            ords.clear_notice(order.id, text, path=path)
        elif ok:
            ords.clear_notice_held_before(order.id, started, path=path)
        elif not held:
            ords.set_notice_pending(order.id, text, path=path)
    except Exception:
        logger.warning("commerce: could not hold the notice of %s", order.id, exc_info=True)
    return ok


#: A thread notice still unsent this long after it was first held goes to the owner.
NOTICE_GIVE_UP_S = 3600.0
#: The gap between two tries of one held thread notice.
NOTICE_RETRY_S = 600.0
_LAST_TRY: Dict[str, float] = {}


async def _retry_notices(container, path: Optional[str], ts: float) -> int:
    """Retry held thread notices; after :data:`NOTICE_GIVE_UP_S` tell the owner once."""
    n = 0
    for o in ords.pending_notices(path=path):
        # Each try costs one X write even when it fails: once per NOTICE_RETRY_S.
        if ts - _LAST_TRY.get(o.id, 0.0) < NOTICE_RETRY_S:
            continue
        _LAST_TRY[o.id] = ts
        if len(_LAST_TRY) > 5000:
            _LAST_TRY.pop(next(iter(_LAST_TRY)))
        if await _tell_thread(o, str(o.notice_pending), path=path, held=True):
            n += 1
        elif ts - float(o.notice_since or o.updated_at or 0) > NOTICE_GIVE_UP_S:
            if not ords.clear_notice(o.id, str(o.notice_pending), path=path):
                continue                 # a newer notice is held: its own clock runs
            await owner_notice(container, o.user_id, (
                f"The buyer of order {o.id} ({o.status}) could not be told in their "
                f"{o.channel} thread for an hour. Last notice: {str(o.notice_pending)[:300]}"))
    return n


__all__ = ["ORDER_KIND", "OrderRefused", "cancel_order", "check_proof", "close_open_orders",
           "create_order",
           "deliver", "fail",
           "on_expired", "on_settled", "owner_notice", "payment_view", "quote_order",
           "start_fulfillment", "tell_buyer", "tick"]
