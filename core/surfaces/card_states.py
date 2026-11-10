"""The action-card state table, and what a decided money card says about a payment.

``core.surfaces.cards`` writes every state change through ``CardStore.transition``
(a compare-and-swap on the row). This module declares the edges those writes may
take, so a new caller cannot invent one (098 S1). Every edge below is one the code
performs:

* ``open``      → ``confirmed`` (a quote's or a line card's Confirm), ``cancelled``,
  ``expired``, ``replaced`` (a refresh, a proposal's quote, a builder's last pick),
  ``done`` (a choice picked), and ``open`` itself (a builder advances one step);
* ``confirmed`` → ``done`` | ``failed`` (``finish``; the stuck sweep closes as failed);
* ``failed``    → ``done`` | ``failed`` (a late real result replaces the sweep's
  "no result", AC2);
* ``replaced``  → ``replaced`` (``note_result`` stores what its line produced).

``failed`` mixes two facts about a payment, so a money card also stores its
:func:`outcome_of`: ``not_sent`` (the rail said nothing moved) or ``unknown`` (it
may have landed). The /send repeat guard counts ``unknown`` as paid.

Pure core: imports only the state primitive at load, ``cards`` lazily.
"""
from typing import Any, List, Tuple

from core.state.machine import StateMachine

S_OPEN = "open"
S_CONFIRMED = "confirmed"   # the confirm line is running
S_DONE = "done"
S_FAILED = "failed"
S_CANCELLED = "cancelled"
S_EXPIRED = "expired"
S_REPLACED = "replaced"     # a refresh (or a proposal's quote) superseded it

CARD = StateMachine(
    name="action_card",
    states=frozenset({S_OPEN, S_CONFIRMED, S_DONE, S_FAILED, S_CANCELLED, S_EXPIRED,
                      S_REPLACED}),
    initial=frozenset({S_OPEN}),
    transitions={
        S_OPEN: frozenset({S_OPEN, S_CONFIRMED, S_DONE, S_CANCELLED, S_EXPIRED, S_REPLACED}),
        S_CONFIRMED: frozenset({S_DONE, S_FAILED}),
        S_FAILED: frozenset({S_DONE, S_FAILED}),
        S_REPLACED: frozenset({S_REPLACED}),
    },
    terminal=frozenset({S_DONE, S_CANCELLED, S_EXPIRED}))

OUTCOME_SENT = "sent"
OUTCOME_NOT_SENT = "not_sent"
OUTCOME_UNKNOWN = "unknown"

#: Words of a result after which a payment MAY have landed: a broadcast whose receipt
#: was not seen, an unknown outcome, a run that reported nothing, or one that raised.
_UNKNOWN_MARKS = ("NOT CONFIRMED", "outcome unknown", "No result was reported",
                  "Command failed")


def outcome_of(text: str, failed: bool) -> str:
    """What a finished money card's result says about the payment."""
    if any(m in (text or "") for m in _UNKNOWN_MARKS):
        return OUTCOME_UNKNOWN
    return OUTCOME_NOT_SENT if failed else OUTCOME_SENT


def recent_sends(st: Any, user_id: str, since: float, verb: str = "/send",
                 limit: int = 300, *, kind: str, sent_marks: Tuple[str, ...],
                 failed_result: Any) -> List[Tuple[str, str, float, List[str], str]]:
    """``(to, token word, finished_at, args, how)`` of this owner's rail cards for ``verb``
    since ``since``: confirmed by the rail (``how="paid"``), running now (``"paid"``), or
    failed with an UNKNOWN outcome (``"unknown"``: it may have landed)."""
    conn = st._conn()
    try:
        rows = conn.execute(
            "SELECT doc, state, updated_at FROM cards WHERE user_id=? AND kind=? "
            "AND state IN (?,?,?) AND updated_at>=? ORDER BY updated_at DESC LIMIT ?",
            (user_id, kind, S_DONE, S_CONFIRMED, S_FAILED, float(since),
             int(limit))).fetchall()
    finally:
        conn.close()
    import json
    out = []
    for r in rows:
        doc = json.loads(r["doc"] or "{}")
        if doc.get("verb") != verb:
            continue
        res, how = str(doc.get("result") or ""), "paid"
        if r["state"] == S_DONE and (not any(m in res for m in sent_marks)
                                     or failed_result(res)):
            continue
        if r["state"] == S_FAILED:
            if (doc.get("outcome") or outcome_of(res, True)) != OUTCOME_UNKNOWN:
                continue
            how = OUTCOME_UNKNOWN
        args = [str(a) for a in doc.get("args") or []]
        low = [a.lower() for a in args]
        if "to" not in low or low.index("to") + 1 >= len(args) or len(args) < 2:
            continue
        out.append((args[low.index("to") + 1], args[1], float(r["updated_at"]), args, how))
    return out


__all__ = ["CARD", "OUTCOME_NOT_SENT", "OUTCOME_SENT", "OUTCOME_UNKNOWN", "S_CANCELLED",
           "S_CONFIRMED", "S_DONE", "S_EXPIRED", "S_FAILED", "S_OPEN", "S_REPLACED",
           "outcome_of", "recent_sends"]
