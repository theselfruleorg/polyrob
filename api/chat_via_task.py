"""Canonical handler for the HTTP chat endpoint (/api/chat/message).

Chat is served purely by the unified task agent's synchronous ``chat_once`` — the
legacy ``ChatAgent`` was retired (HANDOFF-C, 2026-06-19). There is no legacy path
to fall back to, so any failure returns a graceful ``MessageResponse(success=False)``
rather than ``None``. This module is the single seam between the public HTTP chat
contract and the one agent core.
"""
import logging
from typing import Optional

from api.models import MessageResponse

logger = logging.getLogger(__name__)

_UNAVAILABLE_TEXT = "Chat is temporarily unavailable. Please try again."
_ERROR_TEXT = "Sorry, something went wrong handling your message."


async def handle_chat_via_task_agent(
    container,
    user_id: str,
    text: str,
    chat_id: Optional[str],
) -> MessageResponse:
    """Serve one chat turn from the unified task agent.

    Always returns a ``MessageResponse``: ``success=True`` with the agent reply,
    or ``success=False`` with a user-safe message when the agent is unavailable
    or ``chat_once`` raises (logged as ``chat.error`` — there is no legacy agent
    to fall back to).
    """
    try:
        task_agent = container.get_agent("task_agent") if container else None
        if not task_agent or not hasattr(task_agent, "chat_once"):
            logger.error("chat.error: task_agent unavailable")
            return MessageResponse(success=False, text=_UNAVAILABLE_TEXT)
        reply = await task_agent.chat_once(user_id=user_id, text=text, chat_id=chat_id)
        return MessageResponse(success=True, text=str(reply or ""))
    except Exception as e:
        logger.error(f"chat.error: {e}", exc_info=True)
        return MessageResponse(success=False, text=_ERROR_TEXT)
