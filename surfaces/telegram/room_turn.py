"""044 T14: the Telegram side of a room turn.

Extracted from ``harness.py`` (fix round 1, minor 7) — the harness is already
one of the tree's largest modules and new behaviour belongs in its own file, the
repo's standing decomposition rule. Everything here is a pure-ish resolver: what
the room is CALLED, who spoke, whether their bytes may be absorbed, and which
tenant a bound session actually runs as. Delivery stays in the harness.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


def chat_title(container: Any, inbound: Any) -> str:
    """What the room is CALLED, for the ``<group-context>`` head.

    Three sources, most trusted first (044 T17): the owner's own ``chat.name``
    from the per-chat policy, then the title Telegram carries on the update's
    chat object, then the chat id — never pretty, always true. The Telegram
    title is attacker-authorable (any room admin can rename the room), so the
    caller renders it through ``group_turn.neutralize_name``; ``chat.name`` is
    owner-authored and neutralized at write.
    """
    from surfaces.telegram.harness import _tg_message
    src = getattr(getattr(inbound, "identity", None), "source", None)
    chat_id = str(getattr(src, "chat_id", "") or "")
    named = _policy_name(container, str(getattr(src, "surface_id", "") or ""), chat_id)
    if named:
        return named
    msg = _tg_message(getattr(inbound, "raw", None) or {}) or {}
    return str((msg.get("chat") or {}).get("title") or chat_id)


def _room_policy(container: Any, surface: str, chat_id: str) -> Any:
    """This room's ``chat.*`` overlay, or None. Fail-open: a policy that cannot
    be read costs the room its NAME and its context budget, never the turn."""
    try:
        from core.runtime_paths import data_dir_or_home
        from core.surfaces.chat_policy import load_for_chat
        cfg = getattr(container, "config", None)
        return load_for_chat(data_dir_or_home(getattr(cfg, "data_dir", None)),
                             surface, chat_id)
    except Exception as e:
        logger.debug("room policy unresolved for %s:%s (%s)", surface, chat_id, e)
        return None


def _policy_name(container: Any, surface: str, chat_id: str) -> str:
    policy = _room_policy(container, surface, chat_id)
    return str(getattr(policy, "name", "") or "")


def room_role(result: Any) -> str:
    """The speaker's role for this room turn.

    044 T16: the role is resolved ONCE at the routing boundary
    (``core/surfaces/access.py::resolve_access_tier``) against the per-chat role
    store and stamped onto the envelope, so this READS that answer rather than
    deriving a second one that could disagree with the tier the router already
    acted on.

    The owner-or-member fallback still runs when nothing stamped a role — a DM,
    or an inbound that never reached the tier model. It is the conservative
    side: a member's line is DATA, an admin's is a steer.

    ``_is_admin_owner`` is imported at CALL time: the harness imports this
    module, so a module-level import would close the cycle. One owner check,
    not two.
    """
    stamped = getattr(result.inbound.identity, "chat_role", None)
    if stamped:
        return str(stamped)
    from surfaces.telegram.harness import _is_admin_owner
    return "owner" if _is_admin_owner(result.inbound.identity.user_id) else "member"


def room_absorbs_media(result: Any) -> bool:
    """Whether this turn's attachment bytes may be written into the session's
    workspace. A DM is unchanged (always); in a ROOM only an owner/admin turn
    absorbs (044 §4.4 + the 2026-09-13 media rule)."""
    src = getattr(result.inbound.identity, "source", None)
    if (getattr(src, "chat_type", "dm") or "dm") == "dm":
        return True
    return room_role(result) in ("owner", "admin")


def session_owner_uid(task_agent: Any, session_id: str, fallback: str) -> str:
    """The tenant a session runs AS, read from the session's own metadata.

    A member may trigger (and start) a room session, so the speaker's uid is NOT
    the tenant — running the loop as the speaker would put a stranger's id on the
    owner's session. Fail-open to ``fallback`` only when the store cannot answer.
    """
    try:
        info = task_agent.session_manager.get_session_info(session_id)
        owner = (info.get("user_id") if isinstance(info, dict)
                 else getattr(info, "user_id", None))
        return str(owner) if owner else fallback
    except Exception as e:  # pragma: no cover - fail-open probe
        logger.debug("session owner lookup failed for %s: %s", session_id, e)
        return fallback


def room_turn_text(task_agent: Any, result: Any) -> Tuple[Any, str]:
    """``(RoomTurn, role)`` for one room line. Fail-open: any fault costs the
    room CONTEXT, never the turn."""
    from core.surfaces.group_turn import RoomTurn, build_room_turn
    role = room_role(result)
    container = getattr(task_agent, "container", None)
    src = getattr(result.inbound.identity, "source", None)
    policy = _room_policy(container, str(getattr(src, "surface_id", "") or ""),
                          str(getattr(src, "chat_id", "") or ""))
    try:
        turn = build_room_turn(container, result.inbound, role=role,
                               chat_name=chat_title(container, result.inbound),
                               # 044 T17: how much of the room the model is
                               # shown is the room's own setting.
                               context_lines=int(getattr(policy, "context_lines", 30)))
    except Exception as e:
        logger.warning("room turn build failed (%s) — answering without context", e)
        turn = RoomTurn(
            context="", addressed=(result.inbound.text or ""),
            surface=str(getattr(src, "surface_id", "") or ""),
            chat_id=str(getattr(src, "chat_id", "") or ""),
            shown_message_ids=(), addressed_message_id=None)
    return turn, role


def mark_turn_answered(task_agent: Any, turn: Any, session_id: str, *,
                       include_shown: bool = True) -> None:
    """Mark the rows this turn covers as handled (fail-open). Used by the COLD
    start; the warm path marks inside ``deliver_group_turn``.

    044 I8: ``include_shown=False`` marks ONLY the addressed line. "Presented =
    handled" needs the context block to have been PRESENTED, and
    ``push_room_context`` is fail-open — when it could not place the block, the
    lines in it were never shown to anyone and must stay unanswered for the next
    turn or the service run. The ADDRESSED line is still marked either way: it
    rode the session's own task text, so it WAS delivered, and leaving it open
    would have the service job answer it a second time.
    """
    from core.surfaces.group_turn import mark_room_lines_answered
    ids = turn.answered_ids
    if not include_shown:
        mid = getattr(turn, "addressed_message_id", None)
        ids = (mid,) if mid else ()
    mark_room_lines_answered(getattr(task_agent, "container", None),
                             surface=turn.surface, chat_id=turn.chat_id,
                             message_ids=ids, session_id=session_id)


def room_session_owner(fallback_uid: str) -> str:
    """The tenant a ROOM session is created under — always the OWNER tenant.

    A room key is NOT user-scoped (one key per chat,
    ``core/surfaces/session_chat_registry.py::build_session_key``) and a member
    may START the session, so without this the first stranger to speak would
    become the tenant the room runs as — and every later owner turn in it with
    him. The owner's own cold start is unchanged: ``_is_admin_owner`` answers
    True only when the uid already EQUALS the principal.

    ⚠️ ``fallback_uid`` is UNUSED and is kept only so the call sites that pass a
    sender id keep working. It was a real fallback while the resolver could
    answer empty; the ONE owner-tenant resolver cannot (its last tier is the
    literal ``local``), so returning it would have been dead code that LOOKED
    like a safety net — and the thing it would have fallen back to is the
    stranger this function exists to keep out.
    """
    from core.instance import resolve_owner_user_id
    return resolve_owner_user_id()


def cold_start_request(turn: Any, role: str) -> str:
    """The session TASK for a room's first turn: the framed addressed line ONLY.

    The context block is pushed as an ephemeral message after ``create_session``
    returns (``TaskAgent.push_room_context``). It must NOT ride the request: a
    task string lives for the whole session, and "the context block is API-only"
    means exactly that old room chatter never becomes durable session state.
    """
    from core.surfaces.group_turn import frame_addressed
    return frame_addressed(turn.addressed, role=role)


def attachment_description(turn: Any, text: Optional[str]) -> Any:
    """Fold an absorbed attachment description INSIDE the ``<addressed>`` block —
    text placed after the closing fence reads as a separate, unattributed
    instruction (fix round 1, minor 9)."""
    return turn.with_attachment(text or "")
