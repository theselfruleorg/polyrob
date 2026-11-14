"""The owner's list of open cards and the cancel of a card by a typed verb.

``/cards`` lists each open card in three short lines: what it does, why, and
its actions. ``/cancel <card id>`` and ``/cancel cards`` cancel cards on the
verb a person types to stop something. Split out of ``core.surfaces.cards``
(file size); the store and the press stay there.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from core.surfaces import cards

logger = logging.getLogger(__name__)


def list_reply(user_id: str, *, st: Optional["cards.CardStore"] = None) -> str:
    """``/cards`` — the owner's open cards, newest first."""
    try:
        st = st or cards.store()
        for stuck in st.sweep_stuck(str(user_id)):
            cards._notify(stuck)
        open_ = st.open_cards(str(user_id))
    except Exception:
        logger.warning("/cards: store unreadable", exc_info=True)
        return "I could not read the card store, so I cannot say what is open."
    if not open_:
        return "No open cards."
    out = [f"🃏 Open cards: {len(open_)}", ""]
    for i, c in enumerate(open_, start=1):
        out.append(f"{i}. {card_summary(c)}")
        why = card_reason(c)
        if why:
            out.append(f"   {why}")
        acts = " · ".join(f"{a.label} {a.command}" for a in cards.card_actions(c))
        out.append(f"   {acts}" if acts else f"   open it: {cards.read_token(c.card_id)}")
        out.append("")
    out.append("Open one in full: /card_<id>. Cancel every card: /cancel cards.")
    return "\n".join(out)


def _addr(a: str) -> str:
    a = str(a or "")
    return f"{a[:6]}…{a[-4:]}" if a.startswith("0x") and len(a) > 12 else a


def _count(word: str) -> str:
    try:
        v = float(word)
    except (TypeError, ValueError):
        return str(word)
    return f"{v:,.6f}".rstrip("0").rstrip(".")


def card_summary(card: "cards.Card") -> str:
    """One short line: WHAT the card does. A /send reads as the send itself
    (`Send 177,103 0xabab…abab → 0xcdcd…cdcd on base`), every other card
    as its title. Who raised it is the prefix (🤖 the agent)."""
    a = list(card.args or [])
    who = "🤖 " if card.origin == cards.ORIGIN_AGENT else ""
    if card.verb == "/send" and len(a) >= 6 and a[2].lower() == "to" and a[4].lower() == "on":
        amount = a[0] if a[0].startswith("$") else _count(a[0])
        token = "native" if a[1].lower() == "native" else _addr(a[1])
        line = f"💸 Send {amount} {token} → {_addr(a[3])} on {a[5]}"
    elif card.verb and card.kind in (cards.KIND_QUOTE, cards.KIND_PROPOSAL):
        line = cards._short(" ".join([card.verb] + [_addr(x) for x in a]), cards.MAX_TITLE_CHARS)
    else:
        line = cards._short(card.title, cards.MAX_TITLE_CHARS)
    kind = {cards.KIND_QUOTE: "quote", cards.KIND_PROPOSAL: "proposal"}.get(card.kind, card.kind)
    return f"{who}{line} ({kind})"


def card_reason(card: "cards.Card") -> str:
    """The card's reason in one short line: the last line of its body (the
    agent's `why`), cut to 90 characters. Empty when there is none."""
    lines = [ln.strip() for ln in (card.body or "").splitlines() if ln.strip()]
    if not lines or card.kind == cards.KIND_QUOTE:
        return ""
    return cards._short(lines[-1], 90)


def cancel_reply(user_id: str, words: List[str], *,
                 st: Optional["cards.CardStore"] = None) -> Optional[str]:
    """``/cancel <card id>`` and ``/cancel cards``: the cancel of a CARD, on
    the verb a person types to stop something. None when the words name no
    card, so the seat's ``/cancel`` stops the running task as before (prod
    2026-11-13: the owner typed /cancel to drop a card and it stopped a task).
    Nothing moves when a card is cancelled."""
    w = [str(x).strip().lower() for x in (words or []) if str(x).strip()]
    if not w:
        return None
    if w in (["cards"], ["all", "cards"], ["cards", "all"]):
        try:
            st = st or cards.store()
            open_ = st.open_cards(str(user_id), limit=200)
        except Exception:
            logger.warning("/cancel cards: store unreadable", exc_info=True)
            return "I could not read the card store, so I cancelled no card."
        cancelled = cards._STATE_WORDS[cards.S_CANCELLED]
        done = [c.card_id for c in open_
                if cards.press(c.card_id, "no", user_id, st=st).reply == cancelled]
        if not done:
            return "No open cards. Nothing was cancelled."
        return f"✖️ Cancelled {len(done)} card(s): {', '.join(done)}. Nothing was done."
    if len(w) == 1 and cards._CARD_ID_RE.match(w[0].removeprefix("/card_")):
        cid = w[0].removeprefix("/card_")
        return cards.press(cid, "no", user_id, st=st).reply
    return None


def agent_view(user_id: str, *, hours: float = 24.0,
               st: Optional["cards.CardStore"] = None) -> str:
    """What the AGENT reads about its cards: every OPEN card (id, the exact
    command, its reason) and every card decided in the last *hours* with its
    outcome. Prod 2026-11-13: the agent could not read the store, answered the
    owner from its notes, and named a replaced card's token count."""
    import time as _time
    try:
        st = st or cards.store()
        open_ = st.open_cards(str(user_id), limit=50)
        changed = st.changed_since(str(user_id), _time.time() - hours * 3600)
    except Exception:
        logger.warning("cards_read: store unreadable", exc_info=True)
        return "UNREADABLE: I could not read the card store. This is NOT 'no cards'."
    out = [f"OPEN cards ({len(open_)}) — the owner can tap these now:"]
    for c in open_:
        out.append(f"- {c.card_id} {c.kind}: {c.line or c.title}"
                   + (f" | why: {card_reason(c)}" if card_reason(c) else ""))
    if not open_:
        out.append("- none")
    decided = [c for c in changed if c.state != cards.S_OPEN]
    out.append(f"DECIDED in the last {hours:g} h ({len(decided)}), newest first:")
    for c in decided:
        when = _time.strftime("%H:%MZ", _time.gmtime(c.updated_at))
        what = c.outcome or c.state
        out.append(f"- {when} {c.card_id} {c.kind} {c.state}"
                   + (f" ({what})" if c.outcome else "") + f": {c.line or c.title}")
    if not decided:
        out.append("- none")
    out.append("A replaced card is NOT open and NOT paid; a quote 'done (sent)' paid once. "
               "Quote these ids and counts, never your notes.")
    return "\n".join(out)


__all__ = ["agent_view", "cancel_reply", "card_reason", "card_summary", "list_reply"]
