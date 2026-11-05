"""101: `/admin <task>` — the room+admin session (proposal 101 §3, Phase 3).

The router only proved the speaker is the owner or a room admin. Whether a tool
runs is decided at each call by the room gate (``core.surfaces.room_grants``), so
nothing here widens anything.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

OFF_TEXT = "No admin tools are on in this room."
CAP_TEXT = "The daily limit for /admin is reached in this room."
HELD_TEXT = "The owner has paused the agent's work, so this did not run."
WAIT_TEXT = "Wait a moment, then send /admin again."
ROOM_CAP_TEXT = "This room reached its reply limit for now, so /admin did not run."
BUSY_TEXT = "Your admin task has too many lines waiting. Send this again in a moment."
START_FAILED_TEXT = "I could not start your admin task. Try again later."
UNREADABLE_TEXT = "I could not read this room's settings, so /admin is off. Tell the owner."

#: A session in one of these has a run in progress, or one about to start.
_ACTIVE_STATUSES = ("created", "running", "resumed")


def _room_line(text: str) -> Any:
    from core.surfaces.command_reply import CommandReply
    return CommandReply(text, to_room=True)


def _reply_author(result: Any) -> Optional[dict]:
    """The person this line replies to. Reads Telegram's own field, so a forum topic's
    implicit reply is no reply (``group_ops._reply_target``). A reply to the agent's own
    message names nobody: the moderation pin has no person to hold it to."""
    if getattr(getattr(result.inbound, "reply_ref", None), "from_self", False):
        return None
    from surfaces.telegram.group_ops import _reply_target
    found = _reply_target(result)
    return {"uid": found[0], "name": found[1]} if found else None


def _session_active(task_agent: Any, session_id: str) -> bool:
    """A store fault reads as active: a line then moves no pin, and a stop still tries."""
    try:
        info = task_agent.session_manager.get_session_info(session_id) or {}
        return info.get("status") in _ACTIVE_STATUSES
    except Exception as e:
        logger.warning("/admin: session status unread for %s: %s", session_id, e)
        return True


@dataclass
class AdminTurn:
    """What a cold start needs beyond an ordinary room session."""
    stamp: dict
    line: str
    author: Optional[dict]
    outside_words: bool = False

    def opened(self, task_agent: Any, session_id: str) -> None:
        from core.surfaces.room_seat import note_admin_line
        note_admin_line(task_agent.get_orchestrator(session_id), self.line, self.author,
                        self.outside_words)


async def _control(task_agent: Any, word: str, decision: Any) -> Any:
    """`/admin stop` and `/admin new`, on the speaker's own admin session only."""
    sid = decision.session_id
    stopped = False
    if sid and _session_active(task_agent, sid):
        try:
            stopped = bool(await task_agent.cancel_session_by_id(sid, force=True))
        except Exception as e:
            logger.warning("/admin %s: cancel failed: %s", word, e)
            return _room_line("I could not stop your admin task; it may still be running.")
    if word == "new":
        try:
            task_agent.unbind_chat(decision.session_key)
        except Exception as e:
            logger.debug("/admin new: unbind failed: %s", e)
        return _room_line("Your next /admin starts fresh.")
    return _room_line("Stopped your admin task." if stopped
                      else "No admin task of yours is running in this topic.")


async def handle(task_agent: Any, result: Any, spawn: Any, deliver: Any, fetch_media: Any) -> Any:
    from surfaces.telegram import harness as h
    from surfaces.telegram.room_reconcile import reconcile_room_toolset
    from surfaces.telegram.room_turn import _room_policy, room_absorbs_media
    from core.surfaces.room_grants import live_admin_bundles
    from core.surfaces.room_seat import note_admin_line

    decision, inbound = result.decision, result.inbound
    if decision.reason == "wait":
        return _room_line(WAIT_TEXT)
    if decision.reason == "room_cap":
        return _room_line(ROOM_CAP_TEXT)
    identity = inbound.identity
    source = identity.source
    surface, chat_id = str(source.surface_id or ""), str(source.chat_id or "")
    container = getattr(task_agent, "container", None)
    parts = (inbound.text or "").split(None, 1)
    task = parts[1].strip() if len(parts) > 1 else ""
    if task.lower() in ("stop", "new"):
        return await _control(task_agent, task.lower(), decision)

    policy = _room_policy(container, surface, chat_id)
    if getattr(policy, "unreadable", False):
        return _room_line(UNREADABLE_TEXT)
    bundles = live_admin_bundles(policy)
    if not bundles:
        return _room_line(OFF_TEXT)
    if not task:
        return _room_line("Usage: /admin <task>. Admin tools here: " + ", ".join(bundles)
                          + ". /admin stop · /admin new")
    # 031 owner pause: the warm path holds on this too; a cold start must not get past it.
    from core.autonomy_control import allows
    if not allows("resume_session").allowed:
        return _room_line(HELD_TEXT)
    caps = container.get_service("room_caps") if container is not None else None
    uid = str(identity.raw_user_id or identity.user_id)
    if caps is None or not caps.may_admin_turn(surface, chat_id):
        return _room_line(CAP_TEXT)

    author = _reply_author(result)
    outside = bool(author or getattr(inbound, "reply_ref", None)
                   or getattr(inbound, "media", None))
    inbound.text = task      # the ledger stored its own copy before routing
    if decision.session_id:
        turn, role = h._room_turn_text(task_agent, result)
        metadata = None
        if room_absorbs_media(result):
            extra, metadata = await h.absorb_for_session(
                task_agent, result, decision.session_id, fetch_media, base_text="")
            turn = h._with_attachment(turn, extra)
        status = "gone"
        mid_run = _session_active(task_agent, decision.session_id)   # before the line lands
        try:
            status = await task_agent.deliver_group_turn(
                decision.session_id, user_id=identity.user_id, context_block="",
                addressed_block=turn.addressed, role=role,
                reply_to=h._room_reply_anchor(result), metadata=h._with_forwarded(result, metadata),
                surface=turn.surface, chat_id=turn.chat_id, shown_message_ids=())
        except Exception as e:
            logger.warning("/admin deliver failed (%s:%s): %s", surface, chat_id, e)
        if status == "held":
            return _room_line(HELD_TEXT)
        if status == "busy":
            task_agent.touch_chat_binding(decision.session_key)
            return _room_line(BUSY_TEXT)
        if status == "delivered":
            task_agent.touch_chat_binding(decision.session_key)
            caps.record_admin_turn(surface, chat_id, uid)
            note_admin_line(task_agent.get_orchestrator(decision.session_id), task, author,
                            outside, mid_run=mid_run)
            await reconcile_room_toolset(task_agent, decision.session_id, source, admin=True)
            h._spawn(h._run_and_deliver(
                task_agent, h._session_owner_uid(task_agent, decision.session_id, identity.user_id),
                decision.session_id, deliver, notice_key=decision.session_key), spawn)
        if status != "gone":
            return None
    stamp = {"surface": surface, "chat_id": chat_id, "uid": uid,
             "user_id": str(identity.user_id or "")}
    try:
        await h._start_task_session(task_agent, result, spawn, deliver, fetch_media,
                                    room_admin=AdminTurn(stamp, task, author, outside))
    except Exception as e:
        logger.warning("/admin start failed (%s:%s): %s", surface, chat_id, e)
        return _room_line(START_FAILED_TEXT)
    caps.record_admin_turn(surface, chat_id, uid)
    return None
