"""Correspondent auto-seed guardrail (WS-B Fusion must-fix #5; generic since A1/A2).

When the agent INITIATES contact with a new external address, a correspondent binding
is seeded so their reply can later route back as DATA. Originally email-only
(the former ``surfaces/email/seed.py`` shim, deleted F-3g); lives in core so EVERY outbound
path (the ``message`` tool, any surface) can seed. Auto-seed is the soft underbelly
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


def _event_log():
    """Best-effort telemetry event log (None when disabled/unavailable)."""
    try:
        from agents.task.telemetry.event_log import event_log_enabled, get_event_log
        if event_log_enabled():
            return get_event_log()
    except Exception:
        pass
    return None


def effective_max_new_per_day(user_id, home_dir) -> int:
    """Tenant-effective new-correspondent seeding cap: the
    ``CORRESPONDENT_MAX_NEW_PER_DAY`` env ceiling min-merged with the guarded
    ``outbound.max_new_recipients_per_day`` pref — the pref can only TIGHTEN
    (018 P0.2; this key was DEAD: settable/displayed but the cap check read the
    env directly). ``env=0`` is a REAL ceiling ("no new correspondents"), not a
    disabled sentinel (``min_value=0`` in the spec), so it is always fed to the
    min-merge. Fail-open to the env value."""
    from agents.task.surface_config import SurfaceConfig
    env_cap = SurfaceConfig.correspondent_max_new_per_day()
    try:
        from core import prefs
        out = prefs.resolve("outbound.max_new_recipients_per_day", user_id,
                            home_dir, env_value=env_cap, default=env_cap)
        return int(out) if out is not None else env_cap
    except Exception:
        return env_cap


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
        was_new = True
        try:
            was_new = not registry.exists(
                surface=surface, address=address, user_id=user_id, thread_id=thread_id)
        except Exception:
            was_new = False  # can't tell -> don't double-notify / don't cap-refuse
        # The per-day cap bounds NEW bindings only — an idempotent re-seed of an
        # existing correspondent must never be refused (A5 made "refused" block the
        # send, so cap-refusing an existing address would cut off live conversations).
        if was_new:
            from core.surfaces.user_delivery import _home_dir_for_container
            cap = effective_max_new_per_day(user_id, _home_dir_for_container(container))
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
        # E5 (2026-07-13 review): a NEW pending binding is otherwise invisible —
        # replies silently DENY until the owner approves. Be loud: WARN + telemetry
        # event (shown by `polyrob owner pending`). Fail-soft, never affects the seed.
        if was_new and state == "pending":
            logger.warning(
                "correspondent %s:%s is PENDING — their replies are NOT routable until "
                "`polyrob owner approve %s %s`", surface, address, surface, address)
            el = _event_log()
            if el is not None:
                try:
                    el.record("correspondent_pending", user_id=user_id,
                              session_id=session_id or "", source=surface,
                              attrs={"address": address, "provenance": provenance})
                except Exception:
                    pass
        return state
    except Exception as e:  # fail-safe: never let a seed fault break the outbound path
        logger.debug("correspondent auto-seed failed: %s", e)
        return "disabled"
