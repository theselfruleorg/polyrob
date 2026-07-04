"""Webgate config object — the deployment posture SSOT.

POLYROB's `webview/` was built multitenant-first (JWT/SIWE auth, ownership,
profile/billing/admin pages, bound on `0.0.0.0`). The *primitive* is the
single-user, local-first webgate: loopback bind, no auth, no admin pages, every
session owned by the local owner. Own-ops (public status page + owner login)
and multitenant (full SaaS UI) are layers on top, gated by posture.

This module is the single source of truth for the deployment posture
(``local`` | ``own_ops`` | ``multitenant``) and the derived bind/ownership
decisions. It is consulted at seam points in ``webview/server.py``
(middleware short-circuit, ownership short-circuit, page/router mount-gate)
and in ``webview/server_launcher.py`` (bind host/port).

Landmine (AGENTS.md): ``BotConfig.get(flag, default)`` is a ``getattr`` that
silently returns the default — NEVER use it for these flags. Read ``os.environ``
directly here.
"""
import logging
import os
from typing import Dict, Optional

from core.env import bool_env
from core.instance import (
    console_display_name as _console_display_name,
    resolve_instance_id,
    resolve_owner_principal,
)

logger = logging.getLogger(__name__)

_POSTURES = ("local", "own_ops", "multitenant")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _explicit_host_override() -> Optional[str]:
    """WEBGATE_HOST / WEBVIEW_HOST, if set — read directly (no bind_host() call, to
    avoid the posture<->bind_host circular dependency; see B1 docstring)."""
    return os.environ.get("WEBGATE_HOST", os.environ.get("WEBVIEW_HOST"))


def posture() -> str:
    """The deployment posture: "local" | "own_ops" | "multitenant". SSOT.

    Resolution order:
      1. Explicit POLYROB_POSTURE (case-insensitive) — wins outright.
      2. WEBGATE_MULTITENANT=true (back-compat) -> "multitenant".
      3. Derive from an explicit WEBGATE_HOST/WEBVIEW_HOST override: loopback -> "local",
         anything else -> "own_ops".
      4. No explicit host override and WEBGATE_MULTITENANT is not truthy -> "local"
         (today's default: loopback, no auth — Posture 0 must not regress).

    An explicit POLYROB_POSTURE that doesn't match one of the valid values (a typo,
    e.g. "own-ops") is NOT silently ignored (B1-LOW): it logs a warning naming the
    bad value, then falls through to the rest of the derivation below — so a
    misconfigured operator gets a signal instead of silently landing on "local"
    (no auth) when they thought they set a public posture.
    """
    explicit_raw = os.environ.get("POLYROB_POSTURE", "").strip()
    explicit = explicit_raw.lower()
    if explicit in _POSTURES:
        return explicit
    if explicit_raw:
        logger.warning(
            "POLYROB_POSTURE=%r is not a recognized posture (expected one of %s) — "
            "ignoring it and deriving the posture instead.",
            explicit_raw, _POSTURES,
        )

    if bool_env("WEBGATE_MULTITENANT", False):
        return "multitenant"

    host = _explicit_host_override()
    if host is not None:
        return "local" if host.strip() in _LOOPBACK_HOSTS else "own_ops"

    return "local"


def is_multitenant() -> bool:
    """True when the multitenant layer (auth/ownership/admin pages) is enabled.

    Back-compat accessor — equivalent to ``posture() == "multitenant"``.
    """
    return posture() == "multitenant"


def is_own_ops() -> bool:
    """True when posture() == "own_ops" (public own-ops instance, owner-login gated)."""
    return posture() == "own_ops"


def is_local() -> bool:
    """True when posture() == "local" (loopback, no auth — the primitive)."""
    return posture() == "local"


def requires_owner_login() -> bool:
    """True for own_ops/multitenant — console access needs SOME authenticated identity.

    False only for "local": the loopback operator IS the owner, no login needed.
    """
    return posture() != "local"


def bind_host() -> str:
    """Address the webgate binds to.

    Multitenant/own_ops -> ``0.0.0.0`` (an operator fronts it with a reverse proxy);
    local -> ``127.0.0.1`` (loopback only — the owner's own machine).
    An explicit ``WEBGATE_HOST``/``WEBVIEW_HOST`` override always wins.
    """
    default = "127.0.0.1" if is_local() else "0.0.0.0"
    return os.environ.get("WEBGATE_HOST", os.environ.get("WEBVIEW_HOST", default))


def bind_port() -> int:
    """Port the webgate binds to (default 5050 — the service port-of-record)."""
    return int(os.environ.get("WEBGATE_PORT", os.environ.get("WEBVIEW_PORT", "5050")))


def local_owner_id() -> str:
    """The single owner of this instance in single-user mode.

    Reuses instance/owner resolution: the bound owner principal if any, else
    ``POLYROB_LOCAL_OWNER``, else the instance id (defaults to ``"rob"``).
    """
    return (
        # STRICT: an explicitly-bound owner wins, but POLYROB_LOCAL_OWNER must rank
        # ABOVE the instance-id default — so resolve the principal WITHOUT its instance
        # fold-in here and let the layered fallback below own the instance default.
        resolve_owner_principal(default_to_instance=False)
        or os.environ.get("POLYROB_LOCAL_OWNER")
        or resolve_instance_id()  # defaults to "rob"
    )


def console_display_name() -> str:
    """Product display name for the console shell. See core.instance.console_display_name."""
    return _console_display_name()


def _strip_scheme(url: str) -> str:
    return url.split("://", 1)[-1]


def branding_config() -> Dict[str, str]:
    """Env-driven UI copy for the console shell: support link, access-gate
    label, and footer domain links. All independently overridable so an OSS/
    instance deploy isn't locked to any one instance's domain / support handle /
    access-gate copy baked in at authoring time. Read fresh on every call (not
    memoized) so tests can monkeypatch env without a module reload.

    Keys: support_url, support_display, support_handle, access_gate_label,
    brand_url, brand_display, org_url, org_display, terms_url, privacy_url.
    """
    support_url = os.environ.get("POLYROB_SUPPORT_URL", "https://t.me/tmachinrobot").strip()
    brand_url = os.environ.get("POLYROB_BRAND_URL", "https://your-polyrob-host.example").strip()
    org_url = os.environ.get("POLYROB_ORG_URL", "https://theselfrule.org").strip()
    return {
        "support_url": support_url,
        "support_display": _strip_scheme(support_url),
        "support_handle": os.environ.get("POLYROB_SUPPORT_HANDLE", "@TMACHINROBOT").strip(),
        "access_gate_label": os.environ.get("POLYROB_ACCESS_GATE_LABEL", "DEN holders").strip(),
        "brand_url": brand_url,
        "brand_display": _strip_scheme(brand_url),
        "org_url": org_url,
        "org_display": _strip_scheme(org_url),
        "terms_url": os.environ.get("POLYROB_TERMS_URL", f"{brand_url}/terms").strip(),
        "privacy_url": os.environ.get("POLYROB_PRIVACY_URL", f"{brand_url}/privacy").strip(),
    }


__all__ = [
    "posture", "is_multitenant", "is_own_ops", "is_local", "requires_owner_login",
    "bind_host", "bind_port", "local_owner_id", "console_display_name", "branding_config",
]
