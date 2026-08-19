"""Credential health (proposal 024, L1).

Three states. For metered keys, exhaustion is an anomaly; for subscriptions it
is the NORMAL daily condition — the fallback hierarchy must route around a
spent plan instead of hammering it, and the credit sentinel should say WHICH
plan is spent and until when.

- ``ok`` — usable.
- ``rate_limited`` — throttled; usable again after ``retry_after`` (epoch sec).
- ``exhausted`` — quota gone for the period; needs ``retry_after`` (the window
  roll, when known) or an owner action.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

HEALTH_OK = "ok"
HEALTH_RATE_LIMITED = "rate_limited"
HEALTH_EXHAUSTED = "exhausted"

_VALID_STATES = (HEALTH_OK, HEALTH_RATE_LIMITED, HEALTH_EXHAUSTED)


def effective_state(health: Optional[Dict[str, Any]], now: Optional[float] = None) -> str:
    """The state a consumer should act on: a lapsed ``retry_after`` heals a
    ``rate_limited``/``exhausted`` stamp back to ``ok``."""
    if not isinstance(health, dict):
        return HEALTH_OK
    state = health.get("state") or HEALTH_OK
    if state not in _VALID_STATES:
        return HEALTH_OK
    if state in (HEALTH_RATE_LIMITED, HEALTH_EXHAUSTED):
        retry_after = health.get("retry_after")
        if retry_after is not None:
            now = time.time() if now is None else now
            if now >= float(retry_after):
                return HEALTH_OK
    return state


def is_usable(health: Optional[Dict[str, Any]], now: Optional[float] = None) -> bool:
    return effective_state(health, now) == HEALTH_OK


# ⚠ NOT YET WIRED (024 L1.5, 2026-08-11). `stamp_from_error_verdict` still has
# zero callers, so stored health never leaves "ok" and the READ path below —
# now consumed by `profiles.credential_status` / `usable_providers_with_
# credentials` / `polyrob doctor` — is correct but always reports healthy.
#
# It was NOT wired here because the obvious call site is agent-tier
# (`agents/task/agent/core/error_recovery.py`, right after `classify_error`),
# and importing this package from `agents/` is a PERMANENT non-goal pinned by
# tests/unit/core/llm_auth/test_agent_unreachable.py. Reaching it through a
# core-tier hop would pass that AST scan while defeating its purpose, so the
# call site is an owner decision, not a workaround.
#
# A second hazard to settle first: a `rate_limited` stamp written with NO
# `retry_after` never heals (`effective_state` only clears on a lapsed
# retry_after), and the gating oracle excludes unhealthy providers — so a
# transient 429 stamped from a retry loop would park a working provider
# permanently. Whatever call site is chosen must supply `retry_after`, or
# stamp only terminal verdicts.
def stamp_from_error_verdict(
    provider: str,
    verdict: str,
    *,
    retry_after: Optional[float] = None,
    store=None,
) -> bool:
    """Map an ``core.error_classifier`` verdict onto a credential health stamp.

    The one hook the LLM error path calls (024 §4.1): CREDIT_DEATH → exhausted,
    RATE_LIMIT → rate_limited, anything else → no stamp. Fail-open: a store
    problem never worsens the original LLM error.
    """
    state = {
        "CREDIT_DEATH": HEALTH_EXHAUSTED,
        "RATE_LIMIT": HEALTH_RATE_LIMITED,
    }.get(verdict)
    if state is None:
        return False
    try:
        if store is None:
            from core.llm_auth.store import get_auth_store
            store = get_auth_store()
        return store.stamp_health(provider, state, retry_after=retry_after)
    except Exception:
        return False
