"""045 lane 2 — the unauthorized-action ledger.

``tool_denied`` already existed and was already rendered by the activity feed and
the status snapshot. What was missing is that the gates which refuse SILENTLY —
the correspondent capability gate, an approval denial or timeout, a forged-turn
refusal, a money-verb refusal — emitted nothing at all, so a refusal trend was
invisible. This helper gives all of them the same typed emit, riding the existing
kind so no consumer has to change.

``reason`` is a closed set on purpose: a free-typed reason string is how the
original event-kind drift happened (see core/event_kinds.py's docstring).
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)

REFUSAL_REASONS = frozenset({
    "correspondent_taint",   # agents/task/agent/core/correspondent_gate.py
    "approval_denied",       # tools/controller/approval.py
    "approval_timeout",      # tools/controller/approval.py (wait_for cancel)
    "forged_turn",           # core/security/forged_turns.py consumers
    "money_gate",            # core/wallet/tx_guard.py, core/config_policy/spend_lane.py
    "room_toolset",          # 044 Phase 0 core/surfaces/room_policy.py
    "pause",                 # core/autonomy_control.py refusal of an autonomous verb
})


def _log():
    from core.event_log import get_event_log
    return get_event_log()


def record_refusal(reason: str, *, tool: str = "", user_id: str = "",
                   session_id: str = "", detail: Any = "") -> None:
    """Record ONE refusal. Raises ValueError on an unknown reason (a programming
    error, caught in tests), fail-open on anything else."""
    if reason not in REFUSAL_REASONS:
        raise ValueError(f"unknown refusal reason: {reason!r} "
                         f"(add it to REFUSAL_REASONS)")
    try:
        from core.event_log import event_log_enabled
        from core.event_kinds import TOOL_DENIED
        from core.security_flags import security_event_log_enabled
        if not (security_event_log_enabled() and event_log_enabled()):
            return
        _log().record(TOOL_DENIED, user_id=str(user_id or ""),
                      session_id=str(session_id or ""), source="gate",
                      attrs={"reason": reason, "tool": str(tool or ""),
                             "detail": str(detail or "")[:200]})
    except Exception:
        logger.debug("refusals: record skipped", exc_info=True)
