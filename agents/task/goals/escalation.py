"""Blocker → owner escalation (§7.2).

Historically a goal that tripped the circuit breaker went to ``status='blocked'`` +
a ``gave_up`` audit event and then **died silently** — nothing surfaced it to the
owner. This module is the missing producer: when a goal blocks, push a concrete,
specific ask to the owner over the SAME rail an out-of-band cron report uses
(``self_evolution.push_owner_message`` → telegram sink + ``resolve_owner_telegram_id``).

One sweep on the dispatch tick tells every stop, whichever path made it: the run's
breaker, a crash reclaim, a dependency cascade or the age-out.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


def build_blocker_escalation(goal: Any) -> str:
    """A concrete owner-facing ask for a blocked goal (not just a status line)."""
    title = getattr(goal, "title", None) or "(untitled goal)"
    reason = _reason(goal)
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


#: A stop is told after this many seconds, so the run that blocked the goal can
#: stamp the block kind, and say that it already told the owner, before the sweep reads it.
STOP_SETTLE_SEC = 60
#: Lines in one notice; the rest are counted and left to ``/goals``.
STOP_LINES = 5
_TOLD_KEY = "stop_told"


def _stop_time(goal: Any) -> float:
    return goal.completed_at if goal.completed_at is not None else goal.created_at


def _heals_itself(goal: Any) -> bool:
    """Only the age-out sweep requeues an outage, and ``0`` days turns that sweep off."""
    payload = goal.payload or {}
    if (payload.get("block_kind") != "provider_outage"
            or payload.get("provider_retry_exhausted")):
        return False
    from core.config_policy import AutonomyConfig
    return AutonomyConfig.goal_blocked_max_age_days() > 0


def _already_told(goal: Any) -> bool:
    told = (goal.payload or {}).get(_TOLD_KEY)
    return told is not None and abs(float(told) - float(_stop_time(goal))) < 0.001


def untold_stops(board: Any, now: float) -> list:
    """The goals that stopped and the owner has not heard of. ``completed_at`` names
    the stop, so a new block after a retry is a new stop and nothing clears the key."""
    from core.sqlite_util import execute_retry
    from agents.task.goals.board import Goal
    rows = execute_retry(
        board.db_path,
        f"""SELECT * FROM goals WHERE kind='goal' AND (status='blocked'
              OR (status='cancelled' AND json_extract(payload,'$.aged_out')=1
                  AND json_extract(payload,'$.{_TOLD_KEY}') IS NULL))""",
        (), fetch="all") or []
    goals = [Goal.from_row(r) for r in rows]
    return [g for g in goals
            if now - _stop_time(g) >= STOP_SETTLE_SEC
            and not _heals_itself(g) and not _already_told(g)]


def _claim_stop(board: Any, goal: Any) -> bool:
    """Compare-and-set on the key, because the sweeps run before the tick lock:
    two workers must not tell the same stop."""
    from core.sqlite_util import execute_retry
    from agents.task.goals.payload_keys import payload_expr
    expr, params = payload_expr({_TOLD_KEY: _stop_time(goal)})
    prev = (goal.payload or {}).get(_TOLD_KEY)
    # ⚠️ Never `IS ?` on the float: SQLite's JSON parse and Python's can differ by one
    # ulp for the same text (about 1 time stamp in 8 on SQLite 3.40), and then a later
    # stop of the goal was never told. Compare like ``_already_told`` does.
    if prev is None:
        cond, cparams = f"json_extract(payload,'$.{_TOLD_KEY}') IS NULL", ()
    else:
        cond, cparams = f"abs(json_extract(payload,'$.{_TOLD_KEY}') - ?) < 0.001", (float(prev),)
    return execute_retry(
        board.db_path, f"UPDATE goals SET payload={expr} WHERE id=? AND {cond}",
        (*params, goal.id, *cparams)) == 1


def _release_stop(board: Any, goal: Any) -> None:
    from agents.task.goals.payload_keys import patch
    patch(board.db_path, goal.id, remove=(_TOLD_KEY,))


def mark_stop_told(board: Any, goal_id: str) -> None:
    """The run told the owner about this block itself, so the sweep stays quiet.
    Only a goal that is blocked now has a stop to mark: a mark on a goal that went
    back to ready names an old stop time and could silence a later block."""
    from agents.task.goals.board import STATUS_BLOCKED
    from agents.task.goals.payload_keys import patch
    goal = board.get(goal_id)
    if goal is not None and goal.status == STATUS_BLOCKED:
        patch(board.db_path, goal_id, set_keys={_TOLD_KEY: _stop_time(goal)})


def _reason(goal: Any) -> str:
    if (getattr(goal, "payload", None) or {}).get("block_kind") == "dep_failed":
        return "a goal it depends on stopped"
    return (getattr(goal, "last_failure_error", None) or "repeated failures").strip()


def _stop_line(goal: Any) -> str:
    gid = goal.id[:8]
    reason = _reason(goal).split("\n", 1)[0][:100]
    try:
        from core.owner_remedy import correction_line
        if correction_line(reason, real_options=""):
            reason = f"see /goal show {gid}"  # the agent's text names a verb no seat has
    except Exception:
        logger.debug("remedy check failed; line sent unchecked", exc_info=True)
    if goal.status == "cancelled":
        return f"• {goal.title[:60]}: cancelled, blocked too long ({reason}). /goal new starts it again."
    return f"• {goal.title[:60]}: {reason}. /goal show {gid}"


def build_stop_notice(goals: list) -> str:
    """One message for what this tick found: the full ask for one blocked goal, else lines."""
    if len(goals) == 1 and goals[0].status != "cancelled":
        return build_blocker_escalation(goals[0])
    lines = [f"🚧 {len(goals)} goal(s) stopped and need you:" if len(goals) > 1
             else "🚧 A goal stopped:"]
    lines += [_stop_line(g) for g in goals[:STOP_LINES]]
    if len(goals) > STOP_LINES:
        lines.append(f"…and {len(goals) - STOP_LINES} more.")
    lines.append("/goals shows them all.")
    return "\n".join(lines)


def _owner_known() -> bool:
    """The rail records nothing for no owner, so a stop sent then would be lost."""
    try:
        from core.instance import resolve_owner_user_id
        return bool(resolve_owner_user_id())
    except Exception:
        return False


async def tell_stops(board: Any, task_agent: Any, *, now: Optional[float] = None) -> int:
    """Tell the owner once about every goal that stopped since the last tick: a blocked
    goal from any path, and a goal the age-out cancelled. A provider outage that can still
    heal waits until it stops healing. Returns how many stops this call told; fail-open.

    The rail keeps what it could not deliver for ``/missed``, so any rail outcome counts
    as told. Only a message that never reached the rail is tried again."""
    container = getattr(task_agent, "container", None)
    if container is None or not _owner_known():
        return 0
    stops = untold_stops(board, time.time() if now is None else now)
    claimed = [g for g in stops if _claim_stop(board, g)]
    if not claimed:
        return 0
    from core.self_evolution import _push_owner_message_outcome
    # source="goal_blocked" is the critical lane: the daily chatter cap may not drop a
    # stopped goal (prod suppressed 156 self_evolution messages in 8 days, escalations
    # among them, leaving 44 asks unanswered for a month).
    # The time keeps a new stop with the same reason from reading as a repeat to the rail.
    at = datetime.fromtimestamp(max(_stop_time(g) for g in claimed)).strftime("%m-%d %H:%M")
    outcome = await _push_owner_message_outcome(
        container, f"{build_stop_notice(claimed)}\n(stopped {at})", source="goal_blocked")
    if outcome is None:
        for goal in claimed:
            _release_stop(board, goal)
        return 0
    from agents.task.goals.board_asks import create_auto_unblock_ask
    for goal in claimed:
        try:
            kind = (goal.payload or {}).get("block_kind")
            if goal.status != "blocked" or kind not in (None, "dep_failed"):
                continue  # a run stamped its kind beside its own ask, which may be answered
            # A crash or a cascade did not pass through the run's own ask.
            board.stamp_block_kind(goal.id, "needs_input", only_if_absent=True)
            create_auto_unblock_ask(board, goal, why=_reason(goal))
        except Exception:
            logger.debug("stop ask skipped for %s", goal.id, exc_info=True)
    logger.info("goal stops told to the owner: %d", len(claimed))
    return len(claimed)


async def tell_unverified(task_agent: Any, goal: Any, reason: str) -> bool:
    """One line for a goal that completed with no verified proof. Fail-open."""
    try:
        from core.self_evolution import push_owner_message
        text = (f"⚠ Goal \"{goal.title[:60]}\" finished, but nothing verified it: "
                f"{str(reason or 'no reason given')[:200]}. /goal show {goal.id[:8]}")
        return bool(await push_owner_message(getattr(task_agent, "container", None), text,
                                             source="goal_unverified"))
    except Exception:
        logger.debug("unverified line skipped for %s", getattr(goal, "id", "?"), exc_info=True)
        return False


__all__ = [
    "build_blocker_escalation",
    "build_stop_notice",
    "mark_stop_told",
    "tell_stops",
    "tell_unverified",
    "untold_stops",
]
