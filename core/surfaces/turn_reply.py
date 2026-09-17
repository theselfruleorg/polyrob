"""Per-turn reply record — "which text is THE reply?" (communication contract C1).

``send_message`` is the speech verb: the only verb whose text the user reads.
``done`` writes an internal completion record to history and the session feed
and is NOT delivered on any surface. (Until 2026-09-17 a router mirror published
``done``'s text whenever ``send_message`` had not claimed the turn — and the
``message`` tool, a file with a caption, never claimed it, so the owner received
the answer AND a 2,400-char third-person recap after every such turn. Before
that, in a room, the same net leaked a session recap into a public group.)

What survives is the RECORD: ``send_message`` stores the text it published so
the UNBOUND delivery paths (``_extract_chat_reply`` — raw API, ``chat_once``,
``/v1``) can return the reply the user actually got rather than scanning
history, where ``done`` writes "✅ Task Complete\n\n<text>" and so IS the last
AIMessage.

State is one attribute on the orchestrator, reset at the same seam
``_forged_turn_kind`` is recomputed at (``core/user_ingress.py::
_drain_user_messages``) and by ``deliver_group_turn`` for a member's ephemeral
line — recomputed per drain, never "set once and cleared somewhere else".

Everything here is fail-open: a record fault must never cost the user a message.
"""
import logging

logger = logging.getLogger(__name__)

#: Orchestrator attribute holding the text of this turn's reply. Private by
#: convention, alongside ``_forged_turn_kind`` / ``_chat_session_key`` /
#: ``_message_router``.
_TEXT_ATTR = "_reply_text_this_turn"


def mark_reply_published(orchestrator, text: str = "") -> None:
    """Record what this turn said to the user.

    Only a genuine non-blank str is recorded. Anything else (None, a mock, a
    stray object) leaves the slot untouched rather than becoming the text the
    user is shown as the agent's answer.
    """
    if orchestrator is None:
        return
    try:
        if isinstance(text, str) and text.strip():
            setattr(orchestrator, _TEXT_ATTR, text)
    except Exception:
        logger.debug("turn_reply: reply-text write failed (fail-open)", exc_info=True)


def last_reply_text(orchestrator):
    """The text this turn already spoke to the user, or None.

    Used by the UNBOUND delivery path to answer "which text is the reply?" the
    same way the bound path does. Fail-open to None: the caller then falls back
    to its existing history scan, i.e. today's behaviour.
    """
    if orchestrator is None:
        return None
    try:
        value = getattr(orchestrator, _TEXT_ATTR, None)
    except Exception:
        logger.debug("turn_reply: reply-text read failed (fail-open)", exc_info=True)
        return None
    return value if isinstance(value, str) and value.strip() else None


def reset_turn(orchestrator) -> None:
    """Clear the record at a turn boundary."""
    if orchestrator is None:
        return
    try:
        if hasattr(orchestrator, _TEXT_ATTR):
            delattr(orchestrator, _TEXT_ATTR)
    except Exception:
        logger.debug("turn_reply: reset failed (fail-open)", exc_info=True)
