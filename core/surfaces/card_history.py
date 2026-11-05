"""What was decided on the owner's cards, for a reader that counts (103).

A read over ``cards.db``: it holds no state and changes no card. The kept cards are
``card_groups.leaves``: a quote that a refresh replaced is not a second send.
"""
from typing import Any, List

from core.surfaces.card_groups import leaves


def decided_since(st: Any, user_id: str, since: float, *, session_id: str = "",
                  origin: str = "", origin_ref: str = "", limit: int = 500) -> List[Any]:
    """The verb cards that left ``open`` at or after ``since``, newest first.

    With ``session_id`` / ``origin``: only that session's cards and that goal's or
    cron job's groups. Without: every card of the owner.
    """
    where = ["user_id=?", "updated_at>=?", "state NOT IN ('open', 'confirmed')"]
    args: List[Any] = [str(user_id), float(since)]
    scope = []
    if session_id:
        scope.append("json_extract(doc, '$.session_id')=?")
        args.append(str(session_id))
    if origin:
        scope.append("json_extract(doc, '$.group_id') IN "
                     "(SELECT group_id FROM card_groups WHERE origin=? AND origin_ref=?)")
        args += [str(origin), str(origin_ref or "")]
    if scope:
        where.append("(" + " OR ".join(scope) + ")")
    conn = st._conn()
    try:
        rows = conn.execute(
            "SELECT * FROM cards WHERE " + " AND ".join(where)
            + " ORDER BY updated_at DESC LIMIT ?", (*args, int(limit))).fetchall()
    finally:
        conn.close()
    return [c for c in leaves([st._row(r) for r in rows]) if c.verb]


def open_count(st: Any, user_id: str) -> int:
    return sum(1 for c in st.open_cards(user_id, limit=200) if c.verb)
