"""The Telegram room transport, and the room verbs' old Telegram home (proposal 125 S14).

The decision body of `/groups`, `/mute`, `/unmute`, `/ban`, `/unban` and `/paid` lives
in ``cron/owner_rooms.py``, below the seats; this module re-exports its names. What
stays here is what only Telegram can do — the Bot API calls
(``surfaces/telegram/moderation.py`` through the ONE adapter
``surfaces/telegram/room_moderator.py``), the Bot resolver and the raw-update reads —
as the :class:`TelegramRoomTransport` that ``core.surfaces.room_transport`` hands the
body for a ``telegram`` room. Importing this module registers it (the harness does at
start, through ``install_room_moderator``).
"""
import logging
from typing import Any, List, Optional, Tuple

from core.surfaces.room_transport import register_room_transport

logger = logging.getLogger(__name__)


def _bot(task_agent: Any):
    """The live aiogram Bot, or None. One resolver for every seat here.

    ⚠️ The first two lookups are the TEST shape and were the only ones here
    until 2026-09-15. Nothing in production sets either: `harness.py` sets
    ``self.bot`` on the HARNESS, and the object that reaches these seats is the
    TaskAgent. So `_bot` was permanently None in prod — `_target_status_fn`
    raised on every call, `_target_protection` read that as PROTECTED (its
    correct fail-closed default), and a plain member buying a ban on another
    plain member got "That person cannot be targeted here". Observed live in
    Playground Env at 17:43:49. `_rights_fn` shares this resolver and would
    have failed one step later for the same reason.

    The container lookups are the production path: `TelegramHarness.start`
    registers the `room_moderator` (with the surface) and the surface itself,
    and both already hold the bot. Resolved LAZILY per call, like
    `RoomModerator._resolve_bot` — the surface outlives a reconnect and the bot
    object does not.

    ⚠️ Returns None rather than a stub when nothing is reachable. A stub would
    make an unreadable member status look like a readable "not an admin", which
    is exactly how a chat administrator would become buyable.
    """
    found = (getattr(task_agent, "bot", None)
             or getattr(getattr(task_agent, "surface", None), "_bot", None))
    if found is not None:
        return found
    container = getattr(task_agent, "container", None)
    if container is None:
        return None
    try:
        mod = container.get_service("room_moderator")
        if mod is not None:
            resolve = getattr(mod, "_resolve_bot", None)
            found = resolve() if callable(resolve) else None
            if found is not None:
                return found
        reg = container.get_service("surface_registry")
        surface = reg.get("telegram") if reg is not None else None
        return (getattr(surface, "_bot", None)
                or getattr(surface, "bot", None)) or None
    except Exception as e:
        logger.debug("telegram room transport: bot unresolved from the container (%s)", e)
        return None


def _tg_reply_target(raw: Any) -> Optional[Tuple[str, str, bool]]:
    """``(raw_user_id, display_name, is_bot)`` of the message a raw update replied to.

    ⚠️ 101 R3: every message in a forum topic "replies" to the topic root, so a
    line with no reply would read as a reply to whoever opened the topic.
    """
    from surfaces.telegram.harness import _tg_message
    msg = _tg_message(raw or {}) or {}
    reply = msg.get("reply_to_message") or {}
    if reply.get("forum_topic_created") is not None:
        return None
    frm = reply.get("from") or {}
    uid = frm.get("id")
    if not uid:
        return None
    name = frm.get("first_name") or frm.get("username") or str(uid)
    return str(uid), str(name), bool(frm.get("is_bot"))


def _tg_room_title(raw: Any) -> str:
    """The chat title of a raw update (an object or a dict). Never raises."""
    try:
        msg = getattr(raw, "message", None)
        if msg is None and isinstance(raw, dict):
            msg = raw.get("message")
        chat = getattr(msg, "chat", None)
        if chat is None and isinstance(msg, dict):
            chat = msg.get("chat")
        title = getattr(chat, "title", None)
        if title is None and isinstance(chat, dict):
            title = chat.get("title")
        return str(title or "").strip()
    except Exception as e:
        logger.debug("telegram room transport: chat title probe failed: %s", e)
        return ""


class TelegramRoomTransport:
    """``core.surfaces.room_transport.RoomTransport`` over the live aiogram Bot.

    The Bot is resolved LAZILY per call (:func:`_bot`): the surface outlives a
    reconnect and the Bot object does not."""

    def __init__(self, task_agent: Any = None) -> None:
        self._agent = task_agent

    def connected(self) -> bool:
        return _bot(self._agent) is not None

    def is_admin_owner(self, user_id: str) -> bool:
        from surfaces.telegram.harness import _is_admin_owner
        return _is_admin_owner(user_id)

    def reply_target(self, raw: Any) -> Optional[Tuple[str, str, bool]]:
        return _tg_reply_target(raw)

    def room_title(self, raw: Any) -> str:
        return _tg_room_title(raw)

    async def rights(self, chat_id: str) -> set:
        """EMPTY on any fault (``moderation.bot_rights`` fails closed)."""
        bot = _bot(self._agent)
        if bot is None:
            return set()
        from surfaces.telegram.moderation import bot_rights
        return set(await bot_rights(bot, chat_id) or ())

    async def member_status(self, chat_id: str, user_id: str) -> str:
        """RAISES on any fault, so the caller reads the target as PROTECTED."""
        bot = _bot(self._agent)
        if bot is None:
            raise RuntimeError("no chat connection to read member status with")
        from surfaces.telegram.moderation import member_status
        return await member_status(bot, chat_id, user_id)

    async def apply(self, row: Any, effect: Any, until_ts: int) -> Any:
        """The ONE verb -> Bot API adapter: the container's ``room_moderator``
        (the one the settlement path uses), else one built over this Bot."""
        from surfaces.telegram.room_moderator import RoomModerator
        moderator = None
        container = getattr(self._agent, "container", None)
        if container is not None:
            try:
                moderator = container.get_service("room_moderator")
            except Exception:
                moderator = None
        if moderator is None:
            moderator = RoomModerator(getattr(self._agent, "surface", None),
                                      bot=_bot(self._agent))
        return await moderator(row, effect, until_ts)

    async def suggest_admins(self, chat_id: str) -> Optional[List[dict]]:
        """Telegram's admin list through the registered harness, or None (no harness)."""
        container = getattr(self._agent, "container", None)
        harness = container.get_service("telegram_harness") if container is not None else None
        if harness is None or not hasattr(harness, "suggest_admins"):
            return None
        return await harness.suggest_admins(chat_id)


register_room_transport("telegram", TelegramRoomTransport)


# The decision body, re-exported under its old Telegram names (callers and tests).
from cron.owner_rooms import (  # noqa: E402,F401
    _OWNER_ONLY_TEXT, _OWNER_OR_ADMIN_TEXT, _PAID_MEMBER_VERBS, _PAID_OWNER_ONLY_VERBS,
    _PAID_OWNER_OR_ADMIN_VERBS, _PAID_VERBS, ROOM_FACING_REFUSALS, _apply_free,
    _member_may_use, _mint_fn, _moderation_reply, _offer_card, _owner_uid, _paid_offer,
    _perform, _quoter, _rail_check_fn, _reply_target, _rights_fn, _target_status_fn,
    ban_reply, groups_reply, mute_reply, paid_reply, refusal_reply, split_pay_flag,
    unban_reply, unmute_reply,
)

__all__ = ["ROOM_FACING_REFUSALS", "TelegramRoomTransport", "ban_reply", "groups_reply",
           "mute_reply", "paid_reply", "refusal_reply", "split_pay_flag", "unban_reply",
           "unmute_reply"]
