"""WS-D: owner/access quick-access — one read of the access SSOT.

A single place that reports who the agent serves and the per-surface access posture, so
the `polyrob owner` CLI (and any admin surface) doesn't re-derive it from scattered
flags. Owner-by-email is reported as a hard OFF in v1 (a forgeable From: can never
confer OWNER tier until verified-sender lands).
"""
from __future__ import annotations

from typing import Any, Dict


def owner_access_summary() -> Dict[str, Any]:
    from core.instance import resolve_owner_principal
    from agents.task.surface_config import SurfaceConfig
    return {
        # STRICT: report only an EXPLICITLY-bound owner (None = running on the
        # auto-derived instance-id default), so the summary reflects real config.
        "owner_principal": resolve_owner_principal(default_to_instance=False),
        "correspondent_access_enabled": SurfaceConfig.correspondent_access_enabled(),
        "require_approval": SurfaceConfig.correspondent_require_approval(),
        "max_new_correspondents_per_day": SurfaceConfig.correspondent_max_new_per_day(),
        "surfaces": {
            "telegram": SurfaceConfig.telegram_surface_enabled(),
            "whatsapp": SurfaceConfig.whatsapp_surface_enabled(),
            "email": SurfaceConfig.email_surface_enabled(),
        },
        "owner_by_email": False,  # v1: hard OFF (forgeable From:)
    }
