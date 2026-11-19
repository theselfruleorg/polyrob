"""Slack Block Kit buttons for the action list (order 0017).

Out: :func:`render_blocks` turns a message's EXPLICIT ``OutboundMessage.actions``
into a section block (the text) plus actions blocks of buttons whose ``value`` is
the command itself (buttons are never inferred from text — ``core/surfaces/actions.py``).
The message ``text`` keeps the same tappable tokens, so nothing is lost where a
button is not shown.

In: :func:`parse_block_actions` turns a ``block_actions`` interactive payload into
the PRESSER typing the command — an ordinary :class:`InboundMessage` on the path a
typed message takes (dedup, tier, verb dispatch). Two rules make it safe:

* the sender is ``user.id``, which the platform authenticates; nothing in ``value``
  names a user;
* ``value`` is re-checked with ``is_action_command``: a payload that is not a
  tappable token or a verb is DROPPED, never routed as text.

A non-owner who presses an approve button is refused exactly as if they had typed it.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from core.surfaces.actions import actions_for, is_action_command
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource

logger = logging.getLogger(__name__)

BLOCK_ACTIONS = "block_actions"
#: Slack: a section's text <= 3000 chars, an actions block <= 25 elements, a
#: button value <= 2000 chars, a button label <= 75.
SECTION_MAX = 3000
_ROW_MAX, _BUTTONS_MAX = 5, 25
_VALUE_MAX, _LABEL_MAX = 2000, 75
_STYLE = {"primary": "primary", "danger": "danger"}   # "default" = no style key


def render_blocks(text: str, actions: Optional[list]) -> List[dict]:
    """Blocks for ``text`` plus these actions, or ``[]`` when there are no buttons.
    ``text`` must already fit :data:`SECTION_MAX`."""
    acts = [a for a in actions_for(actions) if len(a.command) <= _VALUE_MAX]
    acts = acts[:_BUTTONS_MAX]
    if not acts:
        return []
    blocks: List[dict] = [{"type": "section",
                           "text": {"type": "plain_text", "text": text or " ",
                                    "emoji": False}}]
    for i in range(0, len(acts), _ROW_MAX):
        elements = []
        for j, a in enumerate(acts[i:i + _ROW_MAX], start=i):
            el = {"type": "button", "action_id": f"act_{j}",
                  "text": {"type": "plain_text", "text": a.label[:_LABEL_MAX]},
                  "value": a.command}
            if a.style in _STYLE:
                el["style"] = _STYLE[a.style]
            elements.append(el)
        blocks.append({"type": "actions", "elements": elements})
    return blocks


def parse_block_actions(body: Any, user_directory: Any = None) -> Optional[InboundMessage]:
    """A ``block_actions`` payload -> the presser typing the command, or None (not a
    button press, no presser/channel, or a payload that is no command)."""
    if not isinstance(body, dict) or body.get("type") != BLOCK_ACTIONS:
        return None
    acts = [a for a in (body.get("actions") or [])
            if isinstance(a, dict) and a.get("type") == "button"]
    user = body.get("user") if isinstance(body.get("user"), dict) else {}
    chan = body.get("channel") if isinstance(body.get("channel"), dict) else {}
    author = str(user.get("id") or "")
    channel = str(chan.get("id") or "")
    if not (acts and author and channel):
        return None
    command = acts[0].get("value")
    if not isinstance(command, str) or not is_action_command(command):
        logger.warning("slack: button payload %r is not a command — dropped",
                       str(command)[:64])
        return None
    from surfaces.slack.socket_mode import _internal_user_id
    msg = body.get("message") if isinstance(body.get("message"), dict) else {}
    thread = str(msg.get("thread_ts") or "") or None
    source = SessionSource(surface_id="slack", chat_id=channel,
                           chat_type="dm" if channel.startswith("D") else "group",
                           thread_id=thread)
    trigger = str(body.get("trigger_id") or "")
    return InboundMessage(
        text=command,
        identity=Identity(user_id=_internal_user_id(author, user_directory),
                          source=source, raw_user_id=author,
                          display_name=str(user.get("username") or "") or None),
        # A press is not a message of its own: dedup on the trigger id.
        idempotency_key=f"card:{trigger}" if trigger else None,
        raw={"trigger_id": trigger},   # response_url and tokens stay out of the envelope
        mentions_bot=True,             # a press on the bot's own message addresses the bot
    )
