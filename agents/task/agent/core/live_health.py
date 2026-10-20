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
                           session_id=None, status_tool: bool = True,
                           with_operations: bool = False) -> str:
    """The rendered note (sync; the mixin runs it in a worker thread)."""
    from core.status_snapshot import build_status_snapshot
    from core.status_render import render_agent_health_note
    snap = build_status_snapshot(str(user_id or ""), data_dir=data_dir,
                                 task_agent=task_agent, session_id=session_id,
                                 include_money=False)
    text = render_agent_health_note(snap, status_tool=status_tool,
                                    with_operations=with_operations)
    # Review 2026-10-13 C-01/B5: the ONE "what may I spend now" read, from the
    # guard's own objects — so a cap is never rebuilt from an identity doc or
    # memory. Only where a wallet exists (no money, no limits block).
    from core.env import bool_env
    if bool_env("AGENT_WALLET_ENABLED", False):
        from core.money.limits import spend_limits_lines
        text = text + "\n" + "\n".join(spend_limits_lines())
    return text


def build_operations_text(user_id: str, *, data_dir=None) -> str:
    """The ``<operations>`` body: live jobs, open goals, ``/pause`` and owner
    stops, from the status SSOT's own ``work`` / ``loops`` builders (sync)."""
    from core.status_snapshot import build_operations_sections
    from core.status_render import render_operations_block
    return render_operations_block(
        build_operations_sections(str(user_id or ""), data_dir=data_dir))


def has_status_tool(controller) -> bool:
    """080 WS-5: True only when this session's controller registers
    ``agent_status`` — the note never names a tool the agent cannot call."""
    try:
        return "agent_status" in controller.registry.registry.actions
    except Exception:
        return False


def _operations_injected(agent) -> bool:
    """Whether this turn also carries ``<operations>`` (the owner-memory
    regimes): the ONE predicate both injectors read. Fail-closed."""
    try:
        from agents.task.session_class import regime_policy
        return bool(regime_policy(agent).owner_memory)
    except Exception as e:
        logger.warning("operations block not injected: regime unknown (%s)", e)
        return False


class LiveHealthMixin:
    async def _maybe_inject_live_health(self) -> None:
        """First step of every turn, main agent only. Never raises."""
        try:
            if getattr(self.state, "n_steps", 0) != 1:
                return
            if getattr(self, "_is_sub_agent", False):
                return
            from core.surfaces.room_policy import is_public_session
            if is_public_session(getattr(self, "orchestrator", None)):
                return  # 044 T4: no owner health note into a public room
            from agents.task.session_class import correspondent_facing
            if correspondent_facing(getattr(self, "orchestrator", None)):
                return  # H16: output goes to a correspondent — no owner-tenant state
            from core.config_policy import AutonomyConfig
            if not AutonomyConfig.live_health_context():
                return
            orch = getattr(self, "orchestrator", None)
            container = getattr(orch, "container", None)
            cfg = getattr(container, "config", None)
            task_agent = getattr(orch, "task_agent", None)
            status_tool = has_status_tool(getattr(self, "controller", None))
            try:
                text = await asyncio.to_thread(
                    build_live_health_text, getattr(self, "user_id", None),
                    data_dir=getattr(cfg, "data_dir", None), task_agent=task_agent,
                    session_id=getattr(self, "session_id", None), status_tool=status_tool,
                    with_operations=_operations_injected(self))
            except Exception as e:
                hint = "call agent_status or say" if status_tool else "say"
                text = (f"Live health: unavailable ({type(e).__name__}: {str(e)[:120]}) — "
                        f"do not claim a clean state; {hint} it is unknown.")
            from modules.llm.messages import MessageOrigin, make_control_message
            self.message_manager.push_ephemeral_message(
                make_control_message(f"<live-health>\n{text}\n</live-health>",
                                     MessageOrigin.SYSTEM_NOTE))
            self.logger.debug("Injected live-health note")
        except Exception as e:
            logger.debug("live-health injection skipped: %s", e)

    async def _maybe_inject_operations(self) -> None:
        """Review 2026-10-15 rec. 2: first step of every turn, the owner-memory
        regimes only (owner chat, cron, goal, autonomous — never a room, a
        correspondent or a child), a short ``<operations>`` note of what runs,
        what is open, what is paused and what the owner stopped. Read live, so a
        doc or recall line about the same thing cannot win. It rides the
        volatile tail (an ephemeral), outside the cached prefix. Never raises."""
        if getattr(self.state, "n_steps", 0) != 1:
            return
        if not _operations_injected(self):
            return  # fail closed: no owner state into an unknown session
        try:
            cfg = getattr(getattr(getattr(self, "orchestrator", None), "container", None),
                          "config", None)
            text = await asyncio.to_thread(build_operations_text, getattr(self, "user_id", None),
                                           data_dir=getattr(cfg, "data_dir", None))
        except Exception as e:
            text = (f"Live harness state: could not read ({type(e).__name__}: {str(e)[:120]}) "
                    f"— jobs, goals, pauses and stops are UNKNOWN, not none.")
        from modules.llm.messages import MessageOrigin, make_control_message
        self.message_manager.push_ephemeral_message(
            make_control_message(f"<operations>\n{text}\n</operations>",
                                 MessageOrigin.SYSTEM_NOTE))
