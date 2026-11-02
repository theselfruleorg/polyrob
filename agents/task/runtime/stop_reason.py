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
    LOOP = "loop"


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
    StopReason.LOOP: "stopped because it repeated the same tool call",
}


#: 095 W6 — "why did you not reply": one owner sentence and the next step for
#: each stop reason. The guide FAQ renders its troubleshooting rows from here.
OWNER_EXPLAIN = {
    StopReason.DONE: (
        "The run finished. If no answer reached you, a delivery limit or a pause "
        "held it.", "`/missed`"),
    StopReason.AWAITING_REPLY: (
        "A question to you is open; the run waits for your answer.", "answer in this chat, or `/inbox`"),
    StopReason.MAX_STEPS: (
        "The run used every step it may take.",
        "say continue in this chat; for a goal, `/goal retry <id>`"),
    StopReason.FAILURES: (
        "Too many steps in a row failed, so the run stopped.",
        "`/status`, then `/why` for the refusals"),
    StopReason.ERROR: (
        "An error that a retry cannot fix ended the run.", "`/status`"),
    StopReason.STALL: (
        "A provider or a tool did not answer in time.", "`/status`, then say continue"),
    StopReason.RUN_BUDGET: (
        "The run reached its AI cost limit.", "`/usage`"),
    StopReason.MONEY_INFRA: (
        "A money step failed on the host side. Nothing was sent.", "`/status`, then `/book`"),
    StopReason.CONVERSATIONAL_EXIT: (
        "The run ended after it replied; the reply was the answer.", "`/missed` if none came"),
    StopReason.CONTROL: (
        "A stop control ended the run: a pause or a stop on that run.",
        "`/status`; `/resume` lifts a pause"),
    StopReason.CANCELLED: (
        "The run was cancelled, for example by `/cancel`.", "send the task again"),
    StopReason.WALL_CLOCK: (
        "The run reached its time limit.", "for a goal, `/goal retry <id>`"),
    StopReason.LOOP: (
        "The same tool call repeated, so the harness stopped the run.",
        "say continue with a hint, or `/why`"),
}


#: F7 (prod review 2026-10-29): a run that ended at the step limit, after
#: consecutive failures, or by repeating one call wrote phase summaries that
#: recall then served as fact ("six winners paid", a wrong tweet id). Its own
#: account of what happened does not become cross-session memory.
NOT_MEMORY_WORTHY = frozenset({StopReason.MAX_STEPS.value, StopReason.FAILURES.value,
                               StopReason.LOOP.value})


def memory_worthy(reason: Any) -> bool:
    """False for a stop reason in :data:`NOT_MEMORY_WORTHY`; True otherwise
    (no reason yet = a run still going or one that ended normally)."""
    return _value(reason) not in NOT_MEMORY_WORTHY


#: Stop reasons after which an owner chat turn needs no word from the harness:
#: it finished, it paused for the owner's reply, the owner stopped it, or it
#: ended by the R1 conversational exit — two reply-only steps, so the turn
#: already answered with send_message (a "turn ended early" notice there was
#: false and went into the owner thread tail).
QUIET_STOPS = frozenset({StopReason.DONE.value, StopReason.AWAITING_REPLY.value,
                         StopReason.CANCELLED.value, StopReason.CONTROL.value,
                         StopReason.CONVERSATIONAL_EXIT.value})

#: The last line of every turn-end notice.
CONTINUE_HINT = "Say continue and I pick up where I stopped."


def _one_line(text: Any, limit: int = 200) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[:limit - 1] + "…"


def turn_end_notice(reason: Any, steps: Optional[int] = None,
                    max_steps: Optional[int] = None, *, last_error: str = "",
                    last_report: str = "") -> str:
    """What an owner chat turn says when its run ended without finishing (the
    step limit, a loop, failures, an error): the typed stop reason in plain
    words, the agent's last report when there is one, the last tool error in
    one line, and how to resume. Pure."""
    lines = [f"⚠️ My turn ended early: {describe_stop_reason(reason, steps, max_steps)}."]
    report = label_last_report(last_report)
    if report:
        lines.append(report)
    if (last_error or "").strip():
        lines.append(f"Last tool error: {_one_line(last_error)}")
    lines.append(CONTINUE_HINT)
    return "\n".join(lines)


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
