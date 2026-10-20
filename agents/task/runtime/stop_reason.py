"""Why a run stopped — one typed reason, set at the exit that ended the loop (083 WS-1).

Before this, every exit without ``done()`` was recorded with the same guess
("likely ran out of steps"): a stall, consecutive failures, a control stop, a
money-infra break and a cancel all read alike, so an owner could not tell a
step-budget problem from anything else. The run loop now tags each exit with a
:class:`StopReason` on ``AgentState.stop_reason`` (first writer wins), the run
envelope carries it, and the goal / cron / delegation records state it in plain
words with ``steps/max_steps``.

Pure and dependency-free: ``agents``, ``tools`` and ``cron`` may all import it.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional


class StopReason(str, Enum):
    DONE = "done"
    AWAITING_REPLY = "awaiting_reply"
    MAX_STEPS = "max_steps"
    FAILURES = "failures"
    ERROR = "error"
    STALL = "stall"
    RUN_BUDGET = "run_budget"
    MONEY_INFRA = "money_infra"
    CONVERSATIONAL_EXIT = "conversational_exit"
    CONTROL = "control"
    CANCELLED = "cancelled"
    WALL_CLOCK = "wall_clock"


_TEXT = {
    StopReason.DONE: "finished with done()",
    StopReason.AWAITING_REPLY: "paused to wait for a reply",
    StopReason.MAX_STEPS: "stopped at the step limit",
    StopReason.FAILURES: "stopped after too many consecutive failed steps",
    StopReason.ERROR: "stopped by a fatal error",
    StopReason.STALL: "stopped because no step finished within the stall timeout",
    StopReason.RUN_BUDGET: "stopped by the run cost budget",
    StopReason.MONEY_INFRA: ("stopped after a money verb failed for a host reason "
                             "(nothing was sent)"),
    StopReason.CONVERSATIONAL_EXIT: "ended after reply-only steps",
    StopReason.CONTROL: "stopped by a stop control",
    StopReason.CANCELLED: "cancelled",
    StopReason.WALL_CLOCK: "cut by the wall-clock cap",
}


def _value(reason: Any) -> Optional[str]:
    if isinstance(reason, StopReason):
        return reason.value
    return reason if isinstance(reason, str) and reason else None


def set_stop(agent: Any, reason: Any) -> None:
    """Record *reason* on ``agent.state.stop_reason`` unless one is already set
    (first writer wins: the exit that really ended the run names it). Never raises."""
    try:
        state = getattr(agent, "state", None)
        if state is not None and not getattr(state, "stop_reason", None):
            state.stop_reason = _value(reason)
    except Exception:
        pass


def _count(steps: Optional[int], max_steps: Optional[int]) -> str:
    try:
        if steps is None and not max_steps:
            return ""
        n = int(steps or 0)
        if max_steps:
            return f"{n}/{int(max_steps)}"
        return f"step {n}"
    except (TypeError, ValueError):
        return ""


def describe_stop_reason(reason: Any, steps: Optional[int] = None,
                         max_steps: Optional[int] = None) -> str:
    """Plain words for a stop reason, with ``n/max`` steps when known.

    ``describe_stop_reason("max_steps", 30, 30)`` →
    ``"stopped at the step limit (30/30)"``. An unknown or missing reason says
    so instead of guessing a cause."""
    value = _value(reason)
    try:
        text = _TEXT[StopReason(value)] if value else "stop reason unknown"
    except ValueError:
        text = f"stop reason {value!r}"
    count = _count(steps, max_steps)
    return f"{text} ({count})" if count else text


def done_stop_reason(results: Any) -> StopReason:
    """The reason for a loop exit on an ``is_done`` result: ``error`` when the
    terminal result carries an error (error_recovery's permanent-error halt sets
    ``is_done`` too), ``awaiting_reply`` for a blocking ``send_message`` that
    paused the run, else ``done``."""
    try:
        items = list(results or [])
        if any(getattr(r, "is_done", False) and getattr(r, "error", None) for r in items):
            return StopReason.ERROR
        if any((getattr(r, "metadata", None) or {}).get("awaiting_reply") for r in items):
            return StopReason.AWAITING_REPLY
    except Exception:
        pass
    return StopReason.DONE


#: The label every consumer puts before the agent's last report at the step
#: limit (083 WS-2). The report is model-authored and may claim success; it is
#: never a done(), never fed to the judge or to acceptance.
LAST_REPORT_LABEL = "Last report (agent's words): "


def label_last_report(text: Optional[str], limit: int = 600) -> str:
    """``LAST_REPORT_LABEL`` + the first *limit* chars of *text*, or ''."""
    t = (text or "").strip() if isinstance(text, str) else ""
    return f"{LAST_REPORT_LABEL}{t[:limit]}" if t else ""
