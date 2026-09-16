"""Telegram moderation calls — the first in this tree (046 Phase 1).

Duck-typed on the aiogram ``Bot`` the surface already holds, with no hard
aiogram import at module top, so every call here is testable with a fake bot and
no network (the rule `surfaces/telegram/surface.py` states).

⚠️ Every function returns a typed ``ModResult`` and NEVER raises. The caller
(`core/surfaces/room_actions.apply`) owes the payer a credit on failure, and it
can only do that if it is TOLD about the failure instead of being unwound by it.

⚠️ ``bot_rights`` fails CLOSED. An unreadable permission set is an EMPTY set, so
a sale is refused rather than made against a permission we merely hope we have.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Set

logger = logging.getLogger(__name__)

#: The permission flags 046's effect catalog names.
_RIGHT_FLAGS = ("can_restrict_members", "can_change_info",
                "can_delete_messages", "can_pin_messages")

#: A muted member: every send permission off.
_MUTED = dict(can_send_messages=False, can_send_audios=False,
              can_send_documents=False, can_send_photos=False,
              can_send_videos=False, can_send_video_notes=False,
              can_send_voice_notes=False, can_send_polls=False,
              can_send_other_messages=False, can_add_web_page_previews=False)

#: An unmuted member: EVERY permission back on.
#:
#: ⚠️ Enumerated in full, never derived from ``_MUTED``. ``restrictChatMember``
#: reads an OMITTED flag as False, so `{k: True for k in _MUTED}` silently
#: STRIPPED `can_invite_users`, `can_pin_messages`, `can_change_info`,
#: `can_manage_topics`, `can_react_to_messages` and `can_edit_tag` from a member
#: every time we "unmuted" them. `tests/unit/surfaces/telegram/test_moderation.py`
#: asserts this covers the whole `ChatPermissions` field set, so a new Bot API
#: permission fails a test instead of quietly removing a right.
_UNMUTED = dict(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True,
    can_send_other_messages=True, can_add_web_page_previews=True,
    can_invite_users=True, can_pin_messages=True, can_change_info=True,
    can_manage_topics=True, can_react_to_messages=True, can_edit_tag=True)

#: ⚠️ Telegram treats an ``until_date`` less than 30 s — or more than 366 d —
#: from now as FOREVER. The catalog caps at 30 d, so only the lower bound needs
#: care, but a forever-mute sold as an hour is exactly the kind of quiet
#: overreach this whole rail must not have.
_MIN_UNTIL_OFFSET_SEC = 31


@dataclass(frozen=True)
class ModResult:
    ok: bool
    reason: str = ""


def _permissions(mapping: dict) -> Any:
    """A ChatPermissions object, or a plain namespace when aiogram is absent
    (the unit-test path)."""
    try:
        from aiogram.types import ChatPermissions
        return ChatPermissions(**mapping)
    except Exception:
        return type("ChatPermissions", (), dict(mapping))()


def clamp_until(until_ts: int, *, now: float | None = None) -> int:
    """An ``until_date`` Telegram will read as a real deadline, not forever."""
    floor = int((now or time.time()) + _MIN_UNTIL_OFFSET_SEC)
    return max(int(until_ts), floor)


async def bot_rights(bot: Any, chat_id: Any) -> Set[str]:
    """The permission flags the BOT holds in this chat. Empty on any fault."""
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
    except Exception as e:
        logger.warning("moderation: cannot read bot rights in %s (%s) — reading "
                       "as NO rights", chat_id, e)
        return set()
    if getattr(member, "status", "") not in ("administrator", "creator"):
        return set()
    return {f for f in _RIGHT_FLAGS if bool(getattr(member, f, False))}


async def member_status(bot: Any, chat_id: Any, user_id: Any) -> str:
    """One member's status in this chat (``creator``/``administrator``/
    ``member``/``restricted``/``left``/``kicked``).

    ⚠️ RAISES on any fault, unlike :func:`bot_rights`, and the difference is
    deliberate: the caller (`room_actions._target_protection`) reads a raised
    probe as PROTECTED, so an unreadable status refuses the sale instead of
    letting an administrator be targeted because we could not look.
    """
    member = await bot.get_chat_member(chat_id, int(user_id))
    return str(getattr(member, "status", "") or "")


async def _call(op: str, fn) -> ModResult:
    try:
        await fn()
        return ModResult(True)
    except Exception as e:
        logger.warning("moderation: %s failed (%s)", op, e)
        return ModResult(False, str(e))


async def restrict_member(bot, chat_id, user_id, *, until_ts: int) -> ModResult:
    return await _call("restrict", lambda: bot.restrict_chat_member(
        chat_id=chat_id, user_id=int(user_id),
        permissions=_permissions(_MUTED), until_date=clamp_until(until_ts)))


async def unrestrict_member(bot, chat_id, user_id) -> ModResult:
    return await _call("unrestrict", lambda: bot.restrict_chat_member(
        chat_id=chat_id, user_id=int(user_id),
        permissions=_permissions(_UNMUTED), until_date=0))


async def ban_member(bot, chat_id, user_id, *, until_ts: int) -> ModResult:
    return await _call("ban", lambda: bot.ban_chat_member(
        chat_id=chat_id, user_id=int(user_id), until_date=clamp_until(until_ts)))


async def unban_member(bot, chat_id, user_id) -> ModResult:
    return await _call("unban", lambda: bot.unban_chat_member(
        chat_id=chat_id, user_id=int(user_id), only_if_banned=True))


# ⚠️ `set_slow_mode` was removed with the `slowmode` catalog verb (046 phase 2).
# `Bot.set_chat_slow_mode_delay` does not exist on aiogram and there is no Bot
# API method behind it, so every slowmode sale took money and wrote a credit.


__all__ = ["ModResult", "ban_member", "bot_rights", "clamp_until",
           "member_status", "restrict_member", "unban_member",
           "unrestrict_member"]
