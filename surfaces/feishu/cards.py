"""Feishu / Lark buttons for the action list (064 order 0005).

Out: :func:`render_card` turns a message's EXPLICIT ``OutboundMessage.actions``
into an interactive card (one ``action`` row of buttons); each button's
``value`` is ``{"command": <the command itself>}``. Buttons are never inferred
from arbitrary text (``core/surfaces/actions.py``); the message text keeps the
same tappable tokens, so nothing is lost where the card does not render.

In: :func:`parse_card_action` turns a ``card.action.trigger`` callback into the
OPERATOR typing the command — an ordinary :class:`InboundMessage` that takes the
path a typed message takes (dedup, tier, verb dispatch). Two rules make it safe:

* the sender is ``event.operator.open_id`` — the platform authenticates the
  presser (the long connection is the app's own authenticated socket; the
  webhook body is decrypted/signed or token-checked before this runs). Nothing
  inside the button's ``value`` names a user;
* ``value.command`` is re-checked with ``is_action_command``: a payload that is
  not a tappable token or a verb is DROPPED, never routed as text.

A non-owner who presses an approve button is therefore refused exactly as if
they had typed ``/approve_p_…`` themselves.

The callback does not say whether its chat is a DM or a group. The harness
remembers the chat type of every chat an inbound message came from; an unknown
chat is treated as a GROUP (the narrower tier) with the bot addressed.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.surfaces.actions import actions_for, is_action_command
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource

logger = logging.getLogger(__name__)

EVENT_TYPE = "card.action.trigger"

#: Action.style → the card button type.
_BUTTON_TYPE = {"primary": "primary", "danger": "danger", "default": "default"}

#: The card's header line (the notice text itself arrives as its own message).
CARD_TITLE = "Choose"


def card_buttons(actions: Optional[list]) -> List[Dict[str, Any]]:
    return [{"tag": "button",
             "text": {"tag": "plain_text", "content": a.label[:40]},
             "type": _BUTTON_TYPE.get(a.style, "default"),
             "value": {"command": a.command}}
            for a in actions_for(actions)]


def render_card(actions: Optional[list]) -> Optional[Dict[str, Any]]:
    """The interactive card for these actions, or None when there are none."""
    buttons = card_buttons(actions)
    if not buttons:
        return None
    return {"config": {"wide_screen_mode": True},
            "elements": [{"tag": "div",
                          "text": {"tag": "plain_text", "content": CARD_TITLE}},
                         {"tag": "action", "actions": buttons}]}


def parse_card_action(payload: dict, *, user_directory: Any = None,
                      chat_types: Optional[Dict[str, str]] = None) -> Optional[InboundMessage]:
    """A ``card.action.trigger`` callback → the operator typing the command,
    or None (not a card callback, no operator, or a payload that is not a
    command)."""
    if not isinstance(payload, dict):
        return None
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    if header.get("event_type") != EVENT_TYPE:
        return None
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    operator = event.get("operator") if isinstance(event.get("operator"), dict) else {}
    action = event.get("action") if isinstance(event.get("action"), dict) else {}
    context = event.get("context") if isinstance(event.get("context"), dict) else {}
    author = str(operator.get("open_id") or "")
    chat_id = str(context.get("open_chat_id") or "")
    value = action.get("value")
    command = value.get("command") if isinstance(value, dict) else None
    if not (author and chat_id and isinstance(command, str)):
        return None
    if not is_action_command(command):
        logger.warning("feishu: card payload %r is not a command — dropped", command[:64])
        return None

    chat_type = (chat_types or {}).get(chat_id) or "group"
    source = SessionSource(surface_id="feishu", chat_id=chat_id,
                           chat_type="dm" if chat_type == "dm" else "group")
    user_id = None
    if user_directory is not None:
        try:
            user_id = user_directory.resolve_internal(author, "feishu")
        except Exception:
            logger.debug("feishu: user directory resolve failed", exc_info=True)
            user_id = None
    event_id = str(header.get("event_id") or "")
    return InboundMessage(
        text=command,
        identity=Identity(user_id=user_id or f"u_feishu_{author}", source=source,
                          raw_user_id=author),
        # A press is not a message of its own: the dedup key is the callback's
        # event id (the platform redelivers an unanswered callback).
        idempotency_key=f"card:{event_id}" if event_id else None,
        raw=payload,
        mentions_bot=True,       # a press on the bot's own card addresses the bot
    )
