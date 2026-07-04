"""Session-start activity digest (2026-07-03).

Passive, read-only, pinned foundation message that puts the agent's OWN recent runs
in front of it — so it can answer 'what did I run in the last 8h?' without a tool call.
Scoped to owner/chat sessions ONLY (never goal/cron/sub-agent) to avoid a per-run token
tax + distraction. Fail-open.

NOTE on `kind not in ("chat",)`: goal/cron sessions are excluded by the CALLER (the
first-step wiring in `memory_prefetch.py::MemoryPrefetchMixin._maybe_inject_episodic_digest`
resolves `kind` via `agents.task.goals.autonomy_marker.is_autonomous(session_id)` and only
ever passes `kind="chat"` here after confirming the session is not autonomous — see that
module's docstring for why this can't be decided at Agent-construction time). This function
re-checks `kind` itself so it is also safe to call directly (as the tests below do) with an
explicit non-chat `kind`.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


async def build_activity_digest(*, user_id: Optional[str], kind: str,
                                is_sub_agent: bool, window_hours: int = 24,
                                limit: int = 10):
    try:
        from agents.task.constants import AutonomyConfig
        if not AutonomyConfig.episodic_digest_inject():
            return None
        if is_sub_agent or kind not in ("chat",):        # owner/chat sessions only
            return None
        from modules.memory.registry import memory_recall_episodes
        since_ts = int(time.time()) - window_hours * 3600
        # exclude_surfaced=True: an episode already delivered out-of-band (cron
        # email/telegram/twitter, or a goal self-wake) doesn't need to be
        # re-surfaced here too — recent_activity() (explicit query) still sees
        # everything regardless of the surfaced flag.
        rows = await memory_recall_episodes(user_id=user_id, since_ts=since_ts,
                                            limit=limit, order="newest",
                                            exclude_surfaced=True)
        if not rows:
            return None
        lines, total = [], 0.0
        for e in rows:
            total += float(e.spend_usd or 0)
            lines.append(f"- {e.kind}:{e.outcome or '?'} ${float(e.spend_usd or 0):.2f} "
                         f"\"{(e.task or '')[:60]}\"")
        body = ("\n".join(lines)
                + f"\n({len(rows)} runs in last {window_hours}h, ${total:.2f} total). "
                  "Call recent_activity(since=\"8h\") for more/older.")
        try:
            from agents.task.agent.core.untrusted_wrap import wrap_untrusted
            body = wrap_untrusted("recent_activity", body)
        except Exception:
            pass
        from modules.llm.messages import make_control_message, MessageOrigin
        return make_control_message(
            f"<recent_activity window=\"last {window_hours}h\">\n{body}\n</recent_activity>",
            MessageOrigin.EPISODIC_DIGEST)
    except Exception:
        logger.warning("build_activity_digest failed", exc_info=True)
        return None


async def build_mission_continuity(*, user_id: Optional[str], window_hours: int = 24,
                                   limit: int = 8):
    """Carry recent autonomous activity INTO the next goal/cron tick (§7.5).

    Mirror-image of ``build_activity_digest``: this one is FOR autonomous sessions
    (the caller confirms ``is_autonomous`` and never sub-agent), so a tick knows what
    it already did/attempted and stops re-deriving 'nothing new' from scratch. Gated
    ``AUTONOMOUS_CONTINUITY_BRIDGE`` (default OFF). Fail-open. Does NOT exclude
    already-surfaced episodes — a tick genuinely needs to see everything it did.
    """
    try:
        from agents.task.constants import AutonomyConfig
        if not AutonomyConfig.autonomous_continuity_bridge():
            return None
        from modules.memory.registry import memory_recall_episodes
        since_ts = int(time.time()) - window_hours * 3600
        rows = await memory_recall_episodes(user_id=user_id, since_ts=since_ts,
                                            limit=limit, order="newest")
        if not rows:
            return None
        lines = [f"- {e.kind}:{e.outcome or '?'} \"{(e.task or '')[:70]}\"" for e in rows]
        body = ("\n".join(lines)
                + f"\n({len(rows)} recent runs in last {window_hours}h). "
                  "Build on this — do NOT repeat work already done; if it's all covered, "
                  "surface the blocker/next step rather than re-deriving 'nothing new'.")
        try:
            from agents.task.agent.core.untrusted_wrap import wrap_untrusted
            body = wrap_untrusted("recent_activity", body)
        except Exception:
            pass
        from modules.llm.messages import make_control_message, MessageOrigin
        return make_control_message(
            f"<mission_continuity window=\"last {window_hours}h\">\n{body}\n</mission_continuity>",
            MessageOrigin.EPISODIC_DIGEST)
    except Exception:
        logger.warning("build_mission_continuity failed", exc_info=True)
        return None
