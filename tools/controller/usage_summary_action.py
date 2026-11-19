"""The read-only ``usage_summary`` action — what the agent's AI use cost.

Extracted from ``action_registration.py`` (its size ceiling) in 120 W3.

⚠️ Registry-closure landmine: NO ``from __future__ import annotations`` here —
the registry introspects the closure's first-param annotation.

Two parts, two gates (120 W3, decision D2):

- **The breakdown** (by kind of work, job, model, upstream, purpose and day) is
  the OWNER tenant's: ``core.status_economics.spend_reply``, the same text
  ``/usage`` prints on every seat. It follows no flag — an agent that cannot see
  why its spend is blocked is the defect 120 fixes. Jobs read
  ``scheduled job <id>``: the seat's title resolver sits above ``tools``.
- **The invoice rollup** (Task 13, Phase 3 R3: the tenant-scoped rollup and a
  non-binding ``suggested_invoice`` draft) stays behind
  ``USAGE_INVOICE_BRIDGE_ENABLED``, for any authenticated tenant. It NEVER creates
  a payment request — the agent must still call the approval-gated
  ``x402_request`` itself.

A tenant that is neither the owner nor on an install with the bridge gets no
action.
"""
import logging
from typing import Optional

from pydantic import BaseModel, Field

from agents.task.agent.views import ActionResult

logger = logging.getLogger(__name__)


def _bridge_on() -> bool:
    try:
        from modules.credits.usage_rollup import usage_invoice_bridge_enabled
        return bool(usage_invoice_bridge_enabled())
    except Exception:
        logger.warning("usage_summary: the invoice bridge flag could not be read", exc_info=True)
        return False


def _is_owner(user_id) -> bool:
    from core.instance import is_owner_uid
    return bool(user_id) and is_owner_uid(str(user_id))


async def _rollup_lines(user_id: str, params) -> tuple:
    """The invoice rollup: ``(lines, metadata, error)``."""
    from modules.credits.usage_rollup import build_invoice_draft, unreadable_text, usage_rollup
    session_id, since = getattr(params, "session_id", None), getattr(params, "since", None)
    rollup = await usage_rollup(user_id, session_id=session_id, since=since)
    if unreadable_text(rollup):
        return [], {}, unreadable_text(rollup)
    draft = build_invoice_draft(rollup)
    scope = ""
    if session_id:
        scope += f", session {session_id}"
    if since:
        scope += f", since {since}"
    lines = [
        f"Usage summary (tenant {user_id}{scope}):",
        f"  api_cost_usd: ${rollup['api_cost_usd']:.4f}",
        f"  credits:      {rollup['credits']:.2f}",
        f"  calls:        {rollup['calls']}",
        "",
        f"Suggested invoice (NOT sent — call x402_request yourself): "
        f"${draft['amount_usd']:.2f} — {draft['purpose']}",
    ]
    if draft["over_cap"]:
        lines.append(f"  WARNING: {draft['note']}")
    return lines, {"rollup": rollup, "suggested_invoice": draft}, None


def register_usage_summary_action(controller) -> None:
    """Register ``usage_summary`` for the owner tenant (the breakdown) and, with
    ``USAGE_INVOICE_BRIDGE_ENABLED``, for any tenant (the invoice rollup)."""
    bridge = _bridge_on()
    if not bridge and not _is_owner(getattr(controller, "user_id", None)):
        return

    class UsageSummaryAction(BaseModel):
        days: int = Field(default=0, description="The window in days (1-90; 0 = 7).")

    if bridge:
        class UsageSummaryAction(UsageSummaryAction):  # noqa: F811 — the bridge's narrowing
            session_id: Optional[str] = Field(
                default=None, description="Narrow the invoice rollup to one session.")
            since: Optional[str] = Field(
                default=None, description="Narrow the invoice rollup to records since a "
                                          "timestamp.")

    _rollup = (" With the invoice bridge on: the tenant rollup (api cost / credits / "
               "calls, optionally narrowed to a session_id or a since timestamp) and a "
               "non-binding suggested invoice draft — never sends money; call "
               "x402_request yourself to actually create an invoice." if bridge else "")

    @controller.registry.action(
        "What your AI use cost over `days`: the total, the cache share and the split by "
        "kind of work, job or goal, model, upstream, purpose and day (the owner's /usage). "
        "Read-only." + _rollup,
        param_model=UsageSummaryAction,
    )
    async def usage_summary(params: UsageSummaryAction, execution_context=None) -> ActionResult:
        user_id = (getattr(execution_context, 'user_id', None)
                   or getattr(controller, 'user_id', None) or "")
        if not user_id:
            return ActionResult(
                error=("usage_summary refused: this action requires an authenticated "
                       "tenant — the execution context has no user_id (anonymous "
                       "financial state is refused)"),
                include_in_memory=True)
        owner, bridge_now = _is_owner(user_id), _bridge_on()
        if not owner and not bridge_now:
            return ActionResult(
                error=("usage_summary refused: the cost breakdown is the owner's, and the "
                       "invoice rollup is off on this install (USAGE_INVOICE_BRIDGE_ENABLED)"),
                include_in_memory=True)
        lines, metadata = [], {}
        if owner:
            import asyncio
            from core.runtime_paths import container_data_home
            from core.status_economics import SPEND_DEFAULT_DAYS, spend_reply
            days = int(params.days or 0) or SPEND_DEFAULT_DAYS
            data_dir = container_data_home(getattr(controller, "container", None))
            lines.append(await asyncio.to_thread(spend_reply, str(user_id), data_dir, days))
        if bridge_now:
            rollup_lines, metadata, error = await _rollup_lines(str(user_id), params)
            if error:
                if not lines:
                    return ActionResult(error=error, include_in_memory=True)
                lines += ["", f"invoice rollup: {error}"]
            else:
                lines += ([""] if lines else []) + rollup_lines
        return ActionResult(extracted_content="\n".join(lines), metadata=metadata or None,
                            include_in_memory=True)
