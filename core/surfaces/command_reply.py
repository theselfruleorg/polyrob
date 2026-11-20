"""A command reply that knows WHERE it belongs (046 T2).

Lives in CORE, not in the Telegram tier, because the destination of a reply is a
routing concept and more than one surface reads a handler's return value
(`core/surfaces/inbound_webhook.py` sends it verbatim). A surface that does not
understand the shape still renders the right thing through :func:`reply_text`.

044 made every room COMMAND reply owner-only output: it goes to the room admin's
DM if an admin typed it, otherwise to the OWNER's DM. That is right for
`/groups` and `/paid`, whose output is configuration.

⚠️ It is wrong for a PURCHASE. A member's paid `/mute` produces a quote — a
price, a treasury address, a chain, an offer id — addressed to the member who
asked. Sent to the owner's DM, the payer sees silence and the rail is dead on
arrival. A member may also have no DM with the bot at all, so the room is the
only channel that certainly reaches them.

So a handler may now say where its own reply belongs. Anything that keeps
returning a plain ``str`` is unchanged — owner-only, exactly as before.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple
from core.rate_limit import SlidingWindowLimiter

_DENIAL_SENDERS = SlidingWindowLimiter(max_calls=1, window_seconds=60, max_keys=4096)
_DENIAL_TOTAL = SlidingWindowLimiter(max_calls=50, window_seconds=60, max_keys=1)


@dataclass(frozen=True)
class CommandReply:
    """``text`` plus its destination.

    ``to_room`` = deliver in the chat the command arrived in.
    ``media`` entries follow the `OutboundMessage.media` contract:
    ``{"kind": "image"|"document", "path": str, "caption": str | None}``.
    """
    text: str
    to_room: bool = False
    media: Tuple[dict, ...] = ()
    #: An action card this reply shows (``core.surfaces.cards``): a seat that
    #: renders buttons shows the card's, and remembers where it put them.
    card_id: Optional[str] = None
    #: Buttons for the next step (``core.surfaces.envelopes.Action``, built with
    #: ``core.surfaces.nav.actions``). A seat with no buttons shows the text only,
    #: so the text must still name each command.
    actions: Tuple[Any, ...] = ()


def with_actions(user_id: Optional[str], text: Any, buttons: Sequence[Any]) -> Any:
    """A verb reply plus its next-step buttons (``core.surfaces.nav.NavButton``s): a
    :class:`CommandReply` whose ``actions`` every seat that draws buttons shows (chat
    apps, the console). The text is unchanged, so a seat without buttons reads the
    same words; no buttons (or a non-text reply) returns *text* as it came."""
    if not buttons or not isinstance(text, str):
        return text
    from core.surfaces.nav import actions
    return CommandReply(text=text, actions=tuple(actions(user_id, buttons)))


def reply_text(value: Any) -> str:
    """The text of a handler's return value, whatever shape it came in."""
    if isinstance(value, CommandReply):
        return value.text
    return value if isinstance(value, str) else ("" if value is None else str(value))


def reply_media(value: Any) -> List[dict]:
    if isinstance(value, CommandReply) and value.media:
        return [dict(m) for m in value.media]
    return []


def reply_actions(value: Any) -> list:
    """The next-step buttons a handler's reply carries (none for a plain ``str``)."""
    if isinstance(value, CommandReply) and value.actions:
        return list(value.actions)
    return []


def reply_buttons(value: Any, command: Optional[str] = None, typed: str = "") -> list:
    """The buttons EVERY seat draws under a command reply, in one rule.

    A carded reply shows its card's buttons (from the card store, in the card's
    current state); otherwise the reply's explicit next-step actions; otherwise,
    for a verb that lists decisions (``/pending``, a bare ``/approve``), one
    button per decision token in the text. Never raises: a seat with no buttons
    still has the text, which names every command.
    """
    import logging
    _log = logging.getLogger(__name__)
    cid = getattr(value, "card_id", None) if isinstance(value, CommandReply) else None
    if cid:
        try:
            from core.surfaces import cards
            st = cards.existing_store()   # a read never mints an empty cards.db
            card = st.get(cid) if st is not None else None
            out = cards.card_actions(card) if card is not None else []
            if out:
                return out
        except Exception:
            _log.debug("card buttons unavailable (fail-open)", exc_info=True)
    try:
        from core.surfaces.actions import actions_for, actions_from_text, lists_decisions
        explicit = actions_for(reply_actions(value))
        if explicit:
            return explicit
        if lists_decisions(command, typed or ""):
            return actions_from_text(reply_text(value))
    except Exception:
        _log.debug("reply buttons unavailable (fail-open)", exc_info=True)
    return []


def reply_to_room(value: Any) -> bool:
    return bool(isinstance(value, CommandReply) and value.to_room)


def admit_inbound_reply(inbound, decision, reply) -> bool:
    """Shared polling/webhook destination gate; private command output never enters a room."""
    from core.surfaces.dispatcher import RouteKind
    source = inbound.identity.source
    room = source.chat_type != 'dm'
    if decision.kind == RouteKind.DENIED:
        if room or getattr(decision, 'silent', False):
            return False
        sender = (source.surface_id, inbound.identity.raw_user_id or inbound.identity.user_id)
        return _DENIAL_SENDERS.check(sender) and _DENIAL_TOTAL.check('all')
    if room and decision.kind in (RouteKind.COMMAND, RouteKind.STEER):
        return reply_to_room(reply)
    return True


__all__ = ["CommandReply", "reply_actions", "reply_media", "reply_text", "reply_to_room"]
