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


def should_conversational_exit(consecutive_reply_steps: int, is_sub_agent: bool) -> bool:
    """Whether the run loop should end the turn now (R1)."""
    if is_sub_agent:
        return False
    return consecutive_reply_steps >= CONVERSATIONAL_EXIT_AFTER_REPLIES
