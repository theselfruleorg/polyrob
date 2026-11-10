"""What the owner reads about selling (109 W6): one status block and one report.

* :func:`status_lines` — the ``money`` section's commerce lines: live offers, open
  orders by state, held thread notices and the public-thread intake's last poll and
  stop reason (a refund owed is its invoice's ``refund_due``, the money section's
  ``payment_refund_due`` item, never a second count here). Problems come back as health
  items ``(key, text, remedy)``.
* :func:`offer_report` — per offer: treasury income (orders that took money), the
  treasury spend the order runs reserved, and treasury margin = income − spend. The
  LLM cost of the runs is the owner's runtime pocket and is never subtracted (the two
  pockets are never summed); ``/usage`` shows it.

⚠️ An unreadable store raises (``CommerceUnreadable``): it is never shown as "no
offers". Tier-0: imports nothing above ``core``.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from core.commerce import orders as ords
from core.commerce import store as st
from core.commerce import thread

#: A thread intake whose last poll is older than this is named as stalled.
POLL_STALE_S = 900.0


def _usd(v: Any) -> Decimal:
    try:
        return Decimal(str(v or "0"))
    except Exception:
        return Decimal("0")


def status_lines(user_id: str, *, path: Optional[str] = None,
                 now: Optional[float] = None) -> Tuple[List[str], List[Tuple[str, str, str]]]:
    ts = time.time() if now is None else float(now)
    offers = st.list_offers(user_id, path=path)
    if not offers:
        return [], []
    live = [o for o in offers if o.state == st.LIVE]
    rows = ords.list_orders(user_id=user_id, limit=500, path=path)
    open_by: Dict[str, int] = {}
    for o in rows:
        if o.is_open:
            open_by[o.status] = open_by.get(o.status, 0) + 1
    lines = [f"selling: {len(live)} live offer(s) of {len(offers)}; open orders: "
             + (", ".join(f"{n} {s}" for s, n in sorted(open_by.items())) or "none")]
    health: List[Tuple[str, str, str]] = []
    # A refund owed on an order is its invoice in `refund_due`: the money section's
    # `payment_refund_due` item names it and clears when the refund is recorded.
    held = [o for o in rows if o.notice_pending]
    if held:
        health.append(("offer_notice_held",
                       f"{len(held)} buyer notice(s) wait for their public thread",
                       "they retry by themselves; the owner is told after an hour"))
    for surface in sorted({r["surface"] for r in thread.threads(path=path)}):
        c = thread.cursor(surface, path=path)
        if c is None:
            lines.append(f"{surface} order replies: not read yet")
            continue
        age = ts - float(c["polled_at"] or 0)
        note = f"{surface} order replies: last read {int(age // 60)} min ago"
        if c["stop_reason"]:
            note += f" — {c['stop_reason']}"
        lines.append(note)
        if live and (age > POLL_STALE_S or str(c["stop_reason"] or "").startswith(
                f"{surface} api")):
            health.append((f"offer_threads_{surface}",
                           f"{surface} order replies are not being answered"
                           + (f" ({c['stop_reason']})" if c["stop_reason"] else ""),
                           f"/status names the {surface} login or credit state"))
    return lines, health


def offer_report(user_id: str, offer_id: Optional[str] = None, *,
                 path: Optional[str] = None) -> str:
    offers = [o for o in st.list_offers(user_id, path=path)
              if offer_id is None or o.id == offer_id]
    if not offers:
        return f"No offer {offer_id}." if offer_id else "No offers yet."
    out = ["Offer report (treasury only; the LLM cost of the runs is your runtime "
           "pocket, shown by /usage and never subtracted here):"]
    for o in offers:
        rows = ords.list_orders(user_id=user_id, offer_id=o.id, limit=10000, path=path)
        sold = [r for r in rows if r.status in ords.SOLD]
        refund = [r for r in rows if r.status == ords.FAILED and (r.payment_tx or r.paid_at)]
        income = sum((_usd(r.amount_usd) for r in sold), Decimal("0"))
        spend = sum((Decimal(str(r.spent_usd or 0)) for r in rows), Decimal("0"))
        out.append(f"• {o.id} — {len(sold)} sold, income ${income:.2f}, order spend "
                   f"${spend:.2f}, margin ${income - spend:.2f}"
                   + (f"; {len(refund)} failed paid order(s)" if refund else ""))
    return "\n".join(out)


__all__ = ["POLL_STALE_S", "offer_report", "status_lines"]
