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
    goal_id = (getattr(goal, "id", None) or "")[:8]

    # `reason` is the agent's OWN `last_failure_error` prose, forwarded verbatim
    # to the owner's phone. On 2026-09-08 that carried "re-activate the standing
    # Treasury goal ... e.g. `goal_update`/requeue on your side" -- a verb that
    # exists on no surface, for a grant that was never missing. The owner, who
    # does not use the CLI, went looking for it.
    #
    # We do NOT delete the agent's diagnosis: it is often right about the WHAT
    # and wrong only about the remedy, and hiding it would cost more than it
    # saves. We label the invented actions and name real ones instead.
    correction = ""
    try:
        from core.owner_remedy import correction_line

        shown = f" {goal_id}" if goal_id else " <id>"
        correction = correction_line(
            reason, real_options=f"/goal show{shown} · /goals · /asks")
    except Exception:            # never let a cosmetic check break an escalation
        logger.debug("remedy check failed; sending escalation unchecked",
                     exc_info=True)

    return (
        f"🚧 I'm blocked and stopped retrying{tail}.\n"
        f"Goal: {title}\n"
        f"Why: {reason}{correction}\n"
        f"What do you want me to do — drop it, give me what it needs, or should I "
        f"try a different approach?"
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
        from core.config_policy import AutonomyConfig
        if not AutonomyConfig.goal_blocker_escalation():
            return False
        container = getattr(task_agent, "container", None)
        from core.self_evolution import push_owner_message
        # source="goal_blocked" puts this on the CRITICAL lane: a stopped goal
        # needing the owner must not be droppable by the daily chatter cap
        # (prod suppressed 156 self_evolution messages in 8 days, escalations
        # among them, leaving 44 asks unanswered for a month).
        sent = await push_owner_message(container, build_blocker_escalation(goal),
                                        source="goal_blocked")
        if sent:
            logger.info("goal %s blocked → owner escalation sent", getattr(goal, "id", "?"))
        return bool(sent)
    except Exception as e:  # never let escalation break the failure path
        logger.debug("blocker escalation skipped (fail-open): %s", e)
        return False


__all__ = [
    "build_blocker_escalation",
    "maybe_escalate_blocked",
]
