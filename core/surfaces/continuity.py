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
