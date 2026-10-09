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
    # 061: this latch is the ONE place every seat's reply passes, so it is where
    # an interactive reply joins the owner thread (autonomous replies are
    # recorded by the delivery rail). Fail-open, never blocks the turn.
    try:
        from core.surfaces.user_delivery import record_interactive_reply
        record_interactive_reply(orchestrator, text)
    except Exception:
        logger.debug("turn_reply: owner thread record skipped (fail-open)", exc_info=True)


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


def is_repeat_reply(orchestrator, text: str) -> bool:
    """Did this turn already say exactly ``text`` to the user?

    Prod 2026-10-06: the model re-sent an identical reply one step after the
    first was delivered, and the owner got it twice. ``send_message`` asks this
    before publishing. Fail-open to False — a fault never swallows a message.
    """
    try:
        if not isinstance(text, str) or not text.strip():
            return False
        prior = last_reply_text(orchestrator)
        if prior is None:
            return False
        a, b = prior.strip(), text.strip()
        if a == b:
            return True
        # 13:32:57 / 13:33:01 the same day: a re-send with a few words reworded
        # reached the owner twice too. Long, near-identical text is the same message.
        if min(len(a), len(b)) >= _NEAR_MIN_CHARS:
            from difflib import SequenceMatcher
            return SequenceMatcher(None, a, b).ratio() >= _NEAR_RATIO
        return False
    except Exception:
        return False


#: What ``send_message`` tells the agent instead of re-sending. It does NOT say
#: "call done()": a repeat can come mid-task (two identical timeout notices from
#: ``llm_runner``), and ending the turn there drops the owner's request.
REPEAT_REPLY_NOTE = ("Not re-sent: the user already has this exact message from this turn. "
                     "Do not send it again. Continue the task; call done() only if the work "
                     "is finished.")

#: A reply this long that matches the turn's earlier reply this closely is a re-send.
_NEAR_MIN_CHARS = 80
_NEAR_RATIO = 0.85


def reset_turn(orchestrator) -> None:
    """Clear the record at a turn boundary."""
    if orchestrator is None:
        return
    try:
        if hasattr(orchestrator, _TEXT_ATTR):
            delattr(orchestrator, _TEXT_ATTR)
    except Exception:
        logger.debug("turn_reply: reset failed (fail-open)", exc_info=True)
