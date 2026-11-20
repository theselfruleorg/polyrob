"""``/invoices`` and ``/settle`` — the agent's receivables, from an owner seat.

The body every seat runs (proposal 125 S14): the Telegram seat table
(``surfaces/telegram/admin_verbs.py``), the REPL and the console import it from here. ``/settle`` is an owner attestation, not a
payment; every id resolves among the caller's OWN rows through the ONE short-id
resolver (``core.short_id.resolve_prefix``).
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from core.short_id import resolve_prefix

logger = logging.getLogger(__name__)

#: Rows one chat listing renders before collapsing the rest into a count.
_LIST_LIMIT = 10


def _code(text: Any) -> str:
    return f"`{text}`"


def _tail(user_id: str, hidden_lines: List[str], what: str) -> Optional[str]:
    """The cut line of a capped list — the rest one tap away (``/more_<token>``)."""
    if not hidden_lines:
        return None
    from core.surfaces.more_store import cut_line
    return cut_line(user_id, len(hidden_lines), "\n".join(hidden_lines),
                    what=f"more {what}", prefix="")


def _invoicing_off_note() -> str:
    """The ONE feature-off note (``modules.x402.invoicing_note``), on its own
    line for a chat bubble."""
    from modules.x402.invoicing_note import invoicing_off_note
    note = invoicing_off_note()
    return f"\n⚠️ {note}" if note else ""


async def invoices_reply(user_id: str, args: List[str], *, db=None) -> str:
    """`/invoices [<status>]` — the agent's receivables. The REPL calls it too,
    with its own bot.db (``db``), so one verb gives one text (113 W4 item 3).

    ⚠️ The filter vocabulary is ``modules.x402.invoicing.INVOICE_STATUSES``,
    imported and never re-listed. This seat carried its own
    ``pending|completed|expired`` triple, so the two states that matter most
    were unaskable: ``settling`` (claimed, facilitator in flight) and
    ``refund_due`` — money the agent TOOK and owes back. A row in either state
    simply disappeared from this seat the moment it was written.
    """
    from modules.x402.invoicing import INVOICE_STATUSES, list_payment_requests
    # `refunded` is also a status: bare it LISTS refunded rows; with an id it
    # records a refund (098 MO-03).
    if len(args) > 1 and args[0].lower() == "refunded":
        return await invoice_refunded_reply(user_id, args[1:], db=db)
    _usage = (f"Usage: /invoices [{'|'.join(INVOICE_STATUSES)}] · "
              "/invoices refunded <id> [tx-hash]")
    status = None
    if args:
        candidate = args[0].lower()
        if candidate in INVOICE_STATUSES:
            status = candidate
        else:
            return _usage
    try:
        rows = await list_payment_requests(user_id=user_id, status=status, limit=50, db=db)
    except Exception as e:
        logger.warning("telegram /invoices read failed: %s", e, exc_info=True)
        return f"Could not read invoices: {e}"
    scope = f"tenant {_code(user_id)}" + (f" · {status}" if status else "")
    if not rows:
        return f"No invoices ({scope})."
    lines = [f"{len(rows)} invoice(s) — {scope}:"]
    rendered = []
    for r in rows:
        line = (f"• {_code(str(r['request_id'])[:8])} [{r['status']}] "
                f"${float(r.get('amount_usd') or 0):.2f} — "
                f"{r.get('purpose') or '(no purpose)'}")
        if r.get("payer_contact"):
            line += f" · billed to {r['payer_contact']}"
        rendered.append(line)
    lines += rendered[:_LIST_LIMIT]
    more = _tail(user_id, rendered[_LIST_LIMIT:], "invoice(s)")
    if more:
        lines.append(more)
    lines.append("Mark one paid: /settle <id> [tx-hash]")
    # A `refund_due` row is money the agent TOOK and owes back, so it must not
    # sit in the list under the same one-line remedy as a pending receivable.
    _owed = [r for r in rows if str(r.get("status")) == "refund_due"]
    if _owed:
        _owed_usd = sum(float(r.get("amount_usd") or 0) for r in _owed)
        lines.append(f"⚠️ {len(_owed)} of these are REFUNDS I owe "
                     f"(${_owed_usd:.2f}) — paid for work that then failed. "
                     f"`/invoices refund_due` lists only those; after you pay one "
                     f"back: /invoices refunded <id> [tx-hash]")
    note = _invoicing_off_note()
    if note:
        lines.append(note.strip())
    return "\n".join(lines)


async def invoice_refunded_reply(user_id: str, args: List[str], *, db=None) -> str:
    """`/invoices refunded <id> [tx-hash]` — record that a refund the agent owed
    was paid back (``refund_due → refunded``, 098 MO-03).

    The SAME operation as ``polyrob owner refunded`` (``mark_payment_refunded``,
    a guarded write from ``refund_due`` only). The id resolves among the
    caller's OWN ``refund_due`` rows, so a chat id can never close another
    tenant's obligation. The REPL calls it too, with its own bot.db (``db``)."""
    from modules.x402.invoicing import list_payment_requests
    from modules.x402.x402_integration import mark_payment_refunded
    try:
        rows = await list_payment_requests(user_id=user_id, status="refund_due", limit=200,
                                           db=db)
    except Exception as e:
        logger.warning("telegram /invoices refunded lookup failed: %s", e, exc_info=True)
        return f"Could not read invoices: {e}"
    request_id, err = resolve_prefix(args[0], [str(r["request_id"]) for r in rows])
    if err:
        return f"{err} among the refunds you owe — see /invoices refund_due."
    tx_hash = args[1] if len(args) > 1 else None
    try:
        done = await mark_payment_refunded(request_id, tx_hash=tx_hash,
                                           note="owner: /invoices refunded", db=db)
    except Exception as e:
        logger.warning("telegram /invoices refunded failed: %s", e, exc_info=True)
        return f"Refund not recorded: {e}"
    if not done:
        return (f"{_code(request_id[:8])} was not changed — it is no longer "
                "refund_due (already refunded, or the store refused the write).")
    return (f"✅ Refund recorded for {_code(request_id[:8])}"
            + (f" (tx {_code(tx_hash)})" if tx_hash else "")
            + ". /status stops counting it as owed.")


async def settle_reply(user_id: str, args: List[str]) -> str:
    """`/settle <id> [tx-hash]` — attest an invoice as PAID (pending → completed).

    Owner attestation, not a payment: it records that money arrived. Scoped to
    the caller's own tenant, so a chat id can never settle another tenant's row.
    """
    if not args:
        return "Usage: /settle <invoice-id> [tx-hash] (see /invoices)"
    from modules.x402.invoicing import list_payment_requests, settle_payment_request
    try:
        rows = await list_payment_requests(user_id=user_id, status="pending", limit=200)
    except Exception as e:
        logger.warning("telegram /settle lookup failed: %s", e, exc_info=True)
        return f"Could not read invoices: {e}"
    request_id, err = resolve_prefix(args[0], [str(r["request_id"]) for r in rows])
    if err:
        return f"{err} among your PENDING invoices — see /invoices pending."
    tx_hash = args[1] if len(args) > 1 else None
    try:
        settled = await settle_payment_request(request_id, transaction_hash=tx_hash)
    except Exception as e:
        logger.warning("telegram /settle failed: %s", e, exc_info=True)
        return f"Settle failed: {e}"
    if not settled:
        return (f"{_code(request_id[:8])} was not settled — it may already be "
                "completed, that tx hash already settled another invoice, "
                "or it is an order's invoice: give the transaction hash.")
    out = f"✅ Settled {_code(request_id[:8])}."
    note = _invoicing_off_note()
    out += (note if note else
            "\nThe settlement watcher will wake the originating session.")
    return out


__all__ = ["invoice_refunded_reply", "invoices_reply", "settle_reply"]
