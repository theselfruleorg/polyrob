"""101: what a person in a room can do, and the note for an admin's refused DM.

Extracted from ``harness.py``. A member and a room admin each read their own
list, never the owner's verb catalog.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: 101 R10: claims nothing, because the undelivered reply may be a refusal.
ADMIN_DM_NOTE = ("I could not send your reply in a direct message. "
                 "Start a chat with me, then try again.")

#: What a room admin may run in this room, whatever the owner granted.
_ADMIN_VERBS = (
    "/groups mode here <mention|active|listen|off> — when I answer here",
    "/groups tail here [n] — the last lines of this room",
    "/groups roles here — who holds a role here",
    "/groups role here blocked — in reply: block a member from me",
    "/mute <duration> — silence me here; in reply: mute that member",
    "/unmute · /ban <duration> · /unban — in reply to the member",
    "/paid status · /paid offers · /paid cancel <id> — this room's paid actions",
    "/cancel · /new — stop or restart this room's session",
)


def _is_owner_groups_line(update: dict) -> bool:
    """044 C6: is this the OWNER's `/groups …` line? (raw update, pre-routing.)

    The one thing `_room_is_allowed` admits from a room the owner has NOT
    allowlisted yet — because `/groups allow here` is what CREATES that row.
    Both halves must hold:

    * the sender's raw Telegram id is the configured owner's. Resolved through
      `owner_surface_alias`, the SAME seam `surfaces/telegram/inbound.py` uses to
      map an authenticated owner onto the principal, so this gate and the
      dispatcher's own `is_room_owner` check can never disagree about who the
      owner is. A non-owner (or an unbound owner, or an unconfigured owner
      telegram id) is None -> False.
    * the first token, `@botname` suffix stripped, is exactly `/groups`. Read
      from `text` only: a caption, a voice note and a forwarded body are not the
      documented path and must keep costing nothing in an unlisted room.

    Never raises — a probe fault reads as "not the owner" (the room stays shut).
    """
    try:
        from surfaces.telegram.harness import _tg_message, _tg_user_id
        msg = _tg_message(update)
        from surfaces.telegram.inbound import is_forwarded
        if is_forwarded(msg):  # H06: a forwarded `/groups` is not the owner's verb
            return False
        text = str(msg.get("text") or "").strip()
        if not text:
            return False
        if text.split(" ", 1)[0].lower().split("@", 1)[0] != "/groups":
            return False
        from core.instance import owner_surface_alias
        return owner_surface_alias(_tg_user_id(update), "telegram") is not None
    except Exception as e:
        logger.debug("telegram owner-/groups probe failed (not the owner): %s", e)
        return False


def admin_elsewhere(result: Any) -> Any:
    """`/admin` where it does not run: one line, in the room it was asked in, no turn."""
    from core.surfaces.command_reply import CommandReply
    from core.surfaces.room_grants import ADMIN_ELSEWHERE_TEXT
    from core.surfaces.room_keys import is_group_session_key
    key = result.decision.session_key if result is not None else ""
    return CommandReply(ADMIN_ELSEWHERE_TEXT, to_room=is_group_session_key(key))


def topic_of(result: Any, reply: Any, room_chat_id: Any) -> dict:
    """A room line stays in the forum topic it answers."""
    from core.surfaces.command_reply import reply_to_room
    from surfaces.telegram.surface import thread_id_from_session_key
    if not (reply_to_room(reply) and room_chat_id):
        return {}
    return {"thread_id": thread_id_from_session_key(result.decision.session_key)}


def admin_topic(result: Any) -> dict:
    """An `/admin` turn's late line (a failed run) goes to the topic it answers."""
    from core.surfaces.dispatcher import RouteKind
    from surfaces.telegram.surface import thread_id_from_session_key
    if result.decision.kind != RouteKind.ROOM_ADMIN_TURN:
        return {}
    return {"thread_id": thread_id_from_session_key(result.decision.session_key)}


def cold_start_held(result: Any) -> bool:
    """031 owner pause: the warm room path holds on this, so a cold start does too."""
    from core.autonomy_control import allows
    decision = allows("resume_session")
    if not decision.allowed:
        logger.warning("room cold start HELD for %s (%s)", result.decision.session_key,
                       decision.reason)
    return not decision.allowed


async def tell_admin_run_failed(notice_key: Any, deliver: Any) -> None:
    """The failure notice went to the owner; the admin who asked also hears, neutrally."""
    if ":admin:" in str(notice_key or ""):
        await deliver("Your admin task did not finish. Try again later.")


def room_admin_help(task_agent: Any, result: Any) -> str:
    """A room admin's `/help`: the verbs of this role, and this room's `/admin`."""
    src = getattr(result.inbound.identity, "source", None)
    surface = str(getattr(src, "surface_id", "") or "telegram")
    chat_id = str(getattr(src, "chat_id", "") or "")
    lines = ["In this room you can run:"]
    lines.extend(f"• {line}" for line in _ADMIN_VERBS)
    bundles = _granted_bundles(task_agent, surface, chat_id)
    if bundles:
        lines.append("• /admin <task> — run a task with this room's admin tools: "
                     + ", ".join(bundles))
        lines.append("• /admin stop · /admin new — stop or restart your admin session")
    return "\n".join(lines)


def _granted_bundles(task_agent: Any, surface: str, chat_id: str) -> tuple:
    try:
        from core.runtime_paths import data_dir_or_home
        from core.surfaces.chat_policy import load_for_chat
        from core.surfaces.room_grants import live_admin_bundles
        cfg = getattr(getattr(task_agent, "container", None), "config", None)
        return live_admin_bundles(load_for_chat(
            data_dir_or_home(getattr(cfg, "data_dir", None)), surface, chat_id))
    except Exception as e:
        logger.warning("room help: admin tools unreadable (%s)", e)
        return ()


def room_help_reply(task_agent: Any, result: Any) -> Optional[Any]:
    """The `/help` of a person in a room, or None for the owner and a DM.

    The role comes from ``room_turn.room_role``, the ONE resolver: it reads the
    role the router stamped, so this can never disagree with the tier that
    admitted the line.
    """
    from core.surfaces.room_keys import is_group_session_key
    if not is_group_session_key(result.decision.session_key):
        return None
    from surfaces.telegram.room_turn import room_role
    role = room_role(result)
    if role == "owner":
        return None
    if role == "admin":
        return room_admin_help(task_agent, result)
    from core.surfaces.command_reply import CommandReply
    from core.surfaces.room_actions import render_member_help
    from surfaces.telegram.harness import _agent_name
    src = getattr(result.inbound.identity, "source", None)
    surface = str(getattr(src, "surface_id", "") or "telegram")
    chat_id = str(getattr(src, "chat_id", "") or "")
    try:
        text = render_member_help(getattr(task_agent, "container", None),
                                  surface=surface, chat_id=chat_id,
                                  agent_name=_agent_name())
    except Exception as e:
        # Fail-open to a true sentence, never to the owner catalog: a member
        # must not be handed the admin verb list because a store was unreadable.
        logger.warning("telegram: member help render failed (%s)", e)
        text = ("I could not read this room's settings just now — try again in "
                "a moment.")
    return CommandReply(text, to_room=True)


def _dm_refused(error: Exception) -> bool:
    """Telegram refuses a DM to someone who never started the bot."""
    text = f"{type(error).__name__} {error}".lower()
    return "forbidden" in text or "chat not found" in text


async def admin_reply_failed(bot: Any, result: Any, room_chat_id: Any, reply: Any,
                             error: Exception) -> bool:
    """True when this failed send was an ADMIN's private reply, now handled.

    ⚠️ ``/missed`` is the owner's verb and an admin cannot reach it, so the reply
    is never held there. A refused DM posts :data:`ADMIN_DM_NOTE`, with no output
    in it. Any other fault is logged. Never raises.
    """
    from core.surfaces.command_reply import reply_to_room
    if getattr(result.inbound.identity, "chat_role", None) != "admin" or reply_to_room(reply):
        return False
    logger.warning("telegram: an admin's command reply was not delivered: %s", error)
    if room_chat_id and _dm_refused(error):
        try:
            from surfaces.telegram.harness import _send_telegram_text
            await _send_telegram_text(bot, room_chat_id, ADMIN_DM_NOTE)
        except Exception as e:
            logger.warning("telegram: the admin DM note was not sent: %s", e)
    return True
