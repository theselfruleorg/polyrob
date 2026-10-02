"""Which chat surfaces this configuration would actually start.

ONE reading of the token/flag pair, shared by ``polyrob doctor`` (which
describes it) and ``polyrob service`` (which decides whether a background
service is worth offering). Before 062 the description lived inline in
``doctor.setup_lines`` and nothing else could ask the question.

The nuance the two callers must not re-derive: ``polyrob gateway`` starts a
surface only when its ``*_SURFACE_ENABLED`` flag is on, while the standalone
commands (``polyrob telegram``/``discord``/``slack``) set that flag themselves
and run off the token alone. So a token without a flag is a real, actionable
state — not "off".
"""
from __future__ import annotations

from typing import Optional

# 064 F1: both tables derive from the surface catalog. A row with a
# ``token_env`` is a token surface (doctor cross-checks the token against the
# flag); the rest are flag-only.


def _token_surfaces() -> tuple:
    """(display name, flag var, token var) for every surface with a token."""
    from core.surfaces.catalog import surfaces
    return tuple((s.display_name, s.enabled_flag, s.token_env)
                 for s in surfaces() if s.token_env)


def _flag_only_surfaces() -> tuple:
    """(display name, flag var) — no separate token signal to cross-check."""
    from core.surfaces.catalog import surfaces
    return tuple((s.display_name, s.enabled_flag)
                 for s in surfaces() if not s.token_env)


TOKEN_SURFACES = _token_surfaces()
FLAG_ONLY_SURFACES = _flag_only_surfaces()


def flag_on(value) -> bool:
    """Mirror ``core.env.parse_bool``'s falsey-DENYlist: anything not falsey is on.
    An allow-list here once read ``DISCORD_SURFACE_ENABLED=enabled`` as OFF while
    the runtime read it as ON."""
    from core.env import parse_bool
    return parse_bool(value, False) if value is not None else False


def gateway_surfaces(env: dict) -> list[str]:
    """Surfaces ``polyrob gateway`` would actually start with this env."""
    out: list[str] = []
    for name, flag_key, token_key in _token_surfaces():
        if flag_on(env.get(flag_key)) and (env.get(token_key) or "").strip():
            out.append(name)
    for name, flag_key in _flag_only_surfaces():
        if flag_on(env.get(flag_key)):
            out.append(name)
    return out


def describe_surfaces(env: dict) -> list[str]:
    """Every configured-ish surface with the state that made it so."""
    out: list[str] = []
    for name, flag_key, token_key in _token_surfaces():
        on = flag_on(env.get(flag_key))
        has_token = bool((env.get(token_key) or "").strip())
        if on and has_token:
            out.append(name)
        elif on:
            out.append(f"{name} (enabled, token missing)")
        elif has_token:
            out.append(f"{name} (token only — set {flag_key} to run via gateway)")
    for name, flag_key in _flag_only_surfaces():
        if flag_on(env.get(flag_key)):
            out.append(name)
    return out


def surface_choices() -> list[str]:
    """The names the setup wizard offers, in the order it offers them."""
    return [name for name, _f, _t in _token_surfaces()] + ["email"]


def token_var(name: str) -> Optional[str]:
    for surface, _flag, token in _token_surfaces():
        if surface == name:
            return token
    return None


def flag_var(name: str) -> Optional[str]:
    for surface, flag, _token in _token_surfaces():
        if surface == name:
            return flag
    for surface, flag in _flag_only_surfaces():
        if surface == name:
            return flag
    return None
