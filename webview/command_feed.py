"""A chat-box verb's answer is part of the chat's history (113 W5, C-05).

``POST /api/session/{id}/messages`` answers an owner verb inline
(``command_reply``) and the verb never reaches the agent, so nothing was written
to the feed: a reload lost a ``/status`` answer and its card buttons. The answer
is now written as a ``command_reply`` feed event through the ONE feed writer,
``SessionManager.add_to_feed``, with the typed line beside it::

    {"type": "command_reply", "data": {"text": <answer>, "line": <the verb line>,
                                        "inline": true}}

``inline: true`` tells the tab that typed the verb that it already drew this
answer from the HTTP response (``transcript.js`` skips the live copy once). A
background money result (``console_commands._console_deliver``) keeps its live
push with no ``inline`` mark.

Fail-open: the verb already ran and its answer is in the response; a feed that
cannot be written is logged, never a refusal. No session (the cold open) and a
console seat name (``money``/``inbox``) write nothing.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The feed event type; the same name the background push uses.
from webview.console_commands import COMMAND_REPLY_EVENT  # noqa: E402


def _session_manager(task_agent) -> Any:
    return getattr(task_agent, "session_manager", None) if task_agent is not None else None


async def record_command_reply(task_agent, clean_id: str, line: str,
                               reply: Optional[Any]) -> Optional[Any]:
    """Write *reply* to *clean_id*'s feed and return it unchanged (None stays None)."""
    if reply is None or not clean_id:
        return reply
    text = str(reply).strip()
    if not text:
        return reply
    try:
        from agents.task.path import is_reserved_session_id
        if is_reserved_session_id(clean_id):
            return reply
        manager = _session_manager(task_agent)
        if manager is None:
            logger.debug("command feed: no session manager in this process; %s not kept",
                         clean_id)
            return reply
        data = {"text": text, "line": str(line or "").strip()[:500], "inline": True}
        await asyncio.to_thread(manager.add_to_feed, clean_id, COMMAND_REPLY_EVENT, data)
    except Exception:
        logger.warning("command feed: could not keep the answer for %s", clean_id,
                       exc_info=True)
    return reply
