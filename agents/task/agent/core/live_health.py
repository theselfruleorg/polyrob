"""Per-turn ``<live-health>`` note for the agent (2026-08-28 status SSOT, D10).

The ``<environment>`` foundation block is per-session STATIC: it tells the
agent where it lives, never that its primary provider is credit-dead, that it
has two open asks, or that 91 of its messages were suppressed today. So when
the owner asked "how's it going?" in prose, the agent confabulated a clean
state from stale context — the exact failure the 2026-08-28 Solana memo named
(a prompt that denies a live condition is a defect).

This mixin renders, at EVERY step boundary, the ``live_health`` part of the
``<state-delta>`` channel (095 W4) from the SAME snapshot the owner's
``/status`` shows (``core/status_snapshot.py`` → ``render_agent_health_note``);
it is pushed only when it changed. Cheap path: no
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


def model_routing_line(user_id: str) -> str:
    """094: which model each kind of run starts on — so the agent can answer
    "what will the nightly job run on" without reading env files (``agent_status``;
    not in ``<operations>``, which rides every turn). "" on error."""
    try:
        from core.model_pins import chat_runtime_for
        from core.runtime_config import heavy_model_pin, resolve_default_provider
        from modules.llm.llm_client_registry import get_default_model
        c_provider, c_model, _r, c_source = chat_runtime_for(user_id)
        a_provider, a_model = resolve_default_provider()[:2]
        c_model = c_model or (get_default_model(c_provider) if c_provider else "?")
        a_model = a_model or (get_default_model(a_provider) if a_provider else "?")
        heavy = heavy_model_pin()
        return (f"Models: a new owner chat starts on {c_model} ({c_provider}; {c_source}); "
                f"autonomous runs use {a_model}; money-class runs use "
                f"{heavy or 'the same (HEAVY_MODEL unset)'}. The owner switches a chat with /model.")
    except Exception:
        logger.debug("model routing line skipped", exc_info=True)
        return ""


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


def live_health_injected(agent) -> bool:
    """095 W3: does this agent get a ``<live-health>`` note each turn? The ONE
    predicate the injector and the ``<environment>`` sentence share: a main
    agent, not a public room (044 T4), not correspondent-facing (H16), and
    ``LIVE_HEALTH_CONTEXT`` on. Fail-closed."""
    try:
        if getattr(agent, "_is_sub_agent", False) or getattr(agent, "_role", None) == "leaf":
            return False
        from core.surfaces.room_policy import is_public_session
        from agents.task.session_class import correspondent_facing
        orch = getattr(agent, "orchestrator", None)
        if is_public_session(orch) or correspondent_facing(orch):
            return False
        from core.config_policy import AutonomyConfig
        return bool(AutonomyConfig.live_health_context())
    except Exception:
        return False


class LiveHealthMixin:
    async def _live_health_part(self) -> str:
        """The ``<live-health>`` text, or "" where this seat gets none."""
        if not live_health_injected(self):
            return ""
        orch = getattr(self, "orchestrator", None)
        cfg = getattr(getattr(orch, "container", None), "config", None)
        status_tool = has_status_tool(getattr(self, "controller", None))
        try:
            return await asyncio.to_thread(
                build_live_health_text, getattr(self, "user_id", None),
                data_dir=getattr(cfg, "data_dir", None),
                task_agent=getattr(orch, "task_agent", None),
                session_id=getattr(self, "session_id", None), status_tool=status_tool,
                with_operations=_operations_injected(self))
        except Exception as e:
            hint = "call agent_status or say" if status_tool else "say"
            return (f"Live health: unavailable ({type(e).__name__}: {str(e)[:120]}) — "
                    f"do not claim a clean state; {hint} it is unknown.")

    async def _operations_part(self) -> str:
        """Review 2026-10-15 rec. 2: the ``<operations>`` text — what runs, what
        is open, paused or stopped — for the owner-memory regimes only (owner
        chat, cron, goal, autonomous; never a room, a correspondent or a child).
        Read live, so a doc or recall line about the same thing cannot win.
        "" elsewhere (SK-18: a seat tainted mid-session drops it)."""
        if not _operations_injected(self):
            return ""  # fail closed: no owner state into an unknown session
        try:
            cfg = getattr(getattr(getattr(self, "orchestrator", None), "container", None),
                          "config", None)
            return await asyncio.to_thread(build_operations_text, getattr(self, "user_id", None),
                                           data_dir=getattr(cfg, "data_dir", None))
        except Exception as e:
            return (f"Live harness state: could not read ({type(e).__name__}: {str(e)[:120]}) "
                    f"— jobs, goals, pauses and stops are UNKNOWN, not none.")

    async def _maybe_update_state_delta(self) -> None:
        """095 W4: EVERY step boundary — ``<live-health>``, ``<operations>`` and
        the skill-index change go to ``MessageManager.update_state_delta``, which
        pushes a part only when its text changed (one durable message at the
        tail, outside the cached prefix). Never raises."""
        try:
            from agents.task.agent.core.state_delta import skills_delta_part
            parts = {"live_health": await self._live_health_part(),
                     "operations": await self._operations_part(),
                     "skills_delta": skills_delta_part(self)}
            self.message_manager.update_state_delta(parts)
        except Exception as e:
            logger.debug("state-delta update skipped: %s", e)
