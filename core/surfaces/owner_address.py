"""The owner-address contract (030 WS-B1): ONE answer to "which surface(s)
reach the owner, and at what address".

Before this, every "notify the owner" path hardcoded Telegram
(`user_delivery.resolve_telegram_recipient` + the telegram sink), so on a
non-Telegram deploy approval prompts, credit-sentinel halts and settlement
alerts became unread telemetry rows.

- ``owner_surface_order()``: the configured fan-out order. ``OWNER_SURFACE``
  (comma list, e.g. ``slack,telegram``) wins; unset keeps the legacy
  telegram-only behavior byte-compatible.
- ``owner_address(container, surface_id, user_id)``: the owner's address on
  that surface — user_directory first, then the per-surface resolver
  (telegram/email reuse the existing canonical resolvers; the rest read
  ``OWNER_<SURFACE>_ID``).
"""
import logging
import os
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

_ENV_BY_SURFACE = {
    "slack": "OWNER_SLACK_ID",
    "discord": "OWNER_DISCORD_ID",
    "signal": "OWNER_SIGNAL_ID",
    "whatsapp": "OWNER_WHATSAPP_ID",
    "x": "OWNER_X_ID",
}


def owner_surface_order() -> List[str]:
    """Configured owner fan-out order. Default = legacy telegram-only."""
    raw = os.getenv("OWNER_SURFACE") or os.getenv("OWNER_SURFACES") or ""
    order = [s.strip().lower() for s in raw.split(",") if s.strip()]
    return order or ["telegram"]


def owner_address(container: Any, surface_id: str, user_id: str = "") -> Optional[str]:
    """The owner's address on ``surface_id``, or None when unreachable there."""
    sid = (surface_id or "").strip().lower()
    uid = str(user_id or "").strip()
    if not sid:
        return None
    # A generic user_directory seam wins when the deployment has one.
    try:
        directory = container.get_service("user_directory") if container else None
        if directory is not None and uid:
            get_addr = getattr(directory, "get_address", None)
            if callable(get_addr):
                addr = get_addr(uid, sid)
                if addr:
                    return str(addr)
    except Exception:
        logger.debug("owner_address: directory lookup failed", exc_info=True)
    if sid == "telegram":
        # Resolve through the module ATTRIBUTE (the documented back-compat
        # alias) so a monkeypatch on either name keeps working.
        import core.surfaces.user_delivery as _ud
        return _ud._resolve_recipient(container, uid)
    if sid == "email":
        try:
            from core.instance import resolve_owner_email
            addr = resolve_owner_email(container)
            return str(addr) if addr else None
        except Exception:
            return None
    env_key = _ENV_BY_SURFACE.get(sid)
    if env_key:
        val = (os.getenv(env_key) or "").strip()
        return val or None
    return None
