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
from typing import Any, List, Optional, Tuple


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


def reply_text(value: Any) -> str:
    """The text of a handler's return value, whatever shape it came in."""
    if isinstance(value, CommandReply):
        return value.text
    return value if isinstance(value, str) else ("" if value is None else str(value))


def reply_media(value: Any) -> List[dict]:
    if isinstance(value, CommandReply) and value.media:
        return [dict(m) for m in value.media]
    return []


def reply_to_room(value: Any) -> bool:
    return bool(isinstance(value, CommandReply) and value.to_room)


__all__ = ["CommandReply", "reply_media", "reply_text", "reply_to_room"]
