"""Shared session-execution helpers used by cron runner and goal dispatcher.

Canonical location for:
- ``_RUN_REFUSALS`` / ``is_refusal`` — moved verbatim from ``cron/runner.py``
- ``run_task_as_session`` — the common create_session → run_session flow
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Known non-completion returns from TaskAgent.run_session — truthy strings that
# mean "the loop did NOT run", so callers must treat them as failure.
_RUN_REFUSALS = (
    "task package not available",
    "no active session found",
    "session not found or unauthorized",
    "session is already executing",
    # Terminal non-success returns from run_session: a 'failed'-status session
    # ("Session failed: ...") and a credit-suspended one ("Session suspended: ...")
    # must count as a goal/cron FAILURE, not be recorded as success (live-test F7).
    "session failed",
    "session suspended",
)


def is_refusal(final: Optional[str]) -> bool:
    """Return True if *final* is falsy or matches a known non-completion prefix."""
    if not final:
        return True
    low = final.strip().lower()
    return any(low.startswith(p) for p in _RUN_REFUSALS)


async def run_task_as_session(
    task_agent: Any,
    *,
    user_id: str,
    request: dict,
    autonomous: bool = False,
) -> tuple[Optional[str], Optional[str]]:
    """Factor the shared create_session → run_session → refusal-check flow.

    *task_agent* must implement:
    - ``create_session(user_id, request)`` → dict with ``"id"`` key, or falsy
    - ``run_session(user_id, session_id)`` → str result or refusal string

    *autonomous* marks the created session (goal/cron/planner-spawned) in the
    in-process autonomy registry (``agents.task.goals.autonomy_marker``) so
    the goal tool can refuse objective mutations from that session later.

    Returns a ``(session_id, final)`` tuple with three shapes:

    - ``(None, None)``   — no session was created (create_session returned no id)
    - ``(session_id, None)``  — session created but run_session returned a known
      refusal/empty string; callers should treat this as a soft failure
    - ``(session_id, final)`` — session ran and produced a genuine result string
    """
    session_info = await task_agent.create_session(user_id=user_id, request=request)
    session_id = (session_info or {}).get("id")
    if not session_id:
        return (None, None)
    if autonomous:
        from agents.task.goals.autonomy_marker import mark_autonomous
        mark_autonomous(session_id)
    status = await task_agent.run_session(user_id, session_id)
    if is_refusal(status):
        return (session_id, None)
    # run_session returns a generic STATUS string ("Session completed successfully"),
    # NOT the agent's actual output — so out-of-band delivery (cron digest, goal board)
    # was shipping that useless string to the owner (the "blind" digest) and [SILENT]
    # could never suppress. Extract the agent's REAL reply the way chat_once / the
    # telegram surface (004) do, and deliver THAT. Degrade to the status string only if
    # extraction yields nothing — never worse than before. Refusals short-circuit above,
    # so a failed run never delivers a stale prior reply.
    final = status
    extract = getattr(task_agent, "_extract_chat_reply", None)
    if extract is not None:
        try:
            content = extract(session_id)
        except Exception as e:  # pragma: no cover - fail-open to the status string
            logger.debug("run_task_as_session: extract reply failed: %s", e)
            content = None
        if content and content.strip():
            final = content.strip()
    return (session_id, final)
