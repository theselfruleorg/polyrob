"""Discord buttons for the action list (order 0016).

Out: :func:`render_components` turns a message's EXPLICIT ``OutboundMessage.actions``
into action rows of buttons whose ``custom_id`` is the command itself (buttons are
never inferred from text — ``core/surfaces/actions.py``). The message text keeps the
same tappable tokens, so nothing is lost where a button is not shown.

In: :func:`parse_interaction` turns an ``INTERACTION_CREATE`` button press into the
PRESSER typing the command — an ordinary :class:`InboundMessage` on the path a typed
message takes (dedup, tier, verb dispatch). Two rules make it safe:

* the sender is ``member.user.id`` (guild) or ``user.id`` (DM), which the platform
  authenticates; nothing in ``custom_id`` names a user;
* ``custom_id`` is re-checked with ``is_action_command``: a payload that is not a
  tappable token or a verb is DROPPED, never routed as text.

A non-owner who presses an approve button is refused exactly as if they had typed it.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from core.surfaces.actions import actions_for, is_action_command
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource

logger = logging.getLogger(__name__)

INTERACTION_MESSAGE_COMPONENT = 3
COMPONENT_BUTTON = 2
_ROW_MAX, _ROWS_MAX = 5, 5
_CUSTOM_ID_MAX, _LABEL_MAX = 100, 80
#: Action.style → Discord button style (1 primary, 2 secondary, 4 danger).
_STYLE = {"primary": 1, "default": 2, "danger": 4}


def render_components(actions: Optional[list]) -> List[dict]:
    """Action rows for these actions, or ``[]`` when there are none."""
    buttons = [{"type": COMPONENT_BUTTON, "style": _STYLE.get(a.style, 2),
                "label": a.label[:_LABEL_MAX], "custom_id": a.command}
               for a in actions_for(actions)
               if len(a.command) <= _CUSTOM_ID_MAX]
    buttons = buttons[:_ROW_MAX * _ROWS_MAX]
    return [{"type": 1, "components": buttons[i:i + _ROW_MAX]}
            for i in range(0, len(buttons), _ROW_MAX)]


def parse_interaction(d: Any, user_directory: Any = None) -> Optional[InboundMessage]:
    """A button-press ``INTERACTION_CREATE`` → the presser typing the command, or
    None (not a button press, no presser/channel, or a payload that is no command)."""
    if not isinstance(d, dict) or d.get("type") != INTERACTION_MESSAGE_COMPONENT:
        return None
    data = d.get("data") if isinstance(d.get("data"), dict) else {}
    if data.get("component_type") != COMPONENT_BUTTON:
        return None
    command = data.get("custom_id")
    member = d.get("member") if isinstance(d.get("member"), dict) else {}
    user = member.get("user") if isinstance(member.get("user"), dict) else d.get("user")
    user = user if isinstance(user, dict) else {}
    author = str(user.get("id") or "")
    channel_id = str(d.get("channel_id") or "")
    if not (author and channel_id and isinstance(command, str)):
        return None
    if not is_action_command(command):
        logger.warning("discord: button payload %r is not a command — dropped", command[:64])
        return None
    source = SessionSource(surface_id="discord", chat_id=channel_id,
                           chat_type="group" if d.get("guild_id") else "dm")
    user_id = None
    try:
        from core.instance import owner_surface_alias
        user_id = owner_surface_alias(author, "discord")
    except Exception:
        user_id = None
    if not user_id and user_directory is not None:
        try:
            user_id = user_directory.resolve_internal(author, "discord")
        except Exception:
            user_id = None
    iid = str(d.get("id") or "")
    return InboundMessage(
        text=command,
        identity=Identity(user_id=user_id or f"u_discord_{author}", source=source,
                          raw_user_id=author, display_name=user.get("username")),
        # A press is not a message of its own: dedup on the interaction id.
        idempotency_key=f"card:{iid}" if iid else None,
        raw={"interaction_id": iid},   # the token stays out of the envelope
        mentions_bot=True,             # a press on the bot's own message addresses the bot
    )
