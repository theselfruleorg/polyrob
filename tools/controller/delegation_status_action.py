"""The `delegation_status` action — the agent sees and stops its own background helpers
(proposal 120 W2).

``delegate_task(background=true)`` detaches a helper that re-enters the session when it
ends. Before this action the agent could start one and never see it again: the list
and the per-id cancel lived only behind the owner's ``/subagents`` and the owner pause.

- **list** — this session's background delegations from
  ``orchestrator.async_delegation`` (id, state, age, goal). The registry is per
  session, so a helper another session started is not listed; the answer says so.
- **cancel** — ``AsyncDelegationRegistry.cancel``, the ONE cancel the owner pause
  (``cancel_all``) also runs: a running helper is cancelled, a finished helper still
  parking its result is not delivered, and its durable row is stamped so a restart
  does not redeliver it. A narrowing write: a leaf / sub-agent never cancels.

⚠️ Registry-closure landmine: NO `from __future__ import annotations` in this module —
the registry introspects the closure's first-param annotation to route the validated
param model.

⚠️ Registered from `tools/controller/service.py`, NOT `action_registration.py`: that
file is at its size-ratchet ceiling.
"""
import logging
import time
from typing import Optional

from pydantic import BaseModel, Field

from agents.task.agent.views import ActionResult

logger = logging.getLogger(__name__)

_GOAL_CHARS = 120
_NOT_AVAILABLE = "delegation_status: background helpers are not available in this session."


def _registry(controller):
    orch = getattr(controller, "orchestrator", None)
    return getattr(orch, "async_delegation", None) if orch is not None else None


def _age(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def list_text(reg) -> str:
    """The agent's view of this session's background helpers, newest first."""
    rows = sorted(reg.list(), key=lambda r: r.dispatched_at or 0.0, reverse=True)
    head = (f"Background helpers of THIS session ({len(rows)}, "
            f"{reg.active_count()} running). Helpers of other sessions are not listed.")
    if not rows:
        return head + "\nnone"
    now = time.time()
    lines = [head]
    for r in rows:
        goal = " ".join(str(r.goal or "").split())
        goal = goal if len(goal) <= _GOAL_CHARS else goal[:_GOAL_CHARS - 1] + "…"
        end = r.completed_at or now
        lines.append(f"- {r.delegation_id} [{r.status}] {_age(end - (r.dispatched_at or end))}"
                     f" ({r.profile}): {goal}")
    return "\n".join(lines)


def register_delegation_status_action(controller) -> None:
    """Register `delegation_status` (list, or cancel one id)."""

    class DelegationStatusAction(BaseModel):
        cancel: Optional[str] = Field(
            default=None,
            description=("A delegation id (deleg_NNNN) of THIS session to stop. A running "
                         "helper is cancelled; a finished one that has not reported yet "
                         "never reports. Omit to only list."))
        reason: str = Field(default="", max_length=200,
                            description="Why you stop it (kept in the log).")

    @controller.registry.action(
        "Your background helpers (delegate_task background=true) in this session: list "
        "them with their state, or stop one by id with cancel.",
        param_model=DelegationStatusAction,
    )
    async def delegation_status(params: DelegationStatusAction,
                                execution_context=None) -> ActionResult:
        reg = _registry(controller)
        if reg is None:
            return ActionResult(error=_NOT_AVAILABLE, include_in_memory=True)
        if not params.cancel:
            return ActionResult(extracted_content=list_text(reg), include_in_memory=True)
        from core.security.owner_turn import is_leaf
        if is_leaf(execution_context, controller):
            return ActionResult(error="Refused: a leaf/sub-agent cannot stop a background "
                                      "helper; report to your parent.", include_in_memory=True)
        did = params.cancel.strip()
        known = {r.delegation_id: r for r in reg.list()}
        if did not in known:
            return ActionResult(error=f"No background helper `{did}` in this session (the "
                                      f"list is this session's only).", include_in_memory=True)
        why = (params.reason or "").strip() or "stopped by the agent"
        outcome = reg.cancel(did, f"delegation_status: {why}")
        if outcome == "cancelled":
            text = f"Cancelled background helper {did}. It does not report back."
        elif outcome == "stopped":
            text = (f"Helper {did} had finished; its result is not delivered and is not "
                    f"redelivered after a restart.")
        else:
            text = f"Helper {did} is {known[did].status}; there was nothing to stop."
        return ActionResult(extracted_content=text, include_in_memory=True)


__all__ = ["list_text", "register_delegation_status_action"]
