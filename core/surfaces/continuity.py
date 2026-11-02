"""Session-boundary continuity bridge.

Seeds a short "last time we were discussing X" foundation message into a FRESH chat
session, sourced from the prior chat episode's summary for the same ``thread_key`` —
so continuity survives ``/new``, the idle/daily reset and a restart. The bridge is
literally "recall my last episode for this chat key": a consumer of the episodic
store, not a second summary system.

The episode is written by ONE method, ``SessionOrchestrator.write_chat_episode``
(``agents/task/session/cleanup.py``), at every boundary that ends an owner chat:
eviction, ``/new`` (``TaskAgent.unbind_chat``), the reset (``TaskAgent.
close_previous_chat_episode``: the structured extract is awaited before the new
session starts, so its first step finds the row; the aux-LLM summary upgrades
the row in the background) and a process stop (``write_open_chat_episodes``). Its
summary covers the whole session — decisions, standing instructions, open
promises, what was done (``agents/task/session/chat_episode.py``). The episode
row is upserted on ``(user_id, session_id)``, so a later boundary of the same
session replaces its account; a boundary with nothing new writes nothing.
"""
from __future__ import annotations

import logging
from core.config_policy import AutonomyConfig
from typing import Optional

logger = logging.getLogger(__name__)


#: E-C4 (2026-10-13 review): the bridge cut the summary at 600 chars and called
#: the owner "this contact". The owner thread gets a longer bridge, owner wording,
#: and the open errand when the last session did not finish.
BRIDGE_MAX_CHARS = 600
OWNER_BRIDGE_MAX_CHARS = 1500
_OPEN_OUTCOMES = ("partial", "failed", "cancelled")


async def build_bridge_message(*, user_id: Optional[str], thread_key: Optional[str],
                               owner: bool = False):
    """Recall the most recent ``kind="chat"`` episode with a non-empty summary for
    ``thread_key`` and wrap it as a ``SESSION_BRIDGE`` control message.

    Returns None when: ``AutonomyConfig.continuity_bridge_enabled()`` is False,
    ``thread_key`` is falsy, no prior chat episode exists for it, or every recalled
    episode has an empty/whitespace-only summary. Fail-open — any error returns None.
    """
    try:
        if not AutonomyConfig.continuity_bridge_enabled() or not thread_key:
            return None

        from modules.memory.registry import memory_recall_episodes
        rows = await memory_recall_episodes(
            user_id=user_id, thread_key=thread_key, kind="chat", limit=3, order="newest")

        summary, row_used = None, None
        for row in rows or []:
            candidate = (getattr(row, "summary", None) or "").strip()
            if candidate:
                summary, row_used = candidate, row
                break
        if not summary:
            return None

        from modules.llm.messages import make_control_message, MessageOrigin
        if owner:
            # The owner thread: an owner chat episode is never written for a
            # correspondent-facing session, so this is the owner's own
            # conversation — dated, longer, and naming an unfinished errand.
            import time as _t
            ts = getattr(row_used, "ts", None)
            day = ""
            try:
                day = f" ({_t.strftime('%Y-%m-%d %H:%M', _t.localtime(int(ts)))})" if ts else ""
            except Exception:
                day = ""
            body = summary[:OWNER_BRIDGE_MAX_CHARS]
            outcome = (getattr(row_used, "outcome", None) or "").strip()
            errand = (getattr(row_used, "task", None) or "").strip()
            if outcome in _OPEN_OUTCOMES and errand:
                body += (f"\nThat session ended {outcome}; the open errand was: "
                         f"{errand[:400]}")
            return make_control_message(
                f"Continuing your earlier conversation with the owner. Last time{day}:\n"
                + body, MessageOrigin.SESSION_BRIDGE)

        # C7: the summary is derived from a past conversation (it can carry a
        # correspondent's words), so it rides as DATA — the lead-in stays outside.
        from core.security.untrusted_wrap import wrap_untrusted
        return make_control_message(
            "Continuing an earlier conversation with this contact. Last time:\n"
            + wrap_untrusted("session_bridge", summary[:BRIDGE_MAX_CHARS]),
            MessageOrigin.SESSION_BRIDGE,
        )
    except Exception:
        logger.warning("build_bridge_message failed", exc_info=True)
        return None
