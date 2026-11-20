"""Call cards (proposal 126): ONE owner decision that holds the exact typed call.

A quote card stores a seat COMMAND LINE, so an action with no seat verb could not
be carded, and a flow of several transactions was several decisions with prose in
between (the 2026-11-20 LP move: a declaration card, then a line to type, then an
approval no seat could make). A call card stores the STEPS themselves:

* each step is ``{"do": <name>, "args": {…}}`` — a registered action id
  (``defi_trade_lp_add``) with its validated params, or a named owner write
  (``lp_seed_confirm``) that only the owner's tap may make;
* the steps carry a digest. The runner (``tools/controller/call_card_run.py``)
  refuses a row whose steps no longer match it, so what runs is what the card showed;
* Confirm runs the steps once, in order, on the owner's seat
  (``core.security.turn_facts.owner_seat_context``): the policy chain, the guard,
  the wallet caps and every gate run again. A card adds no authority beyond "the
  owner tapped this exact call".

This module is the core half: the row's shape, its digest and its progress. It
imports no tool; the runner lives in the tools tier and the seats call it.
"""
import hashlib
import json
import re
from typing import Any, Dict, List, Optional

from core.surfaces import cards

KIND = cards.KIND_CALL
#: The most steps one card holds (a move is two: declare, then grants + withdraw + mint).
MAX_STEPS = 6
#: A call card is priced like a quote: past this the owner refreshes it.
CALL_TTL_S = cards.QUOTE_TTL_S
_STEP_NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
#: The most bytes the steps of one card may take (they are params, not prose).
MAX_STEPS_BYTES = 8000


def _canonical(steps: List[Dict[str, Any]]) -> str:
    return json.dumps(steps, sort_keys=True, separators=(",", ":"))


def digest(steps: List[Dict[str, Any]]) -> str:
    """The short sha256 of the steps — what the card showed is what runs."""
    return hashlib.sha256(_canonical(steps).encode()).hexdigest()[:16]


def clean_steps(steps: Any) -> List[Dict[str, Any]]:
    """The steps as stored, or ValueError. Each is ``{"do": name, "args": dict}``
    with JSON values only (a float, an int, a string, a bool, None)."""
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"a call card holds 1 to {MAX_STEPS} steps")
    out = []
    for step in steps:
        if not isinstance(step, dict) or set(step) != {"do", "args"}:
            raise ValueError("a step is {'do': <name>, 'args': {...}}")
        name, args = step["do"], step["args"]
        if not isinstance(name, str) or not _STEP_NAME.match(name):
            raise ValueError(f"a step name is a lower-case identifier, not {name!r}")
        if not isinstance(args, dict) or any(
                not isinstance(k, str) or not isinstance(v, (str, int, float, bool, type(None)))
                for k, v in args.items()):
            raise ValueError(f"the args of {name} are a flat map of plain values")
        out.append({"do": name, "args": dict(args)})
    if len(_canonical(out)) > MAX_STEPS_BYTES:
        raise ValueError("the steps are too long for one card")
    return out


#: The args that do not change WHAT a call does: its stated reason and its USD bound
#: (a retry restates both). Two calls that differ only here are one call.
_KEY_IGNORED = frozenset({"why", "max_spend_usd"})


def call_key(steps: Any) -> str:
    """The digest of what the steps DO (:data:`_KEY_IGNORED` left out), or ValueError."""
    return digest([{"do": s["do"], "args": {k: v for k, v in s["args"].items()
                                            if k not in _KEY_IGNORED}}
                   for s in clean_steps(steps)])


def open_same(user_id: str, steps: Any, *,
              st: Optional[cards.CardStore] = None) -> Optional[cards.Card]:
    """This owner's OPEN call card that holds the same call, or None. A retry of a
    refused call reuses that card: two cards of one call are two sends after two taps.
    Raises when the store cannot be read (the caller decides)."""
    key = call_key(steps)
    for card in (st or cards.store()).open_cards(str(user_id), limit=50):
        if card.kind != KIND:
            continue
        try:
            if call_key(steps_of(card)) == key:
                return card
        except ValueError:
            continue
    return None


def make(user_id: str, steps: Any, *, title: str, body: str = "",
         rebuild: Optional[Dict[str, Any]] = None, origin: str = cards.ORIGIN_AGENT,
         session_id: Optional[str] = None, group_id: Optional[str] = None,
         st: Optional[cards.CardStore] = None) -> cards.Card:
    """Store one call card. ``rebuild`` names how the runner makes a fresh card for a
    Refresh (``{"kind": "lp_move", "args": {…}}``); without it Refresh says to ask again."""
    clean = clean_steps(steps)
    if rebuild is not None:
        rebuild = {"kind": str(rebuild.get("kind") or ""),
                   "args": clean_steps([{"do": "rebuild", "args": dict(
                       rebuild.get("args") or {})}])[0]["args"]}
    draft = {"call": {"steps": clean, "digest": digest(clean), "rebuild": rebuild,
                      "done": 0}}
    return cards._new(user_id, KIND, origin, title, body=body, session_id=session_id,
                      group_id=group_id, ttl=CALL_TTL_S, st=st, draft=draft)


def steps_of(card: cards.Card) -> List[Dict[str, Any]]:
    """The card's steps, or ValueError when the row does not match its digest."""
    call = (card.draft or {}).get("call") or {}
    steps = clean_steps(call.get("steps"))
    if digest(steps) != call.get("digest"):
        raise ValueError("the card's steps do not match what it showed; nothing ran")
    return steps


def done_count(card: cards.Card) -> int:
    try:
        return max(0, int(((card.draft or {}).get("call") or {}).get("done") or 0))
    except (TypeError, ValueError):
        return 0


def rebuild_of(card: cards.Card) -> Optional[Dict[str, Any]]:
    return ((card.draft or {}).get("call") or {}).get("rebuild") or None


def progress(card_id: str, done: int, text: str, *,
             st: Optional[cards.CardStore] = None) -> Optional[cards.Card]:
    """Record that ``done`` steps finished, with their report so far, on a RUNNING
    card (``confirmed`` → ``confirmed``): each seat re-renders it in place."""
    st = st or cards.store()
    cur = st.get(card_id)
    if cur is None or cur.state != cards.S_CONFIRMED:
        return None
    draft = dict(cur.draft or {})
    call = dict(draft.get("call") or {})
    call["done"] = int(done)
    draft["call"] = call
    moved = st.transition(card_id, frozenset({cards.S_CONFIRMED}), cards.S_CONFIRMED,
                          expect_updated_at=cur.updated_at, draft=draft,
                          result=cards._stored_result(text))
    if moved:
        cards._notify(moved)
    return moved


__all__ = ["CALL_TTL_S", "KIND", "MAX_STEPS", "call_key", "clean_steps", "digest",
           "done_count", "make", "open_same", "progress", "rebuild_of", "steps_of"]
