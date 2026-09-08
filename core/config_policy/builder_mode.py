"""``AGENT_BUILDER_MODE`` — the FIFTH named default bundle (proposal 032).

Sibling of ``autonomy_mode.py`` (the ``AUTONOMY_MODE`` precedent): it moves the
DEFAULTS of a fixed publishing-flag group; an explicit per-flag env always wins
at the call site (this is only the default argument to ``bool_env``). It never
moves money verbs, host access (``AGENT_COMPUTE_POSTURE``) or secrets.

    off    — today. Byte-identical.
    build  — PUBLISH_ENABLED + GITHUB_TOOL_ENABLED default ON; the agent can build,
             version, and publish STATIC pages to an owner-approved address.
    ship   — build + APP_SERVICE_ENABLED + APP_SERVICE_ALLOW_PUBLIC: durable apps
             behind https://<slug>.<APP_SERVICE_BASE_DOMAIN>. Clamps back to
             `build` with a one-time WARN unless the base domain is set AND its
             wildcard certificate exists — the clamp ``full_autonomy_enabled()``
             already performs for AUTONOMY_MODE.
"""
import logging
import os
from typing import Optional

_MODES = ("off", "build", "ship")
_BUILD_FLAGS = frozenset({"PUBLISH_ENABLED", "GITHUB_TOOL_ENABLED"})
_SHIP_FLAGS = frozenset({"APP_SERVICE_ENABLED", "APP_SERVICE_ALLOW_PUBLIC"})
_SHIP_CLAMP_WARNED = False


def agent_builder_mode() -> str:
    """Raw ``AGENT_BUILDER_MODE`` (off|build|ship); unknown values degrade to ``off``
    so a typo never activates a publishing posture. Access-time."""
    raw = (os.getenv("AGENT_BUILDER_MODE") or "").strip().lower()
    return raw if raw in _MODES else "off"


def base_domain() -> str:
    return (os.getenv("APP_SERVICE_BASE_DOMAIN") or "").strip().lower().rstrip(".")


def cert_dir_for(domain: str) -> str:
    """Where the wildcard certificate for *domain* lives: ``APP_SERVICE_CERT_DIR``
    or the certbot layout ``/etc/letsencrypt/live/<domain>``."""
    override = (os.getenv("APP_SERVICE_CERT_DIR") or "").strip()
    if override:
        return override.rstrip("/")
    return f"/etc/letsencrypt/live/{domain}" if domain else ""


def ship_clamp_reason() -> Optional[str]:
    """Why ``ship`` would clamp on THIS deployment (None = it would be granted)."""
    domain = base_domain()
    if not domain:
        return "APP_SERVICE_BASE_DOMAIN is not set"
    cert = os.path.join(cert_dir_for(domain), "fullchain.pem")
    if not os.path.isfile(cert):
        return f"no certificate at {cert}"
    return None


def effective_builder_mode() -> str:
    """The mode after the ship clamp."""
    global _SHIP_CLAMP_WARNED
    mode = agent_builder_mode()
    if mode != "ship":
        return mode
    reason = ship_clamp_reason()
    if reason:
        if not _SHIP_CLAMP_WARNED:
            logging.getLogger(__name__).warning(
                "AGENT_BUILDER_MODE=ship requested but %s — clamping to build", reason)
            _SHIP_CLAMP_WARNED = True
        return "build"
    return "ship"


def publish_enabled() -> bool:
    """The static ship rail's switch: default OFF, ON under ``build``/``ship``;
    explicit env wins. Lives here (not in ``tools/publish``) so the goal-toolset
    gate in the agents tier can read it without an agents->tools import."""
    from core.env import bool_env
    return bool_env("PUBLISH_ENABLED", _builder_capability_default("PUBLISH_ENABLED"))


def _builder_capability_default(flag_name: str) -> bool:
    """Default for a bundle-governed flag under the EFFECTIVE mode. Explicit env
    always wins at the call site."""
    mode = effective_builder_mode()
    if mode == "ship":
        return flag_name in _BUILD_FLAGS or flag_name in _SHIP_FLAGS
    if mode == "build":
        return flag_name in _BUILD_FLAGS
    return False


def builder_mode_display() -> str:
    """One-line display for status seats: ``off`` / ``build`` / ``ship`` /
    ``ship (clamped to build — <reason>)``."""
    mode = agent_builder_mode()
    if mode == "ship":
        reason = ship_clamp_reason()
        return f"ship (clamped to build — {reason})" if reason else "ship"
    return mode


def reset_builder_mode_warnings() -> None:
    """TEST-ONLY seam: clear the one-time ship clamp warning."""
    global _SHIP_CLAMP_WARNED
    _SHIP_CLAMP_WARNED = False
