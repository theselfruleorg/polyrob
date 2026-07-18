"""Owner outbound policy (proposal 013 §2.4): who may the agent contact, per surface.

    open      — any syntactically valid target; every send seeded + capped + reported.
    domains   — only owner-listed domains (email-shaped surfaces); others fall back to
                the allowlist.
    allowlist — exact-address owner allowlist (today's behavior; supervised default).
    off       — owner only.

Effective policy = stricter-of(env-or-mode-default, pref) on the ladder. Loosening
beyond the mode default is impossible from a pref — loosening is AUTONOMY_MODE's job;
a pref can only tighten. `resolve_outbound_policy` resolves the (policy, domains) tuple.

T6 (2026-07-16) adds the two enforcement-adjacent helpers the send gates share:
`resolve_outbound_daily_cap` (the open-tier daily-send cap, same pref-over-env
resolution shape) and `notify_first_contact` (the first-contact telemetry event +
owner notice, fired once per send gate AFTER a successful open-tier send to a
brand-new recipient) — so `tools/controller/message_send.py` and
`tools/email_tool.py` share ONE implementation instead of two copies.
"""
from __future__ import annotations

import logging
from core.config_policy import full_autonomy_enabled
import os

logger = logging.getLogger(__name__)

POLICY_LADDER = ("open", "domains", "allowlist", "off")


def _mode_default_policy() -> str:
    try:
        return "open" if full_autonomy_enabled() else "allowlist"
    except Exception:
        return "allowlist"


def resolve_outbound_policy(user_id: str, surface: str,
                            home_dir=None) -> tuple[str, tuple[str, ...]]:
    """Effective (policy, domains) for a tenant. Fail-closed to allowlist on error.

    ``surface`` is accepted but unused in v1 — policy is global today; the param
    reserves room for a per-surface policy in a later revision.

    ``home_dir`` is the preferences-store root (see ``core.prefs``); when ``None``
    (the default) the pref layer is skipped entirely and the result is env/mode-
    default only. A pref can only TIGHTEN the effective policy (never loosen it
    past the env/mode-default) via the ``stricter_policy`` merge kind.
    """
    try:
        env_raw = (os.getenv("OUTBOUND_POLICY") or "").strip().lower()
        base = env_raw if env_raw in POLICY_LADDER else _mode_default_policy()

        env_domains = tuple(
            d.strip().lower() for d in (os.getenv("OUTBOUND_DOMAINS") or "").split(",")
            if d.strip()
        )

        if home_dir is None:
            return base, env_domains

        from core.prefs import resolve as pref_resolve
        policy = pref_resolve("outbound.policy", user_id, home_dir,
                              env_value=base, default=base)
        domains = pref_resolve("outbound.domains", user_id, home_dir,
                               env_value=list(env_domains), default=[])
        return policy, tuple(domains)
    except Exception:
        return "allowlist", ()


def resolve_outbound_daily_cap(user_id: str, home_dir=None) -> int:
    """Effective outbound send cap for an "open"-tier send (013 T6) — the
    tenant+surface-wide daily budget an open/domains policy is capped at.

    Mirrors `resolve_outbound_policy`'s env/pref shape: `OUTBOUND_DAILY_SEND_CAP`
    env is the operator ceiling, `outbound.daily_send_cap` pref can only TIGHTEN
    it (min-merge), and `home_dir=None` skips the pref layer entirely (env/default
    only) — the same "not cleanly obtainable -> skip prefs" contract callers use
    for `resolve_outbound_policy`. Fails open to the default (30) on any error —
    a resolution fault must never silently make every open-tier send uncapped,
    but it also must never brick sends outright.
    """
    try:
        env_raw = os.getenv("OUTBOUND_DAILY_SEND_CAP")
        env_value = None
        if env_raw is not None and env_raw.strip() != "":
            try:
                env_value = int(env_raw)
            except ValueError:
                env_value = None
        if home_dir is None:
            return env_value if env_value is not None else 30
        from core.prefs import resolve as pref_resolve
        return pref_resolve("outbound.daily_send_cap", user_id, home_dir,
                            env_value=env_value, default=30)
    except Exception:
        return 30


async def notify_first_contact(container, user_id: str, session_id: str,
                               surface: str, target: str) -> None:
    """T6 first-contact report: durable telemetry (`outbound_open_send`, mirrors
    `core/surfaces/seed.py`'s `correspondent_pending` pattern) + an owner notice
    via the one user-delivery rail (`core.surfaces.user_delivery.deliver_user_message`).

    Called AFTER a successful open-tier send to a recipient the conversation
    store had no prior row for. Fail-open throughout: a reporting fault must
    never surface as a send failure — the send already succeeded by the time
    this runs.
    """
    try:
        from agents.task.telemetry.event_log import event_log_enabled, get_event_log
        if event_log_enabled():
            get_event_log().record(
                "outbound_open_send", user_id=str(user_id or ""),
                session_id=str(session_id or ""), source=surface,
                attrs={"address": str(target)})
    except Exception:
        logger.debug("outbound_open_send telemetry skipped", exc_info=True)
    try:
        from core.surfaces.user_delivery import deliver_user_message
        await deliver_user_message(
            container, user_id or "",
            f"first contact: sent to new {surface} recipient {target} under "
            "the open outbound policy",
            source="outbound_open_send", session_id=session_id)
    except Exception:
        logger.debug("outbound_open_send owner notice skipped", exc_info=True)
