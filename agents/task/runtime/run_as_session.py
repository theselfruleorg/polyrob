"""Shared session-execution helpers used by cron runner and goal dispatcher.

Canonical location for:
- ``_RUN_REFUSALS`` / ``is_refusal`` — moved verbatim from ``cron/runner.py``
- ``run_task_as_session`` — the common create_session → run_session flow
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def create_session_accepts(create_fn, name: str) -> bool:
    """Best-effort: would *create_fn* (a ``create_session`` callable) accept
    keyword *name* without raising — either it names the param explicitly or
    accepts ``**kwargs``? False (never raises) on introspection failure.

    Shared by ``run_task_to_outcome`` (session_id/creator pre-checks) and
    cron/runner.py's legacy direct ``create_session`` call (043 A17
    ``creator``) — a narrow test fake with neither the named param nor
    ``**kwargs`` must not start raising ``TypeError``.
    """
    try:
        sig = inspect.signature(create_fn)
    except Exception:
        return False
    return name in sig.parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


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
    goal_id: str | None = None,
    creator: str | None = None,
    cron_job_id: str | None = None,
):
    """The primary run entry (§2): create_session → run_session → RunOutcome.

    *task_agent* must implement:
    - ``create_session(user_id, request)`` → dict with ``"id"`` key, or falsy
    - ``run_session(user_id, session_id)`` → str result or refusal string

    *autonomous* marks the created session (goal/cron/planner-spawned) in the
    in-process autonomy registry (``agents.task.goals.autonomy_marker``) so
    the goal tool can refuse objective mutations from that session later.

    *goal_id* records WHICH goal the session is running, when there is one (a
    cron/planner run has none). The approval queue reads it to stamp
    ``blocks_goal_ids`` on an owner-queue ask, so approving the ask re-arms the
    goal that raised it — otherwise the owner presses /approve and nothing
    happens (039).

    *cron_job_id* (2026-09-18) is the cron twin of *goal_id*: the approval queue
    stamps it on an owner-queue ask so approving the ask re-arms the JOB to run
    on the next tick — the run that redeems the grant is a genuine cron turn,
    where a self-wake into the finished session could never spend.

    *creator* (043 A17): the session-creator display label. This helper is
    shared by BOTH the goal dispatcher and cron runner, which resolve to
    different labels ("goal" vs "cron") — so the CALLER must pass it
    explicitly; ``None`` here falls back to ``resolve_creator``'s own default
    ("api") rather than mislabeling an autonomous run.

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
    # fakes/custom agents keep the post-create marking below. The same
    # ``create_session_accepts`` check gates `creator` (043 A17).
    # 044 T20: a ROOM-bound autonomous run (a `payload.group` cron job or goal)
    # carries its binding IN the request dict, because every producer on this
    # path builds a request and has no other channel to create_session. Popped
    # here and forwarded as kwargs — `create_session` passes them straight to
    # `bind_chat_surface`, which is what stamps `_public_session` and makes the
    # run BE the room session instead of a private session posting into a room.
    # Absent (every legacy caller) => not passed at all, so a narrow test fake
    # with neither param nor **kwargs is untouched.
    #
    # `session_id` rides the same channel (T20 fix round 1, Important 5): a room
    # service caller PRE-generates it so its `finally` can still find the
    # orchestrator — and close the room's books — when the run is cancelled by a
    # wall-clock timeout and never returns a RunOutcome at all.
    room_kwargs = {}
    if isinstance(request, dict):
        for _name in ("session_source", "chat_session_key", "bind_write_row",
                      "session_id"):
            _val = request.pop(_name, None)
            if _val is not None:
                room_kwargs[_name] = _val

    pre_sid = room_kwargs.pop("session_id", None)
    if pre_sid is not None and not create_session_accepts(
            task_agent.create_session, "session_id"):
        pre_sid = None  # narrow legacy fake: drop it rather than raise
    if autonomous and pre_sid is None:
        try:
            import uuid
            if create_session_accepts(task_agent.create_session, "session_id"):
                pre_sid = str(uuid.uuid4())
        except Exception:
            pre_sid = None
    if autonomous and pre_sid:
        from agents.task.goals.autonomy_marker import mark_autonomous
        mark_autonomous(pre_sid, goal_id, cron_job_id=cron_job_id)

    kwargs = {"session_id": pre_sid} if pre_sid else {}
    if creator is not None and create_session_accepts(task_agent.create_session, "creator"):
        kwargs["creator"] = creator
    kwargs.update(room_kwargs)
    session_info = await task_agent.create_session(
        user_id=user_id, request=request, **kwargs)
    session_id = (session_info or {}).get("id")
    if not session_id:
        return RunOutcome(session_id=None)
    if autonomous:
        from agents.task.goals.autonomy_marker import mark_autonomous
        mark_autonomous(session_id, goal_id, cron_job_id=cron_job_id)
    status = await task_agent.run_session(user_id, session_id)
    outcome = await build_run_outcome(task_agent, session_id, status)
    if autonomous:
        # Goal/cron runs are one-shot: release the session's persistent shell
        # sandbox container now. Session end runs only a PARTIAL cleanup (the
        # orchestrator stays resident for continuous chat), which skips the
        # container teardown, and reap_orphans is cold-start-only by design —
        # so autonomous shell users leaked one container per run until the next
        # service restart (live 2026-08-16: 7 containers, 3–21h old). A later
        # self-wake re-entry that runs shell simply gets a fresh container.
        # Routed through the orchestrator (SessionCleanupMixin.
        # release_shell_sandbox), which owns the one allowlisted
        # agents→tools.shell layering edge.
        try:
            orch = None
            get_orch = getattr(task_agent, "get_orchestrator", None)
            if callable(get_orch):
                orch = get_orch(session_id)
            release = getattr(orch, "release_shell_sandbox", None)
            if callable(release):
                await release()
        except Exception:
            logger.debug("shell sandbox release failed (non-fatal)", exc_info=True)
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
    creator: str | None = None,
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

    *creator* (043 A17): forwarded verbatim to :func:`run_task_to_outcome`.
    """
    outcome = await run_task_to_outcome(
        task_agent, user_id=user_id, request=request, autonomous=autonomous,
        creator=creator)
    if outcome.session_id is None:
        return (None, None)
    if outcome.refusal:
        return (outcome.session_id, None)
    return (outcome.session_id, outcome.result_text() or outcome.status)
