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


def completed_via_done(orchestrator: Any) -> Optional[bool]:
    """T2-01: did the run genuinely finish via ``done()``, or just stop?

    ``run_session`` returns the SAME status string ("Session completed successfully")
    whether the agent called ``done()`` or the loop merely ran out of steps / drifted
    into a reply-only conversational exit — so the string can't distinguish a real
    completion from an exhausted one. A goal run that exhausts ``max_steps`` without
    delivering was being recorded as board success (the prod "announce OSS -> marked
    done, never posted" shape).

    Inspect the resident orchestrator's MAIN-agent last-result set (the same
    ``any(r.is_done for r in _last_result)`` signal the run loop logs at
    ``run_loop.py:584``). Scoped to the autonomous goal/cron callers on purpose — the
    global ``_result_session_status`` must NOT change, since a chat turn legitimately
    ends via conversational-exit without ``done()``.

    Returns:
        ``True``  — a main agent's last result carries a genuine ``is_done``.
        ``False`` — we read a main agent's last result and NONE were done (ran out).
        ``None``  — undeterminable (no orchestrator / non-resident / missing attrs);
                    callers MUST fall back to legacy behavior so an introspection
                    miss never flips a real completion to failure.
    """
    if orchestrator is None:
        return None
    try:
        agents = list((getattr(orchestrator, "agents", None) or {}).values())
        mains = [a for a in agents if not getattr(a, "_is_sub_agent", False)]
        if not mains:
            return None
        saw_result = False
        for a in mains:
            last = getattr(a, "_last_result", None)
            if last:
                saw_result = True
                if any(getattr(r, "is_done", False) for r in last):
                    return True
        return False if saw_result else None
    except Exception:  # pragma: no cover - defensive; unknown => legacy behavior
        return None


async def run_task_to_outcome(
    task_agent: Any,
    *,
    user_id: str,
    request: dict,
    autonomous: bool = False,
):
    """The primary run entry (§2): create_session → run_session → RunOutcome.

    *task_agent* must implement:
    - ``create_session(user_id, request)`` → dict with ``"id"`` key, or falsy
    - ``run_session(user_id, session_id)`` → str result or refusal string

    *autonomous* marks the created session (goal/cron/planner-spawned) in the
    in-process autonomy registry (``agents.task.goals.autonomy_marker``) so
    the goal tool can refuse objective mutations from that session later.

    Returns a ``RunOutcome`` assembled while the orchestrator is still resident
    (``session_id is None`` when no session was created; ``refusal=True`` when
    run_session returned a known non-completion string). Consumers read the
    done() text, BLOCKED declaration, user messages and provenance from the
    envelope — never by re-extracting strings from message history.
    """
    from agents.task.runtime.run_outcome import RunOutcome, build_run_outcome

    # §3.3: pre-generate + pre-mark the session id for AUTONOMOUS runs so the
    # marker is visible DURING construction — the communication-contract block
    # gates on it at prompt-build time. Only when the task_agent's
    # create_session accepts a session_id (the real TaskAgent does); legacy
    # fakes/custom agents keep the post-create marking below.
    pre_sid = None
    if autonomous:
        try:
            import inspect
            import uuid
            sig = inspect.signature(task_agent.create_session)
            if "session_id" in sig.parameters or any(
                    p.kind is inspect.Parameter.VAR_KEYWORD
                    for p in sig.parameters.values()):
                pre_sid = str(uuid.uuid4())
        except Exception:
            pre_sid = None
        if pre_sid:
            from agents.task.goals.autonomy_marker import mark_autonomous
            mark_autonomous(pre_sid)

    kwargs = {"session_id": pre_sid} if pre_sid else {}
    session_info = await task_agent.create_session(
        user_id=user_id, request=request, **kwargs)
    session_id = (session_info or {}).get("id")
    if not session_id:
        return RunOutcome(session_id=None)
    if autonomous:
        from agents.task.goals.autonomy_marker import mark_autonomous
        mark_autonomous(session_id)
    status = await task_agent.run_session(user_id, session_id)
    outcome = await build_run_outcome(task_agent, session_id, status)
    try:
        # Opt-in trajectory capture (TRAJECTORY_CAPTURE, datagen W1 T6).
        # maybe_capture is fail-open internally; this guard is belt-and-braces
        # so capture can never break a run even on import failure. Runs in a
        # worker thread — it does sync glob/read/sqlite/write over the whole
        # session, which on a big session stalls every other coroutine (H5,
        # 2026-07-14 review).
        from datagen.capture import maybe_capture, trajectory_capture_enabled
        if trajectory_capture_enabled():
            import asyncio
            await asyncio.to_thread(maybe_capture, task_agent, outcome, user_id=user_id)
    except Exception:
        logger.debug("trajectory capture failed", exc_info=True)
    return outcome


async def run_task_as_session(
    task_agent: Any,
    *,
    user_id: str,
    request: dict,
    autonomous: bool = False,
) -> tuple[Optional[str], Optional[str]]:
    """Legacy tuple shape over :func:`run_task_to_outcome`.

    Returns a ``(session_id, final)`` tuple with three shapes:

    - ``(None, None)``   — no session was created (create_session returned no id)
    - ``(session_id, None)``  — session created but run_session returned a known
      refusal/empty string; callers should treat this as a soft failure
    - ``(session_id, final)`` — session ran and produced a genuine result string

    ``final`` is the envelope's honest ``result_text()`` (done() ledger text →
    extracted reply → non-generic status), degrading to the raw status string
    only when nothing else exists — never worse than the pre-§2 behavior.
    """
    outcome = await run_task_to_outcome(
        task_agent, user_id=user_id, request=request, autonomous=autonomous)
    if outcome.session_id is None:
        return (None, None)
    if outcome.refusal:
        return (outcome.session_id, None)
    return (outcome.session_id, outcome.result_text() or outcome.status)
