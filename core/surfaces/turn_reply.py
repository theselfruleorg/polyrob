"""Per-turn reply latch — "which text is THE reply?" (communication contract C1).

POLYROB had two user-facing emit verbs and no concept of a turn's reply:
``send_message`` published (``tools/controller/action_registration.py``) and so
did ``done`` (the C10 mirror), unconditionally. ``MessageRouter.publish`` has no
per-turn rule and no content dedup, so a turn that answered and then completed
delivered the answer AND a third-person report about answering.

The rule this module holds:

    a DM turn commits exactly one reply — and never zero.
    a ROOM turn commits at most one reply — and zero is legitimate.

``send_message`` marks the latch; ``done`` publishes only when the latch is
clear. ``done`` therefore stops being a second voice and becomes the SAFETY NET
for a turn that spoke nothing (today's majority shape: the model answers and
completes in one action). Deleting ``done``'s publish outright would take those
turns silent, which is strictly worse than the duplicate.

⚠️ **The safety net does not extend to a room** (2026-09-16). The room prompt
teaches ``[SILENT]`` as a valid outcome, so "never zero" there forced a message
the agent had deliberately declined to send — and the only text available to
force was ``done``'s internal completion record. Published live into a public
group it read as a status report to nobody, quoting the owner's private
question. ``build_completion_publish`` now returns early on a room session key;
this latch still governs which text is THE reply, but in a room only
``send_message`` can be it. See ``core/surfaces/outbound_mirror.py``.

State is one attribute on the orchestrator, reset at the same seam
``_forged_turn_kind`` is recomputed at (``core/user_ingress.py::
_drain_user_messages``) — recomputed per drain, never "set once and cleared
somewhere else", so there is no separate step to forget on an exception path.

Everything here is fail-open: a latch fault must never cost the user a message.
The failure direction is deliberately asymmetric —
:func:`reply_published` returns False on any error, so an unreadable latch
duplicates rather than silences.
"""
import logging

logger = logging.getLogger(__name__)

#: Orchestrator attribute holding the latch. Private by convention, alongside
#: ``_forged_turn_kind`` / ``_chat_session_key`` / ``_message_router``.
_ATTR = "_reply_published_this_turn"

#: Orchestrator attribute holding the text of that reply. The unbound delivery
#: path (`_extract_chat_reply`) needs the reply ITSELF, not just the fact that
#: one happened: `done` writes "✅ Task Complete\n\n<text>" into history, so it
#: is the last AIMessage and no ordering rule over history can recover the real
#: answer. Recording it where it is published is exact rather than heuristic.
_TEXT_ATTR = "_reply_text_this_turn"


def single_final_enabled() -> bool:
    """Whether the one-reply-per-turn rule is active (``CHAT_SINGLE_FINAL``).

    Default ON. OFF restores the legacy double-publish byte-identically.
    """
    from core.env import bool_env
    return bool_env("CHAT_SINGLE_FINAL", True)


def reply_published(orchestrator) -> bool:
    """True when a discrete reply was already committed in this turn.

    Fail-open toward False: an unreadable latch means ``done`` publishes, so a
    bug here costs a duplicate message, never a silent turn.
    """
    if orchestrator is None:
        return False
    try:
        return bool(getattr(orchestrator, _ATTR, False))
    except Exception:  # a hostile/proxied orchestrator must not break a send
        logger.debug("turn_reply: latch read failed (fail-open)", exc_info=True)
        return False


def mark_reply_published(orchestrator, text: str = "") -> None:
    """Record that this turn has committed its reply, and what it said.

    *text* is optional so a caller that only needs the latch stays unchanged.
    """
    if orchestrator is None:
        return
    try:
        setattr(orchestrator, _ATTR, True)
        # Only a genuine non-blank str is recorded. Anything else (None, a mock,
        # a stray object) must leave the slot empty rather than become the text
        # the user is shown as the agent's answer.
        if isinstance(text, str) and text.strip():
            setattr(orchestrator, _TEXT_ATTR, text)
    except Exception:
        logger.debug("turn_reply: latch write failed (fail-open)", exc_info=True)


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
    # Strictly a str: a test double or proxied orchestrator auto-creates missing
    # attributes, and a truthy mock coerced with str() would be DELIVERED to the
    # user as the reply. Anything not a real string means "nothing was recorded".
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def reset_turn(orchestrator) -> None:
    """Clear the latch AND the recorded reply at a turn boundary.

    Both must clear together — a stale reply text would be delivered as the next
    turn's answer, which is a worse failure than the duplicate this fixes.
    """
    if orchestrator is None:
        return
    try:
        setattr(orchestrator, _ATTR, False)
        setattr(orchestrator, _TEXT_ATTR, None)
    except Exception:
        logger.debug("turn_reply: latch reset failed (fail-open)", exc_info=True)
