"""R1: conversational-exit policy for the agent run loop.

Pure helpers so the policy is unit-testable without a live Agent. The run loop
ends a turn when the agent keeps producing user-facing replies without calling
done() — otherwise a greeting/chat answer loops and re-greets (the 2026-06-14
Kimi anomaly). The rule is intentionally conservative:

  * A turn ends only after ``CONVERSATIONAL_EXIT_AFTER_REPLIES`` *consecutive*
    reply-only steps (no tool actually ran in any of them).
  * Any productive step (a real tool result) OR a planning turn resets the
    counter, so a task that does real work — even one that opens or closes with a
    single standalone status message — is never cut short.
  * Sub-agents never take this path; their turn boundary is owned by the parent.

A *blocking* send_message and done() already end the turn via ``is_done`` and are
not handled here (they are not tagged ``conversational_reply``).
"""
from __future__ import annotations

from typing import Iterable

# Number of back-to-back reply-only steps that ends a turn. 2 (not 1) leaves room
# for a task to follow a leading status message with real work on the next step.
CONVERSATIONAL_EXIT_AFTER_REPLIES = 2


def is_reply_only_step(results: Iterable) -> bool:
    """True if the step produced ≥1 result and EVERY result is a non-blocking
    user-facing reply (tagged ``metadata.conversational_reply``) — i.e. no tool
    actually executed this step.
    """
    results = list(results or [])
    if not results:
        return False
    return all(bool(getattr(r, "metadata", None)) and r.metadata.get("conversational_reply") for r in results)


def should_conversational_exit(consecutive_reply_steps: int, is_sub_agent: bool,
                               is_autonomous: bool = False) -> bool:
    """Whether the run loop should end the turn now (R1).

    T2-08: an AUTONOMOUS session (goal/cron/planner-spawned) is exempt like a
    sub-agent — there is no human chatting with it, so a run of status
    send_messages is progress narration, not a greeting loop; cutting the turn
    there ended missions mid-flight (and mislabeled the run's completion).
    """
    if is_sub_agent or is_autonomous:
        return False
    return consecutive_reply_steps >= CONVERSATIONAL_EXIT_AFTER_REPLIES


#: Said to the model at the one decision point the exit allowance creates. The
#: allowance (2 reply-only steps, not 1) exists so a lone status message can be
#: followed by real work — but it also hands the model an extra step in which a
#: weak model repeats a reply it already delivered (prod 2026-10-26: the owner got
#: the same reply twice). Rather than police the repeat after the fact, say plainly
#: what state the turn is in. It forbids nothing: more messages and more work stay
#: legal.
REPLY_DELIVERED_NOTE = (
    "Your reply was delivered to the user. If it fully answers them, call done() now "
    "- do not send it again. If you still have work to do, do that work now.")


def should_note_reply_delivered(consecutive_reply_steps: int, is_sub_agent: bool,
                                is_autonomous: bool = False) -> bool:
    """True on the FIRST reply-only step of a live chat turn (the step after which
    the model is offered one more step). Sub-agents and autonomous runs never take
    the exit path, so they get no note either."""
    if is_sub_agent or is_autonomous:
        return False
    return consecutive_reply_steps == 1


#: Prefix of the controller's retry-wall refusal (tools/controller/execution.py).
_RETRY_WALL = "\u26a0\ufe0f Maximum retries"


def is_retry_wall_only_step(results: Iterable) -> bool:
    """True if every result of this step is the controller's "Maximum retries
    exceeded" refusal - the operation was already declared dead and the model
    asked for it again anyway.

    Prod 2026-10-26 04:00-04:08Z: a job called ``wallet_holdings`` without its
    chain 150 times. The wall answered every call after the 5th, but a refusal is
    an error RESULT, not an exception, so ``consecutive_failures`` never moved and
    the run burned its whole 150-step budget (and posted nothing). A step made only
    of retry-wall refusals is a failed step; enough in a row ends the run.
    """
    results = list(results or [])
    if not results:
        return False
    def _is_wall(r) -> bool:
        if (getattr(r, "metadata", None) or {}).get("retry_wall"):
            return True
        err = getattr(r, "error", None)
        return isinstance(err, str) and err.startswith(_RETRY_WALL)
    return all(_is_wall(r) for r in results)


def is_planning_turn_only_step(results: Iterable) -> bool:
    """True if every result of this step is the ALLOWED_REASONING_TURNS
    placeholder (tagged ``metadata.planning_turn``, from
    ``step_execution.py``'s bounded tool-free-turn allowance) — i.e. no tool
    ran and no thinking-loop intervention fired either.

    2026-08-18: the run loop's "reset consecutive_failures on a successful
    step" check only tested ``not any(r.error ...)`` — a planning turn's
    result has no error, so it was ALSO treated as a successful step. A model
    that alternates [planning turn (error-free, resets the counter to 0),
    thinking-loop intervention (error, bumps the counter to 1)] can cycle
    forever: the intervention's +1 gets wiped by the very next planning turn,
    every single time, so ``consecutive_failures`` never reaches
    ``max_failures`` and the safety net never trips. Confirmed empirically as
    the dominant failure mode of the 2026-08-18 post-outage recovery burst
    (82% of goal failures shared this exact signature, several runs never
    recovering even once, straight through to step exhaustion). A planning
    turn is a bounded allowance, not genuine progress, so it must not count
    as the step that clears the failure counter.
    """
    results = list(results or [])
    if not results:
        return False
    return all(bool(getattr(r, "metadata", None)) and r.metadata.get("planning_turn") for r in results)
