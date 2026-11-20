"""The lifecycle verbs' shared text and rules: `/cancel` (proposal 125 S14).

Below the seats, so Telegram, the other chat surfaces on the shared executor, the
console chat box and the REPL say the same thing and run the same gate without
importing one another (moved from ``surfaces/telegram/harness.py``, which re-exports
the old private names).
"""
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _is_owner(user_id: str) -> bool:
    """The owner gate of a NETWORK seat: the single-user local bypass is never
    honoured — only the bound principal qualifies."""
    from core.instance import is_owner_uid
    return is_owner_uid(user_id, allow_local=False)


#: A16/O17: what `/cancel` reaches. It stops the task bound to THIS chat; the goal
#: and cron loops are separate runs and keep going.
CANCEL_REACH = ("This stops the work of this chat only; goals and scheduled jobs keep "
                "going — /pause stops those.")

#: The ONE denial string for "you may not act here" — shared by the DENIED
#: routing branch (the harness's `_act_on_inbound_locked`) and the lifecycle gate below, so
#: the wording can never drift between the two.
UNAUTHORIZED_TEXT = "🔒 I'm not allowed to talk to you — my owner has not approved you."


def lifecycle_permitted(result: Any) -> bool:
    """Gate for the lifecycle verbs `/cancel` and `/new` (043 A10).

    A lifecycle verb is NOT owner-only — a permitted non-owner DM user must
    still be able to cancel or restart THEIR OWN session. Allow when the
    sender is the owner, OR the sender is in a DM (`chat_type == "dm"`) whose
    `decision.session_key` was built from the sender's OWN chat.

    A Telegram private chat is 1:1 with its sender, and
    `core/surfaces/session_chat_registry.py::build_session_key` embeds that in
    the DM shape `agent:main:{surface}:dm:{chat_id}:{user_id}` — so the key
    naming THIS sender's own chat AND this sender confirms the session is
    theirs to act on. See `owns_dm_session_key`.

    044 T16: a room's session is shared by every member, so a member is still
    denied there — but a room ADMIN the owner promoted may cancel or restart the
    room's own session. The role is the one resolved at the routing boundary
    (`identity.chat_role`), never re-derived here.
    """
    identity = result.inbound.identity
    if _is_owner(identity.user_id):
        return True
    chat_type = getattr(identity.source, "chat_type", "dm") or "dm"
    if chat_type != "dm":
        return getattr(identity, "chat_role", None) == "admin"
    return owns_dm_session_key(result.decision.session_key,
                                getattr(identity.source, "chat_id", None),
                                identity.user_id,
                                getattr(identity.source, "surface_id", None))


def owns_dm_session_key(session_key: Optional[str], chat_id: Any,
                         user_id: Any, surface_id: Any = None) -> bool:
    """Is *session_key* exactly the DM key for this (surface, chat, sender)? (043 T4)

    ⚠️ This was `f":dm:{chat_id}:" in key` — a SUBSTRING search, which asks
    whether the key CONTAINS a shape rather than whether the key IS this
    sender's session. A session key is an ADDRESS, not a haystack: its writer
    is `core/surfaces/session_chat_registry.py::build_session_key`, which
    joins `agent:main:{surface}:{chat_type}:{chat_id}[:{user_id}]
    [:thread:{thread_id}]` on `:`. So `:dm:12:` also matches a group key whose
    own payload carries those bytes in a later segment (e.g. a chat id or
    thread id), and nothing at all checked WHO the key belonged to.

    Parsed instead: the surface/chat_type/chat_id through the builder's own
    inverse (`row_from_session_key`), then the DM's user segment positionally —
    the inverse does not expose it, and that segment is the half that answers
    "is this sender's session".

    ⚠️ The SURFACE is checked against THIS INBOUND's surface, never against the
    literal `"telegram"`: this module is the SHARED inbound actor (registered
    via `core/surfaces/act.py::register_inbound_actor`), so discord, slack,
    signal, x — every `surfaces/_shared.py::route_and_act` caller — and the
    email/webhook path reach these lifecycle verbs too. Pinning the string
    would deny `/cancel` to every non-telegram DM user. Comparing the two
    closes the same hole (an `agent:main:email:dm:555:u` key cannot satisfy a
    telegram sender) and is right on all of them.

    Fail-CLOSED: anything that is not exactly a DM key for this triple (a
    missing user segment, a foreign surface, an unparseable key) is a denial. A
    lifecycle verb cancels a running task; a maybe is a no.
    """
    row = None
    try:
        from core.surfaces.session_chat_registry import row_from_session_key
        row = row_from_session_key(str(session_key or ""))
    except Exception:
        logger.debug("lifecycle: session key parse failed", exc_info=True)
        return False
    if not row or row.get("chat_type") != "dm":
        return False
    if not surface_id or str(row.get("surface_id") or "") != str(surface_id):
        return False
    if not chat_id or str(row.get("chat_id") or "") != str(chat_id):
        return False
    # Segment 5 is the DM's user_id (`build_session_key` appends it only for a
    # dm with a user_id). Its absence means the key does not name an owner, so
    # it cannot name THIS one.
    parts = str(session_key or "").split(":")
    return bool(user_id) and len(parts) >= 6 and parts[5] == str(user_id)


async def cancel_reply(task_agent: Any, result: Any) -> Any:
    """`/cancel` on a chat seat: the gate, a CARD cancel (`/cancel <card id>`,
    `/cancel cards`, owner only), else stop the task bound to THIS chat.

    A16/O17: the reply names the reach, and never claims a stop that did not happen.
    """
    if not lifecycle_permitted(result):
        return UNAUTHORIZED_TEXT
    words = (result.inbound.text or "").split()[1:]
    if words and _is_owner(result.inbound.identity.user_id):
        from core.surfaces.card_list import cancel_reply as card_cancel_reply
        card_reply = card_cancel_reply(result.inbound.identity.user_id, words)
        if card_reply is not None:
            return card_reply
    sid = result.decision.session_id
    if not sid:
        return f"Nothing was running in this chat to stop. {CANCEL_REACH}"
    try:
        stopped = await task_agent.cancel_session_by_id(sid, force=True)
    except Exception as e:
        logger.warning("/cancel failed: %s", e, exc_info=True)
        return (f"I could not stop it ({type(e).__name__}); it may still "
                f"be running. {CANCEL_REACH}")
    if stopped is False:
        return f"Nothing was running in this chat to stop. {CANCEL_REACH}"
    return f"Stopped. {CANCEL_REACH}"


__all__ = ["CANCEL_REACH", "UNAUTHORIZED_TEXT", "cancel_reply", "lifecycle_permitted",
           "owns_dm_session_key"]
