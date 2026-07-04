"""Blocker → owner escalation (§7.2).

Historically a goal that tripped the circuit breaker went to ``status='blocked'`` +
a ``gave_up`` audit event and then **died silently** — nothing surfaced it to the
owner. This module is the missing producer: when a goal blocks, push a concrete,
specific ask to the owner over the SAME rail an out-of-band cron report uses
(``self_evolution.push_owner_message`` → telegram sink + ``resolve_owner_telegram_id``).

Pure builders + one gated, fail-open async producer so it is trivially testable and
can never break the dispatcher's failure path.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_blocker_escalation(goal: Any) -> str:
    """A concrete owner-facing ask for a blocked goal (not just a status line)."""
    title = getattr(goal, "title", None) or "(untitled goal)"
    reason = (getattr(goal, "last_failure_error", None) or "repeated failures").strip()
    fails = getattr(goal, "consecutive_failures", None)
    tail = f" after {fails} attempts" if fails else ""
    return (
        f"🚧 I'm blocked and stopped retrying{tail}.\n"
        f"Goal: {title}\n"
        f"Why: {reason}\n"
        f"What do you want me to do — drop it, give me what it needs, or should I "
        f"try a different approach?"
    )


def build_empty_pipeline_escalation(objective_title: str | None) -> str:
    """An owner-facing ask when the board has drained with no next goal to run."""
    obj = objective_title or "the current objective"
    return (
        f"🫗 My goal pipeline is empty — I have nothing queued for {obj}.\n"
        f"Tell me the next concrete step, or what's blocking progress, and I'll pick it up."
    )


async def maybe_escalate_blocked(task_agent: Any, goal: Any) -> bool:
    """Escalate a BLOCKED goal to the owner if enabled. No-op + fail-open otherwise.

    Gated ``GOAL_BLOCKER_ESCALATION`` (default OFF). Only fires for a goal whose
    status is ``blocked`` (the circuit breaker tripped), never a transient retry.
    """
    try:
        from agents.task.goals.board import STATUS_BLOCKED
        if getattr(goal, "status", None) != STATUS_BLOCKED:
            return False
        from agents.task.constants import AutonomyConfig
        if not AutonomyConfig.goal_blocker_escalation():
            return False
        container = getattr(task_agent, "container", None)
        from core.self_evolution import push_owner_message
        sent = await push_owner_message(container, build_blocker_escalation(goal))
        if sent:
            logger.info("goal %s blocked → owner escalation sent", getattr(goal, "id", "?"))
        return bool(sent)
    except Exception as e:  # never let escalation break the failure path
        logger.debug("blocker escalation skipped (fail-open): %s", e)
        return False


__all__ = [
    "build_blocker_escalation",
    "build_empty_pipeline_escalation",
    "maybe_escalate_blocked",
]
