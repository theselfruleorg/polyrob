"""WS-B correspondent auto-seed guardrail (Fusion must-fix #5).

When the agent INITIATES contact with a new external address, a correspondent binding
is seeded so their reply can later route back as DATA. Auto-seed is the soft underbelly
of the design — a prompt-injected agent could be tricked into emailing an attacker — so
seeding is heavily gated:

- OFF unless the access model is enabled;
- a per-tenant per-day cap bounds runaway mass-contact;
- owner-provenance seeds honour ``CORRESPONDENT_REQUIRE_APPROVAL`` (default ON ->
  PENDING until the owner ratifies);
- UNTRUSTED-provenance seeds are ALWAYS pending (the registry enforces this) — no
  self-granted trust from injected content.

Returns the resulting state: "disabled" | "refused" | "pending" | "active".
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def maybe_seed_correspondent(
    container: Any,
    *,
    surface: str,
    address: str,
    session_id: str,
    user_id: str,
    thread_id: Optional[str] = None,
    provenance: str = "owner",
    now: Optional[float] = None,
) -> str:
    from agents.task.surface_config import SurfaceConfig
    if not SurfaceConfig.correspondent_access_enabled():
        return "disabled"
    registry = container.get_service("correspondent_registry") if container else None
    if registry is None:
        return "disabled"
    try:
        cap = SurfaceConfig.correspondent_max_new_per_day()
        if registry.count_seeds_since(user_id=user_id, since_secs=86400, now=now) >= cap:
            logger.warning(
                "correspondent auto-seed refused for %s: per-day cap (%d) reached",
                user_id, cap)
            return "refused"
        require_approval = SurfaceConfig.correspondent_require_approval()
        state = registry.seed(
            surface=surface, address=address, session_id=session_id, user_id=user_id,
            thread_id=thread_id, provenance=provenance,
            require_approval=require_approval, now=now,
        )
        logger.info("correspondent seeded surface=%s addr=%s state=%s provenance=%s",
                    surface, address, state, provenance)
        return state
    except Exception as e:  # fail-safe: never let a seed fault break the outbound path
        logger.debug("correspondent auto-seed failed: %s", e)
        return "disabled"
