"""Idle-reset continuity bridge (Task 6, 2026-07-03).

Seeds a short "last time we were discussing X" foundation message into a FRESH chat
session, sourced from the prior chat episode's summary for the same ``thread_key`` —
so continuity survives an idle/daily reset. The bridge is literally "recall my last
episode for this chat key": a consumer of the episodic store (Tasks 1-2), not a
second summary system.

Deviation from the original brief (documented, not an oversight)
------------------------------------------------------------------
The brief additionally sketched a ``write_closing_episode`` helper called from
``core/surfaces/dispatcher.py``'s ``if fresh:`` branch, writing a closing episode for
the OUTGOING session at the reset boundary (with no summary available there — routing
code isn't a summarizer). That write is deliberately NOT implemented:

``SqliteMemoryProvider.record_episode`` upserts
``ON CONFLICT(user_id, session_id) DO UPDATE SET summary=excluded.summary`` —
keyed by ``(user_id, session_id)``, NOT ``thread_key``. Task 6 Part A (see
``agents/task/session/cleanup.py``) already writes a REAL H-MEM-derived summary for
that same ``session_id`` when the session is actually torn down. In the common case
the old session has already been LRU/idle-evicted (cleanup already ran, already wrote
a good summary) by the time a new inbound message triggers ``fresh`` for its
thread_key — so a dispatcher-side null-summary write would land AFTER and CLOBBER the
good one via that same upsert. Rather than add cross-site write-ordering/locking to
make a second write site safe, this bridge is written from exactly ONE place
(cleanup()), which is also the only place a real summary is available.

Known limitation (v1, acceptable): a session that is still resident — not yet
LRU/idle-evicted, so its cleanup() (and therefore its summary-bearing episode) hasn't
run yet — will not bridge on the very next reset boundary for its thread_key; the
bridge only starts working once that session's cleanup episode has landed. Task 5's
broader "recent activity" digest still covers that window ("what did you do"), just
not the more specific "what were we just discussing".
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def build_bridge_message(*, user_id: Optional[str], thread_key: Optional[str]):
    """Recall the most recent ``kind="chat"`` episode with a non-empty summary for
    ``thread_key`` and wrap it as a ``SESSION_BRIDGE`` control message.

    Returns None when: ``AutonomyConfig.continuity_bridge_enabled()`` is False,
    ``thread_key`` is falsy, no prior chat episode exists for it, or every recalled
    episode has an empty/whitespace-only summary. Fail-open — any error returns None.
    """
    try:
        from agents.task.constants import AutonomyConfig
        if not AutonomyConfig.continuity_bridge_enabled() or not thread_key:
            return None

        from modules.memory.registry import memory_recall_episodes
        rows = await memory_recall_episodes(
            user_id=user_id, thread_key=thread_key, kind="chat", limit=3, order="newest")

        summary = None
        for row in rows or []:
            candidate = (getattr(row, "summary", None) or "").strip()
            if candidate:
                summary = candidate
                break
        if not summary:
            return None

        from modules.llm.messages import make_control_message, MessageOrigin
        return make_control_message(
            "Continuing an earlier conversation with this contact. "
            f"Last time: {summary[:600]}",
            MessageOrigin.SESSION_BRIDGE,
        )
    except Exception:
        logger.warning("build_bridge_message failed", exc_info=True)
        return None
