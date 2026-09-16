"""043 A5 — the ONE owner-create helper for goals and cron jobs.

The owner seat (the web console, ``polyrob goals create``, Telegram ``/trade``)
creates work with the OWNER's grant: the tools it names are written into the
payload VERBATIM, never run through the agent's self-grant allowlist
(``allowed_self_goal_tools``), because the owner acting from an authenticated
seat IS the operator grant. This is the same policy ``/trade`` and
``polyrob goals create`` already apply by hand; centralising it here is what
stops the direct ``board.create`` callers from drifting on it.

⚠️ **Layering.** ``core/`` may import nothing from ``agents.*`` / ``cron.*``, so
the concrete ``GoalBoard`` (``agents.task.goals``) and ``CronService``
(``cron.service``) are INJECTED — the caller passes the board / service and this
module holds only the pure policy: owner grant, UNFILTERED tools, tenant +
input validation. That mirrors the inbox collectors pattern
(``core/surfaces/inbox.py::build_inbox`` takes its collectors injected) and
keeps ``tests/test_layering_ratchet.py`` green.

⚠️ **Reach, never policy.** This helper WRITES a row; it never runs a money verb.
A goal or cron job it creates is dispatched later under exactly the same
per-transaction caps, forged/leaf/tainted refusals, the 031 pause and the owner
queue as any other — writing ``defi_trade`` into ``payload.tools`` is a grant,
not an execution.

The store errors propagate UNCAUGHT so the caller can map them to a surface
answer: ``GoalBoard.create`` raises ``DuplicateGoalError`` (a near-duplicate) or
``ValueError`` (a bad tenant / dependency); ``CronService.schedule`` raises
``ScheduleError`` (an unparseable spec or one with no future run).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from core.identity import is_anonymous

logger = logging.getLogger(__name__)

#: The default status a fresh owner-created goal lands in — dispatchable at once.
#: A string, not the imported ``STATUS_READY`` constant, because importing
#: ``agents.task.goals.board`` would re-open the core->agents edge this module
#: exists to avoid. ``GoalBoard.create`` validates the value.
_STATUS_READY = "ready"


def _clean_tools(tools: Optional[Sequence[Any]]) -> List[str]:
    """The named tool ids, stripped of blanks — and NOT filtered.

    This is the whole point of the owner seat: the ids are written verbatim.
    The one thing done here is dropping empties / whitespace, so a stray comma
    on a CLI ``--tools`` string or a blank field in a console form does not
    persist an empty tool id.
    """
    if not tools:
        return []
    out: List[str] = []
    for raw in tools:
        name = str(raw).strip()
        if name:
            out.append(name)
    return out


def create_goal(board: Any, *, user_id: str, title: str, body: str = "",
                priority: int = 5, tools: Optional[Sequence[Any]] = None,
                acceptance: Optional[str] = None, status: str = _STATUS_READY,
                parent_id: Optional[str] = None,
                extra_payload: Optional[Dict[str, Any]] = None,
                force: bool = False,
                depends_on: Optional[Sequence[str]] = None) -> Any:
    """Create one goal on *board* with the OWNER's grant.

    *board* is an injected ``GoalBoard`` (or a stand-in with the same
    ``create`` signature). ``tools`` is written to ``payload.tools`` VERBATIM —
    the owner is the operator grant, so there is no self-grant allowlist here,
    unlike the agent-callable ``goal_create`` tool.

    Raises ``ValueError`` for an empty title or an anonymous tenant (so a bad
    input is refused BEFORE a row is written), and re-raises whatever
    ``board.create`` raises (``DuplicateGoalError`` / ``ValueError``).
    """
    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("a goal needs a title")
    if is_anonymous(user_id):
        raise ValueError("goal create requires a real (non-anonymous) tenant")

    payload: Dict[str, Any] = dict(extra_payload or {})
    named_tools = _clean_tools(tools)
    if named_tools:
        payload["tools"] = named_tools
    if acceptance:
        payload["acceptance"] = str(acceptance)

    return board.create(
        user_id=user_id, title=clean_title, body=body or "",
        priority=priority, status=status, parent_id=parent_id,
        payload=payload or None, force=force,
        depends_on=list(depends_on) if depends_on else None,
    )


def create_cron(service: Any, *, task: str, schedule_spec: str, user_id: str,
                tools: Optional[Sequence[Any]] = None,
                deliver: Optional[str] = None,
                deliver_target: Optional[str] = None,
                wake_agent: bool = True,
                max_duration_seconds: int = 180,
                via: str = "",
                extra_payload: Optional[Dict[str, Any]] = None) -> Any:
    """Schedule one durable cron job on *service* with the OWNER's grant.

    *service* is an injected ``CronService`` (or a stand-in with the same
    ``schedule`` signature). ``tools`` is written to ``payload.tools`` VERBATIM,
    like :func:`create_goal`. ``via`` is passed through to the service so the
    A29 audit event names the surface (the console passes ``via="webview"``).

    Raises ``ValueError`` for an empty task or schedule, and re-raises whatever
    ``service.schedule`` raises (``ScheduleError``).
    """
    clean_task = (task or "").strip()
    if not clean_task:
        raise ValueError("a scheduled task needs something to do")
    clean_spec = (schedule_spec or "").strip()
    if not clean_spec:
        raise ValueError("a scheduled task needs a schedule")
    if is_anonymous(user_id):
        raise ValueError("cron create requires a real (non-anonymous) tenant")

    payload: Dict[str, Any] = dict(extra_payload or {})
    named_tools = _clean_tools(tools)
    if named_tools:
        payload["tools"] = named_tools
    if deliver:
        payload["deliver"] = str(deliver)
    if deliver_target:
        payload["deliver_target"] = str(deliver_target)
    if not wake_agent:
        payload["wake_agent"] = False

    return service.schedule(
        task=clean_task, schedule_spec=clean_spec, user_id=user_id,
        payload=payload or None, max_duration_seconds=max_duration_seconds,
        via=via,
    )
