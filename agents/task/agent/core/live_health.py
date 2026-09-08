"""Per-turn ``<live-health>`` note for the agent (2026-08-28 status SSOT, D10).

The ``<environment>`` foundation block is per-session STATIC: it tells the
agent where it lives, never that its primary provider is credit-dead, that it
has two open asks, or that 91 of its messages were suppressed today. So when
the owner asked "how's it going?" in prose, the agent confabulated a clean
state from stale context — the exact failure the 2026-08-28 Solana memo named
(a prompt that denies a live condition is a defect).

This mixin injects, at the FIRST step of every turn, a one-shot control
message rendered from the SAME snapshot the owner's ``/status`` shows
(``core/status_snapshot.py`` → ``render_agent_health_note``). Cheap path: no
money section, no network reads — a few read-only sqlite queries. Sub-agents
are excluded (a focused leaf never needs tenant-wide health). Gated
``LIVE_HEALTH_CONTEXT`` (default ON); fail-open — a builder error injects an
honest one-line "health unavailable" note rather than nothing, because
"nothing" is what confabulation grows from.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def build_live_health_text(user_id: str, *, data_dir=None, task_agent=None,
                           session_id=None) -> str:
    """The rendered note (sync; the mixin runs it in a worker thread)."""
    from core.status_snapshot import build_status_snapshot
    from core.status_render import render_agent_health_note
    snap = build_status_snapshot(str(user_id or ""), data_dir=data_dir,
                                 task_agent=task_agent, session_id=session_id,
                                 include_money=False)
    return render_agent_health_note(snap)


class LiveHealthMixin:
    async def _maybe_inject_live_health(self) -> None:
        """First step of every turn, main agent only. Never raises."""
        try:
            if getattr(self.state, "n_steps", 0) != 1:
                return
            if getattr(self, "_is_sub_agent", False):
                return
            from agents.task.constants import AutonomyConfig
            if not AutonomyConfig.live_health_context():
                return
            orch = getattr(self, "orchestrator", None)
            container = getattr(orch, "container", None)
            cfg = getattr(container, "config", None)
            task_agent = getattr(orch, "task_agent", None)
            try:
                text = await asyncio.to_thread(
                    build_live_health_text, getattr(self, "user_id", None),
                    data_dir=getattr(cfg, "data_dir", None), task_agent=task_agent,
                    session_id=getattr(self, "session_id", None))
            except Exception as e:
                text = (f"Live health: unavailable ({type(e).__name__}: {str(e)[:120]}) — "
                        "do not claim a clean state; call agent_status or say it is unknown.")
            from modules.llm.messages import MessageOrigin, make_control_message
            self.message_manager.push_ephemeral_message(
                make_control_message(f"<live-health>\n{text}\n</live-health>",
                                     MessageOrigin.SYSTEM_NOTE))
            self.logger.debug("Injected live-health note")
        except Exception as e:
            logger.debug("live-health injection skipped: %s", e)
