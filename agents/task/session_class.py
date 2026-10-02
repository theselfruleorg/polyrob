"""Is this session AUTONOMOUS (a cron / goal / planner run) or interactive? — 057.

Principle 3 of 057: *autonomous ≠ interactive*. A scheduled or board-dispatched
run should get its own budget class — its own tool rig, context cap, output cap
and timeout — instead of inheriting the chat defaults. Everything in WS-A/WS-B
that needs to know "which kind of session am I in" asks HERE, so there is one
answer and not five heuristics (``skip_memory``, ``cron=True``,
``SessionProfile``, ``creator``, "does it have a goal_id").

The marker already exists and is already authoritative:
``agents.task.goals.autonomy_marker`` is stamped by
``agents/task/runtime/run_as_session.py`` **before** ``create_session``, so it is
visible during construction — which is exactly when the tool catalog is pinned
and the token limits are computed. This module is a fail-open reader over it,
never a second registry.

⚠️ Fail-OPEN on every probe error: an unreadable marker must degrade to
"interactive", i.e. to today's behaviour. A budget cap wrongly applied to an
owner's chat turn is a visible regression; a budget cap missed on one cron run
costs a few cents.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def is_autonomous_session(session_id: Optional[str]) -> bool:
    """True for a cron / goal / planner-spawned run. Fail-open to False."""
    if not session_id:
        return False
    try:
        from agents.task.goals.autonomy_marker import is_autonomous
        return bool(is_autonomous(str(session_id)))
    except Exception:  # pragma: no cover - defensive
        logger.debug("autonomy marker unreadable; treating session as interactive",
                     exc_info=True)
        return False


def tool_disclosure_enabled(session_id: Optional[str] = None) -> bool:
    """Whether this session pins the ``<tool-catalog>`` block + ``load_tool``.

    ``TOOL_PROGRESSIVE_DISCLOSURE`` (the existing flag; ON under POLYROB_LOCAL)
    OR — 057 WS-A — ``AUTONOMOUS_TOOL_DISCLOSURE`` for an autonomous session on
    a server that never sets POLYROB_LOCAL. That second key is what makes a
    NARROW rig safe: a job that finds it needs one more tool can load it,
    instead of failing and filing an owner ask.

    The hard lines are unchanged and live in ``tools/tool_disclosure.py``: money
    tools are NEVER loadable, delegate-blocked ids stay blocked for leaves, and
    loading registers schemas only — it grants no execution right.
    """
    from core.config_policy import tool_progressive_disclosure
    if tool_progressive_disclosure():
        return True
    from core.config_policy.capability_toggles import autonomous_tool_disclosure
    return autonomous_tool_disclosure() and is_autonomous_session(session_id)


def apply_session_output_budget(llm, session_id: Optional[str]) -> int:
    """Stamp ``AUTONOMOUS_MAX_OUTPUT_TOKENS`` on *llm* for an autonomous session.

    The agents-tier half of 057 WS-B's output budget: ``modules/llm`` owns the
    stamping and the ceiling arithmetic, this owns the session-class decision
    (``modules`` may not import ``agents``). Returns the cap in force, 0 = none.
    """
    from modules.llm.output_budget import apply_output_budget, autonomous_max_output_tokens
    cap = autonomous_max_output_tokens()
    if cap <= 0 or not is_autonomous_session(session_id):
        return 0
    return apply_output_budget(llm, cap)


def apply_session_cache_ttl(llm, session_id: Optional[str]) -> Optional[str]:
    """Stamp the Anthropic prompt-cache TTL for this session's class — F4.

    ``ANTHROPIC_CACHE_TTL=auto`` (the default) buys the **1h** cache window for
    an interactive turn and keeps the API-default **5m** one for an autonomous
    run. An owner's chat is bursty: the next turn lands minutes later, by which
    time a 5-minute cache has expired and the whole tools+system prefix is
    re-paid at full price. A cron/goal run is one burst that never returns, so
    the longer window would only pay the 2x cache-WRITE premium for nothing.

    The agents-tier half of the seam: ``modules/llm/cache_hints.py`` owns the
    stamp and the reader (``modules`` may not import ``agents``), this owns the
    session-class decision. Returns the ttl in force, or None.
    """
    from modules.llm.cache_hints import apply_cache_ttl, resolve_cache_ttl
    ttl = resolve_cache_ttl(is_autonomous_session(session_id))
    return apply_cache_ttl(llm, ttl)


def correspondent_facing(orchestrator) -> bool:
    """True when a third party's text drives this session: it was CREATED for a
    correspondent (``_correspondent_session``, stamped at creation and restored
    from the session's ``creator`` on recreate — H16) or it is currently
    correspondent-TAINTED (``_correspondent_tainted``). Owner-only state (the
    owner thread, owner asks) never enters such a session.

    Fail-CLOSED on a probe error: this guards disclosure, not a budget.
    """
    if orchestrator is None:
        return False
    try:
        return bool(getattr(orchestrator, "_correspondent_session", False)
                    or getattr(orchestrator, "_correspondent_tainted", False))
    except Exception:  # pragma: no cover - defensive
        return True
