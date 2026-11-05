"""An owner's message in a chat always gets an answer.

``done()`` reaches no chat, so a turn that ends in ``done()`` with no ``send_message``
is silence for the owner. Prod 2026-11-05 11:35: a voice note drained into a continued
chat, the model called ``done("No new owner request in this step")``, and the owner
waited ten minutes before asking whether it had arrived.

One corrective step that quotes the owner's words; then a line that says so. Rooms,
correspondents and sub-agents may stay silent, and an unbound seat delivers its reply
from the turn's record, so none of them is checked. Fail-open to False.
"""
import logging
from typing import Any

from agents.task.runtime.change_claims import _tell_user

logger = logging.getLogger(__name__)

_QUOTE_CAP = 600

NOT_ANSWERED_LINE = ("I received your message but ended the turn without answering it. "
                     "Please send it again.")


def owner_answer_guidance(words: str) -> str:
    quote = words.strip()
    if len(quote) > _QUOTE_CAP:
        quote = quote[:_QUOTE_CAP].rstrip() + "…"
    return ("You called done(), but the owner's latest message has no reply, and done() "
            f"never reaches the owner. Their message:\n{quote}\n"
            "Do what it asks and answer with send_message, then call done().")


def unanswered_owner_words(agent: Any) -> str:
    """The owner's words for this turn when nothing was said back, else ``""``."""
    orch = getattr(agent, "orchestrator", None)
    if orch is None or getattr(agent, "_is_sub_agent", False):
        return ""
    if not (getattr(orch, "_message_router", None) and getattr(orch, "_chat_session_key", None)):
        return ""
    from core.surfaces.room_policy import is_public_session
    from core.surfaces.turn_reply import last_reply_text
    if is_public_session(orch) or last_reply_text(orch) is not None:
        return ""
    words = getattr(orch, "_owner_turn_text", "")
    return words if isinstance(words, str) and words.strip() else ""


async def check_owner_answered(agent: Any, *, final: bool = False) -> bool:
    """Run at ``done()``: True = the loop continues for one corrective step."""
    try:
        words = unanswered_owner_words(agent)
        if not words:
            return False
        if getattr(agent, "_owner_answer_nudged", None) != words and not final:
            agent._owner_answer_nudged = words
            from modules.llm.messages import MessageOrigin
            agent.message_manager.inject_runtime_guidance(
                owner_answer_guidance(words), origin=MessageOrigin.INTERVENTION,
                source="owner_answer")
            logger.warning("owner message has no reply at done() (session %s): one "
                           "corrective step", getattr(agent, "session_id", ""))
            return True
        logger.warning("owner message still has no reply at done() (session %s)",
                       getattr(agent, "session_id", ""))
        await _tell_user(agent, agent.orchestrator, NOT_ANSWERED_LINE)
        # The line asks the owner to send it again: those same words earn a correction too.
        agent._owner_answer_nudged = None
    except Exception:
        logger.warning("owner answer check skipped", exc_info=True)
    return False
