"""Per-session dollar-budget gate for the agent run loop (T1.1).

Pure policy, mirrors conversational_exit.py: the run loop calls
``check_run_budget(agent)`` at the top of each step iteration; a non-None
return means "halt honestly before the next paid step". Spend is read via the
tenant-scoped, fail-open ``usage_rollup`` (modules/credits/usage_rollup.py) and
compared as REAL provider cost (``api_cost_usd``), not the markup user price.
Session-cumulative by design: aux-model and sub-agent calls bill under the
parent session_id, so one rollup covers the whole tree. Sub-agents are ungated
(the parent's budget covers them). Fail-open everywhere — a broken cost probe
must never kill a healthy run; anonymous sessions record no usage rows, so the
gate is inert there.
"""
from __future__ import annotations

import logging
from typing import Optional

from core.config_policy.policy import run_budget_usd

try:
    from modules.credits.usage_rollup import usage_rollup
except ImportError:  # core-only install: modules.credits absent — the gate stays inert
    usage_rollup = None  # type: ignore[assignment]

RUN_BUDGET_MARKER = "run_budget_exhausted"

logger = logging.getLogger(__name__)

# Final-review fix (T1.1): RUN_BUDGET_USD>0 but a precondition fails (no
# tracker / anonymous session / core-only install) left the gate silently
# inert — the operator believes spend is capped when it isn't. Warn ONCE
# (module-level latch) the first time that happens while the flag is on.
# Sub-agent skip does NOT warn (the parent session's gate covers it); flag
# off does NOT warn (the operator never asked for capping).
_warned_inert = False


def _warn_inert_once(budget: float) -> None:
    global _warned_inert
    if _warned_inert:
        return
    _warned_inert = True
    logger.warning(
        "RUN_BUDGET_USD=%.2f is set but the budget gate cannot operate "
        "(no usage tracker / anonymous session / core-only install) — "
        "spend is NOT capped",
        budget,
    )


async def check_run_budget(agent) -> Optional[str]:
    """Return a halt message when session provider spend >= RUN_BUDGET_USD, else None."""
    try:
        budget = run_budget_usd()
        if budget <= 0:
            return None
        if getattr(agent, "_is_sub_agent", False):
            return None
        tracker = getattr(agent, "usage_tracker", None)
        user_id = getattr(agent, "user_id", None)
        if tracker is None or not user_id:
            _warn_inert_once(budget)
            return None
        if usage_rollup is None:
            _warn_inert_once(budget)
            return None
        rollup = await usage_rollup(
            user_id, session_id=agent.session_id, db=getattr(tracker, "db", None)
        )
        spent = float(rollup.get("api_cost_usd") or 0.0)
        if spent >= budget:
            return (
                f"{RUN_BUDGET_MARKER}: session provider spend ${spent:.4f} reached "
                f"RUN_BUDGET_USD ${budget:.2f}; halting before the next step "
                f"(raise the budget or start a new session to continue)"
            )
        return None
    except Exception:
        return None  # fail-open: never let the cost probe crash the loop
