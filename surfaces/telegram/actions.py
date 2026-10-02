"""Telegram buttons for the action list (064 S2b F2): the one renderer.

Out: ``reply_markup_for(actions)`` turns a message's EXPLICIT actions into an
inline keyboard whose ``callback_data`` is the command itself (buttons are never
inferred from arbitrary text — ``core/surfaces/actions.py``). The text keeps the
same tappable tokens, so nothing is lost where a button is not shown.

In: ``accept_callback(bot, update)`` answers the button press (the spinner stops)
and returns a SYNTHETIC message update — the presser typing the command — which
then takes exactly the path a typed message takes (allowlist, dedup, tier, verb
dispatch). Two rules make that safe:

* the sender is ``callback_query.from`` — Telegram signs it; nothing in the
  button's payload names a user;
* ``callback_data`` is re-checked with ``is_action_command``: a payload that is
  not a tappable token or a verb is dropped, never routed as text.

A non-owner who presses an approve button is therefore refused exactly as if
they had typed ``/approve_p_…`` themselves.
"""
import logging
from typing import Any, Optional

from core.surfaces.actions import actions_for, is_action_command

logger = logging.getLogger(__name__)

#: Telegram's hard limit on ``callback_data``.
_CALLBACK_DATA_MAX = 64
#: Buttons per keyboard row.
_PER_ROW = 2


def keyboard_rows(actions: list) -> list:
    """``[[{"text", "callback_data"}, …], …]`` — the Bot API shape."""
    buttons = [{"text": a.label[:64], "callback_data": a.command}
               for a in actions
               if len(a.command.encode("utf-8")) <= _CALLBACK_DATA_MAX]
    return [buttons[i:i + _PER_ROW] for i in range(0, len(buttons), _PER_ROW)]


def reply_markup_for(actions: Optional[list]) -> Any:
    """The inline keyboard for these actions, or None when there are none."""
    rows = keyboard_rows(actions_for(actions))
    if not rows:
        return None
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=b["text"], callback_data=b["callback_data"])
             for b in row] for row in rows])
    except Exception:  # no aiogram (tests, a bare install): the raw Bot API dict
        return {"inline_keyboard": rows}


def callback_to_update(update: dict) -> Optional[dict]:
    """A ``callback_query`` update → the message update of the presser typing
    the command, or None (not a callback, or a payload that is not a command)."""
    cq = update.get("callback_query") if isinstance(update, dict) else None
    if not isinstance(cq, dict):
        return None
    data = cq.get("data")
    frm = cq.get("from")
    msg = cq.get("message") or {}
    chat = msg.get("chat")
    if not (isinstance(data, str) and isinstance(frm, dict) and isinstance(chat, dict)):
        return None
    if not is_action_command(data):
        logger.warning("telegram: callback payload %r is not a command — dropped",
                       data[:64])
        return None
    # ⚠️ No `message_id`: the press is not a message of its own, and reusing the
    # NOTICE's id aliased the owner-thread referent — a later quote-reply to the
    # notice resolved to "/approve_p_…" instead of the notice's text.
    synthetic = {"message_id": None, "callback_of": msg.get("message_id"),
                 "from": frm, "chat": chat, "date": msg.get("date"), "text": data}
    if msg.get("message_thread_id") is not None:
        synthetic["message_thread_id"] = msg["message_thread_id"]
    return {"update_id": update.get("update_id"), "message": synthetic}


async def accept_callback(bot: Any, update: dict) -> Optional[dict]:
    """Answer the press (best effort) and return the synthetic message update."""
    cq = update.get("callback_query") or {}
    answer = getattr(bot, "answer_callback_query", None)
    if cq.get("id") and callable(answer):
        try:
            await answer(cq["id"])
        except Exception as e:  # a lost spinner-stop never costs the command
            logger.debug("telegram: answer_callback_query failed: %s", e)
    return callback_to_update(update)
