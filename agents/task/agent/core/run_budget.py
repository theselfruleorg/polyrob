"""Per-session dollar-budget gate for the agent run loop (T1.1).

Pure policy, mirrors conversational_exit.py: the run loop calls
``check_run_budget(agent)`` at the top of each step iteration; a non-None
return means "halt honestly before the next paid step". Spend is read via the
tenant-scoped, fail-open ``usage_rollup`` (modules/credits/usage_rollup.py) and
compared as REAL provider cost (``api_cost_usd``), not the markup user price.
Session-cumulative by design: aux-model and sub-agent calls bill under the
parent session_id, so one rollup covers the whole tree. A sub-agent skips the
RUN_BUDGET_USD check (the parent's session gate covers it). Fail-open everywhere — a broken cost probe
must never kill a healthy run; anonymous sessions record no usage rows, so the
gate is inert there.

083 WS-3 adds a per-RUN cap for autonomous runs (AUTONOMOUS_RUN_BUDGET_USD): the
run loop snapshots the session spend at run start (``snapshot_run_baseline``) and
the gate compares the DELTA, so a resumed session never inherits old spend. A
blocking child carries the parent run's (baseline, cap) and halts on the parent's
remaining budget; a background child gets the parent's remaining amount at spawn
as its own cap (``attach_parent_run_budget``). Both caps share RUN_BUDGET_MARKER.
"""
from __future__ import annotations

import logging
from typing import Optional

from core.config_policy.policy import autonomous_run_budget_usd, run_budget_usd

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


def _is_autonomous(agent) -> bool:
    """The budget reading of the autonomous predicate (fail-open to interactive)."""
    from agents.task.session_class import budget_class_autonomous
    return bool(budget_class_autonomous(getattr(agent, "session_id", None)))


async def _session_spend(agent, *, warn_budget: float = 0.0) -> Optional[float]:
    """Session provider spend (api_cost_usd), or None when it cannot be measured.

    ``warn_budget`` > 0 warns once when a precondition fails (RUN_BUDGET_USD only).
    Raises on a probe error — callers fail open.
    """
    tracker = getattr(agent, "usage_tracker", None)
    user_id = getattr(agent, "user_id", None)
    if tracker is None or not user_id or usage_rollup is None:
        if warn_budget > 0:
            _warn_inert_once(warn_budget)
        return None
    rollup = await usage_rollup(
        user_id, session_id=agent.session_id, db=getattr(tracker, "db", None)
    )
    return float(rollup.get("api_cost_usd") or 0.0)


async def snapshot_run_baseline(agent) -> None:
    """083 WS-3: record the session spend at the start of THIS run.

    Set only for an autonomous main agent while AUTONOMOUS_RUN_BUDGET_USD > 0;
    otherwise None (the per-run cap is then off for this run). Fail-open.
    """
    try:
        agent.state.run_spend_baseline = None
        if getattr(agent, "_is_sub_agent", False):
            return
        if autonomous_run_budget_usd() <= 0 or not _is_autonomous(agent):
            return
        agent.state.run_spend_baseline = await _session_spend(agent)
    except Exception:
        logger.debug("run spend baseline unreadable; per-run cap off", exc_info=True)


def _find_parent(orchestrator, parent_agent_id: str):
    agents = getattr(orchestrator, "agents", None) or {}
    session_id = getattr(orchestrator, "session_id", "")
    for key in (parent_agent_id, f"{parent_agent_id}_{session_id}"):
        if key in agents:
            return agents[key]
    return None


async def attach_parent_run_budget(child, orchestrator, parent_agent_id: str,
                                   *, background: bool = False) -> None:
    """083 WS-3: give a sub-agent the parent run's budget. Fail-open.

    Blocking child: the parent's own (baseline, cap) — it halts when the PARENT run's
    spend reaches the cap. Background child: (spend now, parent's remaining at spawn)
    — its own cap. No-op unless the parent run has a baseline (autonomous, cap on).
    """
    try:
        cap = autonomous_run_budget_usd()
        if cap <= 0:
            return
        parent = _find_parent(orchestrator, parent_agent_id)
        baseline = getattr(getattr(parent, "state", None), "run_spend_baseline", None)
        if parent is None or baseline is None:
            return
        if not background:
            child._run_budget_limit = (float(baseline), cap, "blocking")
            return
        now = await _session_spend(parent)
        if now is None:
            return
        remaining = max(0.0, cap - (now - float(baseline)))
        child._run_budget_limit = (now, remaining, "background")
    except Exception:
        logger.debug("parent run budget not attached to child", exc_info=True)


async def _check_child_limit(agent) -> Optional[str]:
    limit = getattr(agent, "_run_budget_limit", None)
    if not limit:
        return None
    baseline, cap, kind = limit
    spent = await _session_spend(agent)
    if spent is None:
        return None
    delta = spent - baseline
    if delta < cap:
        return None
    if kind == "background":
        return (
            f"{RUN_BUDGET_MARKER}: provider spend since this background sub-agent "
            f"started ${delta:.4f} reached its cap ${cap:.2f} (the parent run's "
            f"remaining AUTONOMOUS_RUN_BUDGET_USD at spawn); halting before the next step"
        )
    return (
        f"{RUN_BUDGET_MARKER}: the parent run's provider spend ${delta:.4f} reached "
        f"AUTONOMOUS_RUN_BUDGET_USD ${cap:.2f}; this sub-agent halts before its next step"
    )


async def check_run_budget(agent) -> Optional[str]:
    """Return a halt message when a spend cap is reached, else None.

    Main agent: RUN_BUDGET_USD (session total) and, for an autonomous run with a
    baseline, AUTONOMOUS_RUN_BUDGET_USD (spend since this run started) — whichever
    trips first. Sub-agent: only the parent run's budget it carries (083 WS-3).
    """
    try:
        if getattr(agent, "_is_sub_agent", False):
            return await _check_child_limit(agent)
        budget = run_budget_usd()
        run_cap = autonomous_run_budget_usd()
        baseline = getattr(getattr(agent, "state", None), "run_spend_baseline", None)
        run_on = run_cap > 0 and baseline is not None
        if budget <= 0 and not run_on:
            return None
        spent = await _session_spend(agent, warn_budget=budget)
        if spent is None:
            return None
        if budget > 0 and spent >= budget:
            return (
                f"{RUN_BUDGET_MARKER}: session provider spend ${spent:.4f} reached "
                f"RUN_BUDGET_USD ${budget:.2f}; halting before the next step "
                f"(raise the budget or start a new session to continue)"
            )
        if run_on and spent - float(baseline) >= run_cap:
            return (
                f"{RUN_BUDGET_MARKER}: this run's provider spend "
                f"${spent - float(baseline):.4f} reached AUTONOMOUS_RUN_BUDGET_USD "
                f"${run_cap:.2f}; halting before the next step (raise "
                f"AUTONOMOUS_RUN_BUDGET_USD, or set it to 0 to turn the per-run cap off)"
            )
        return None
    except Exception:
        return None  # fail-open: never let the cost probe crash the loop
