"""Status slot ``room_actions`` (046 paid room actions) — 067 P5a.

Moved out of ``core/status_snapshot.py`` unchanged; registered into the slot
the snapshot names (``core.status_sections``). P5b: the paid half of rooms
moves with the wallet pack and registers this slot from there.
"""
from __future__ import annotations

import os

from core import status_snapshot as _ss
from core.status_sections import register_status_section
from core.status_snapshot import SEVERITY_CRIT, HealthItem, Section


# Called THROUGH the snapshot module so a patched ``_rows`` applies here too.
def _rows(*args, **kwargs):
    return _ss._rows(*args, **kwargs)


def _room_actions_section(user_id: str, data_dir: str) -> Section:
    """046: paid room actions — what is awaiting payment, and what is OWED.

    ⚠️ A CREDIT is money we HOLD against a service that was never delivered, so
    it leads as CRIT. Read-only and existence-guarded: an absent store is "no
    paid actions", never CREATED by a status read.
    """
    sec = Section(name="room_actions")
    db = os.path.join(data_dir, "room_actions.db")
    if not os.path.exists(db):
        sec.lines.append("no paid actions")
        sec.data.update(pending=0, credits_owed=0, applied=0, credits=[])
        return sec
    rows = _rows(db, "SELECT offer_id, surface, chat_id, verb, price_usd, "
                     "status, reason FROM room_action_offers "
                     "ORDER BY created_at DESC LIMIT 200")
    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
    credits = [r for r in rows if r["status"] == "credited"]
    sec.data.update(
        pending=by.get("pending", 0), paid=by.get("paid", 0),
        applied=by.get("applied", 0), credits_owed=len(credits),
        credits=[{"offer_id": r["offer_id"], "verb": r["verb"],
                  "chat_id": r["chat_id"], "price_usd": r["price_usd"],
                  "reason": r["reason"]} for r in credits[:10]])
    if not rows:
        sec.lines.append("no paid actions")
        return sec
    sec.lines.append(", ".join(f"{n} {st}" for st, n in sorted(by.items())))
    # A PAID offer whose effect has not landed is an obligation in flight, not a
    # failure yet — named, but not escalated.
    in_flight = by.get("paid", 0)
    if in_flight:
        sec.lines.append(f"{in_flight} paid action(s) awaiting the effect")
    if credits:
        owed = sum(float(r.get("price_usd") or 0) for r in credits)
        sec.health.append(HealthItem(
            key="room_action_credits_owed", severity=SEVERITY_CRIT,
            text=(f"{len(credits)} paid room action(s) took money and could not "
                  f"be applied (${owed:.2f}): "
                  + "; ".join(f"{r['offer_id']} ({str(r.get('reason') or '')[:50]})"
                              for r in credits[:3])),
            remedy="/paid offers · polyrob owner paid list"))
    return sec


register_status_section(
    "room_actions", lambda ctx: _ss._guarded("room_actions", _ss._room_actions_section,
                                             ctx.uid, ctx.data_dir),
    tenant=False)
