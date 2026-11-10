"""113: what a chat surface says about the chat itself (not about a turn).

The five chat verbs (``/new``, ``/chats``, ``/chat``, ``/rename``, ``/close``) wrap
``core.surfaces.chats.chat_verb``; a room keeps the old ``/new`` (restart the room's
one session). And the line that tells a person their chat started fresh after an idle gap. The
dispatcher sets ``RouteDecision.fresh_gap_s``; before this the chat lost its history
with no word. Shared by every chat surface (they all run the Telegram harness's
executor through ``surfaces/_actor.py``).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from core.surfaces.chats import CHAT_VERBS, parse_chat_tap


def is_chat_command(cmd: str) -> bool:
    """One of the five chat verbs, or the tap ``/chat_<slug>``."""
    return cmd in CHAT_VERBS or bool(parse_chat_tap(cmd))

logger = logging.getLogger(__name__)


async def _restart_room(task_agent: Any, result: Any) -> str:
    """A room has ONE session: ``/new`` from its admin stops it and starts fresh."""
    sid = result.decision.session_id
    if sid:
        try:
            await task_agent.cancel_session_by_id(sid, force=True)
        except Exception as e:
            logger.debug("/new cancel failed: %s", e)
    try:
        task_agent.unbind_chat(result.decision.session_key)
    except Exception as e:
        logger.debug("/new unbind failed: %s", e)
    return "Started fresh — send your next message to begin."


TASK_IS_NEW = ("/task is now /new: I opened a separate chat for this. "
               "/chat main goes back to the main chat.")


async def task_alias(task_agent: Any, result: Any, deliver: Any) -> str:
    """``/task <goal>`` = ``/new`` and the goal as the first line (kept for one
    release). Returns a refusal text, or "" when the caller may start the session:
    the decision then names the new chat, so the bound chat keeps its own run (two
    ``/task`` lines used to leave the first run with no chat). A room, or a seat
    with no chat store, keeps the old form: the goal starts on the key as routed."""
    inbound, decision = result.inbound, result.decision
    words = (inbound.text or "").strip().split(None, 1)
    goal = words[1].strip() if len(words) > 1 else ""
    if not goal:
        return "Say what you want done: /new [name], then your first message."
    inbound.text = goal
    source = inbound.identity.source
    container = getattr(task_agent, "container", None)
    registry = container.get_service("session_chat_registry") if container else None
    if registry is None or (getattr(source, "chat_type", "dm") or "dm") != "dm":
        return ""
    try:
        from core.surfaces import chats
        from core.surfaces.session_chat_registry import split_chat_key
        view = chats.open_chat(
            registry, base_key=split_chat_key(decision.session_key or "")[0],
            user_id=inbound.identity.user_id, surface_id=source.surface_id,
            chat_id=str(source.chat_id))
        decision.session_key, decision.session_id = view["session_key"], None
        if deliver is not None:
            await deliver(TASK_IS_NEW)
    except Exception:
        logger.warning("/task: no separate chat opened (the goal starts here)", exc_info=True)
    return ""


async def chat_command(task_agent: Any, result: Any, cmd: str) -> str:
    """Run one chat verb for the sender's own direct chat. The caller checked that
    the sender owns it (``_lifecycle_permitted``)."""
    from core.surfaces import chats
    from core.surfaces.session_chat_registry import split_chat_key

    decision, inbound = result.decision, result.inbound
    source = inbound.identity.source
    if (getattr(source, "chat_type", "dm") or "dm") != "dm":
        if cmd == "/new":
            return await _restart_room(task_agent, result)
        return "Separate chats are for a direct chat with me, not a group."
    container = getattr(task_agent, "container", None)
    registry = container.get_service("session_chat_registry") if container else None
    words = (inbound.text or "").split()
    args = words[1:]
    tap = chats.parse_chat_tap(words[0] if words else "")
    if tap:                            # the one-token form /chat_<slug>
        cmd, args = "/chat", [tap]
    out = chats.chat_verb(
        registry, cmd, args, base_key=split_chat_key(decision.session_key or "")[0],
        user_id=inbound.identity.user_id, surface_id=source.surface_id,
        chat_id=str(source.chat_id), taps=True)
    if out["closed_key"]:
        try:
            task_agent.close_chat_episode(out["closed_key"])
        except Exception:
            logger.debug("chat close: episode not written", exc_info=True)
    return out["text"]


def _gap_words(seconds: float) -> str:
    hours = int(round(seconds / 3600.0))
    if hours < 48:
        return f"{max(hours, 1)} hours"
    return f"{int(round(hours / 24.0))} days"


def refresh_notice(gap_s: Optional[float]) -> str:
    """The one line, or "" when the chat did not start fresh."""
    if gap_s is None:
        return ""
    return (f"I started this chat fresh: the last message here was {_gap_words(gap_s)} ago. "
            "I still have my memory and our recent lines. /thread shows the earlier ones.")


async def tell_refresh(task_agent: Any, result: Any, deliver: Any) -> bool:
    """Send the fresh-chat line for a DM turn. True when it was sent. Never in a room,
    never raises: a failed notice must not stop the turn it announces."""
    try:
        decision, inbound = result.decision, result.inbound
        text = refresh_notice(getattr(decision, "fresh_gap_s", None))
        if not text or deliver is None:
            return False
        if (inbound.identity.source.chat_type or "dm") != "dm":
            return False
        await deliver(text)
        from core.instance import is_owner_uid
        if is_owner_uid(inbound.identity.user_id, allow_local=False):
            from core.surfaces.owner_thread import record_turn_fallback
            record_turn_fallback(getattr(task_agent, "container", None),
                                 inbound.identity.user_id, text,
                                 session_key=decision.session_key or "", session_id=None)
        return True
    except Exception:
        logger.warning("fresh-chat notice not sent", exc_info=True)
        return False
