"""Consecutive timeouts are a ROUTING fact — 057 WS-B.

A timeout never triggered provider fallback (only rate-limit / auth / connection
errors do, ``llm_runner.get_next_action``), so a stalled upstream was retried on
the SAME upstream: prod's metered seat stalled for 290-410 s often enough to cut
five money-rail runs in one afternoon. And the retry path is not free — the worst
step is ``timeout + min(180, 0.75*timeout)`` ≈ 570 s against a 600 s cron cap, so
retrying forever is how a cap gets hit.

The ladder, per SESSION (never process-wide — that would outlive the run and
re-route every other session):

1. first timeout  — reduced-context retry, as today.
2. second timeout — ask OpenRouter to order upstreams by ``latency`` for the rest
   of this run (the ``OPENROUTER_PROVIDER_SORT`` knob, kept on the agent and scoped
   to each of its steps), then retry.
3. third timeout  — STOP. End the step honestly with the existing recovery brain
   and no further retry. A third consecutive timeout is a statement about the
   route, not about this prompt.

Any successful call resets the count: the ladder measures a STREAK, not a total.

Gated ``LLM_TIMEOUT_REROUTE`` (default OFF => byte-identical: the counter is
still kept, nothing acts on it).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_ATTR = "_consecutive_llm_timeouts"
#: the agent's routing sort for the rest of its run; each step scopes it to its calls.
SORT_ATTR = "_session_provider_sort"

#: timeouts in a row before the session asks for latency-ordered routing.
REROUTE_AFTER = 2
#: timeouts in a row after which the step ends with no further retry.
STOP_AFTER = 3


def reroute_enabled() -> bool:
    """``LLM_TIMEOUT_REROUTE`` — default OFF."""
    from core.env import bool_env
    return bool_env("LLM_TIMEOUT_REROUTE", False)


def note_success(agent: Any) -> None:
    """A completed call resets the streak."""
    try:
        setattr(agent, _ATTR, 0)
    except Exception:  # pragma: no cover - defensive
        pass


def note_timeout(agent: Any) -> int:
    """Record one timeout; return the streak length."""
    try:
        count = int(getattr(agent, _ATTR, 0) or 0) + 1
        setattr(agent, _ATTR, count)
        return count
    except Exception:  # pragma: no cover - defensive
        return 1


def maybe_reroute(agent: Any, count: int) -> bool:
    """At ``REROUTE_AFTER`` timeouts, pin this session's routing to ``latency``.

    Returns True when the sort was applied (idempotent across later timeouts).
    """
    if count < REROUTE_AFTER or not reroute_enabled():
        return False
    try:
        if getattr(agent, SORT_ATTR, None) == "latency":
            return True
        setattr(agent, SORT_ATTR, "latency")
    except Exception:  # pragma: no cover - defensive
        logger.debug("timeout reroute skipped", exc_info=True)
        return False
    logger.warning(
        "%d consecutive LLM timeouts — routing this session by latency "
        "for the rest of the run (LLM_TIMEOUT_REROUTE)", count)
    # 095 W2 (HI-15): one model-visible line, so the model knows why.
    try:
        from modules.llm.messages import MessageOrigin, make_control_message
        agent.message_manager.push_ephemeral_message(make_control_message(
            f"[routing] {count} model calls in a row timed out; this run now "
            "routes by latency. Keep each step small.", MessageOrigin.INTERVENTION))
    except Exception:
        logger.debug("reroute note skipped", exc_info=True)
    return True


def should_stop_retrying(count: int) -> bool:
    """True once the streak reaches ``STOP_AFTER`` and the flag is on."""
    return count >= STOP_AFTER and reroute_enabled()
