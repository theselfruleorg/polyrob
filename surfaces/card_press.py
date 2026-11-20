"""One card press for every owner seat: the tap ``/card_<id>_<act>``, ``/cards <id> <act>``
and the typed ``/approve|/reject <call card id>``.

Telegram (and the chat apps and the console on its executor) and the REPL each had
their own copy of this procedure (``surfaces/telegram/card_ops.py``,
``cli/ui/commands/h_cards.py``), and the copies drifted: a call card's Confirm ran in
the background from a tap but inline from a typed ``/approve <id>``, and the REPL's
``/reject <id>`` did not reach a call card at all. The seats now render; this module
decides.

A seat hands in a :class:`PressSeat`: who presses, how the seat runs a command line
as if the owner typed it (``run_line``), and how a late result reaches the owner
(``deliver``; None = the seat waits for the result). What a press DOES comes from the
stored card (``core.surfaces.cards.press``), never from the seat:

* a call card (126): the runner runs its steps once as this owner seat; with a
  ``deliver`` a Confirm runs in the background;
* a builder pick: the card advances; the last pick runs the verb's QUOTE;
* a refresh or a proposal's quote: the line runs under the card's lineage, and a
  line that made no new card is this card's outcome;
* a confirm: the stored line runs EXACTLY as typed (every gate again), then the card
  finishes with the line's result — in the background when the line is an executing
  money verb and the seat can deliver.

Shared by the seats, so it lives in the seats' tier (it reaches the call-card runner
in ``tools`` and the builder in ``surfaces``, which ``core`` may not import).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from core.surfaces import card_groups  # noqa: F401 — installs the 084 settle hook
from core.surfaces import cards
from core.surfaces.command_reply import reply_text

logger = logging.getLogger(__name__)

#: ``run_line(line, deliver)`` -> the line's reply (text or a ``CommandReply``).
RunLine = Callable[[str, Optional[Callable[[str], Awaitable[Any]]]], Awaitable[Any]]


@dataclass
class PressSeat:
    """What one seat gives the press."""
    #: The turn seat a call card runs as (``core.security.turn_facts.SEATS``).
    seat: str
    user_id: str
    container: Any
    run_line: RunLine
    #: Sends a late result to the owner; None = the seat waits for every result.
    deliver: Optional[Callable[[str], Awaitable[Any]]] = None
    #: Writes one card answer into the owner thread (fail-open, the seat's own rule).
    record: Optional[Callable[[str], None]] = None
    #: True when ``run_line`` records the line's reply itself (the chat executor).
    line_records: bool = False
    #: True when the seat edits a card message in place (Telegram), so a builder
    #: step that only re-renders the card needs no new message.
    edits_in_place: bool = False


def _record(s: PressSeat, text: str) -> None:
    if s.record is None or not (text or "").strip():
        return
    try:
        s.record(text)
    except Exception:
        logger.debug("card press: record skipped (fail-open)", exc_info=True)


_CARD_ID = re.compile(r"^[0-9a-f]{10}$")


def call_card_tap(user_id: str, text: str) -> Optional[str]:
    """The tap a typed ``/approve <id>`` or ``/reject <id>`` stands for when ``<id>``
    names one of this owner's call cards (``/card_<id>_ok`` or ``/card_<id>_no``),
    else None (the seat reads the id as a queue item). Never raises."""
    words = (text or "").split()
    if len(words) != 2:
        return None
    verb = words[0].lower().split("@", 1)[0]
    if verb not in ("/approve", "/reject"):
        return None
    cid = words[1].strip().lower()
    if not _CARD_ID.match(cid):
        return None
    try:
        card = cards.store().get(cid)
    except Exception:
        logger.warning("card press: card store unreadable for %s", cid, exc_info=True)
        return None
    if card is None or card.kind != cards.KIND_CALL or str(card.user_id) != str(user_id):
        return None
    return cards.card_token(cid, "ok" if verb == "/approve" else "no")


def _made_child(card_id: str, user_id: str, reply: Any) -> bool:
    if getattr(reply, "card_id", None) is not None:
        return True
    try:
        return any(c.parent_id == card_id
                   for c in cards.store().open_cards(str(user_id), limit=50))
    except Exception:
        return False


async def _recover(container: Any, card: Any, text: str) -> None:
    """084 R13: a confirm that stopped at the extra check for a value that is NOT
    VERIFIED offers that step as a card. Made BEFORE the confirmed card finishes, so
    the open recovery card keeps the group from settling. Fail-open."""
    try:
        if card is None or card.kind != cards.KIND_QUOTE or not cards.failed_result(text):
            return
        with cards.lineage(card):
            ack = cards._ack_step_card(card.user_id, card.verb, list(card.typed or card.args),
                                       text)
        if ack is None:
            return
        await cards.push(container, card.user_id, ack, session_id=card.session_id)
    except Exception:
        logger.warning("card %s: recovery card skipped", getattr(card, "card_id", "?"),
                       exc_info=True)


def _finish(card_id: str, verb: str, text: str) -> None:
    """``cards.finish`` plus the one INFO line per finished card (079 W5)."""
    done = cards.finish(card_id, text)
    logger.info("card %s: finish verb=%s state=%s", card_id, verb,
                getattr(done, "state", "unchanged"))


async def press(s: PressSeat, cid: str, act: str) -> Any:
    """Press ``act`` on card ``cid`` for this seat. Returns the reply to show now
    (text, a ``CommandReply``, or None when the seat's in-place edit is the answer)."""
    p = cards.press(cid, act, s.user_id)
    logger.info("card %s: press act=%s verb=%s state=%s", cid, act,
                getattr(p.card, "verb", "") or getattr(p.card, "kind", "?"),
                getattr(p.card, "state", "none"))
    if p.call is not None and p.card is not None:
        from tools.controller.call_card_run import on_press
        report = None
        if s.deliver is not None:
            async def report(text: str) -> None:
                _record(s, text)
                await s.deliver(text)
        text = await on_press(p, seat=s.seat, container=s.container, deliver=report)
        _record(s, text)
        return text
    if p.pick is not None and p.card is not None:
        from surfaces.card_builder import advance
        step = advance(p.card, p.pick, s.user_id)
        if not step.run:
            if (s.edits_in_place and step.card is not None
                    and step.text == cards.render_text(step.card)):
                return None     # advanced: the edited card IS the answer
            return step.text
        p.run, act = step.run, "re"      # the last pick: run the verb's QUOTE
    if not p.run:
        # A cancel, an expired or decided card: the card's own answer.
        _record(s, p.reply)
        return p.reply
    card = p.card
    verb = p.run.split()[0]
    if act != "ok":
        # A refresh, or a proposal's quote: the quote becomes a fresh card of the
        # same session and group as this one (084 lineage).
        reply = None
        try:
            with cards.lineage(card):
                reply = await s.run_line(p.run, s.deliver)
        finally:
            if card is not None and not _made_child(card.card_id, s.user_id, reply):
                # No new card (a refused quote, an error): the line's reply is this
                # card's outcome, so its group can still settle.
                cards.note_result(card.card_id, reply_text(reply) or "no quote")
        return reply

    background = s.deliver is not None and cards.runs_in_background(verb, p.run)

    async def _deliver_and_finish(out: str) -> None:
        await _recover(s.container, card, out)
        _finish(card.card_id, verb, out)      # the stored result keeps every line
        from core.surfaces.money_report import owner_view
        await s.deliver(owner_view(out))
    _deliver_and_finish.stores_result = True

    try:
        with cards.lineage(card):
            reply = await s.run_line(p.run, _deliver_and_finish if background else s.deliver)
    except Exception as e:
        logger.error("card %s: the confirm line raised: %s", card.card_id, e, exc_info=True)
        _finish(card.card_id, verb, f"Command failed: {str(e)[:200]}")
        raise
    if not background:
        text = reply_text(reply)
        await _recover(s.container, card, text)
        _finish(card.card_id, verb, text)
        if not s.line_records:
            _record(s, text)
    return reply


__all__ = ["PressSeat", "call_card_tap", "press"]

