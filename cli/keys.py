"""CLI key-presence guard + onboarding message + preflight (Seam 1).

Single home for "does the CLI have a *usable* provider key, and what do we do if
not." The gating decisions (warn / onboard / resolve) drive off
``initializable_providers_with_keys`` — the SSOT in ``modules.llm.profiles`` — so a
key that can't bootstrap a client (deepseek: direct client disabled) never passes a
guard only to hard-crash at container build. The raw ``providers_with_keys`` stays
the DISPLAY oracle (doctor/webview show a key is present even when unusable directly).

``no_key_message`` is defined in the neutral ``modules.llm.profiles`` module (so the
LLM manager can reuse it without ``modules/`` importing ``cli/``) and re-exported here
for back-compat.
"""
from __future__ import annotations

import os
import sys

from modules.llm.profiles import (  # noqa: F401  (no_key_message re-exported)
    no_key_message,
    providers_with_keys,
    usable_providers_with_keys,
)

_TRUTHY = {"1", "true", "yes", "on"}


def should_warn_no_key(env=None) -> bool:
    """True when NO *usable* provider API key is present (after env-load/backfill).

    Keys off ``usable_providers_with_keys`` — a deepseek-only environment still
    warns/onboards (its key can't bootstrap a client), AND a malformed/placeholder key
    (too short — the kind BotConfig blanks) still warns instead of passing the guard
    and crashing the manager. Decision is driven purely by usable-key presence, not by
    whether ``~/.polyrob/.env`` exists.
    """
    return not usable_providers_with_keys(env)


def first_run_no_config(env=None, home=None) -> bool:
    """True on a genuine first run: no usable provider key AND no ~/.polyrob/.env."""
    if usable_providers_with_keys(env):
        return False
    if home is None:
        from core.paths import polyrob_home
        home = polyrob_home()
    return not (home / ".env").exists()


def _can_prompt() -> bool:
    """True when it is safe to prompt interactively.

    Needs a readable stdin AND a visible stdout, and must not be a CI / explicitly
    non-interactive run. ``--plain``/``POLYROB_PLAIN`` are RENDER toggles and are
    deliberately NOT coupled here (a piped-stdin REPL is a supported headless path).
    """
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
    except Exception:
        return False
    if str(os.environ.get("CI", "")).strip().lower() in _TRUTHY:
        return False
    if str(os.environ.get("POLYROB_NONINTERACTIVE", "")).strip().lower() in _TRUTHY:
        return False
    return True


def preflight_or_onboard(*, interactive: bool) -> bool:
    """Gate before building a container. Return True to proceed.

    - Usable key already present → True (no prompt).
    - No key, ``interactive`` and a real TTY → run the inline OpenRouter-first key
      wizard in-process, reload env, re-check; True on success.
    - Otherwise → print the canonical no-key message to stderr and return False (the
      caller exits). Never raises.

    Loads the env layers first (idempotent, override=False) so file-based keys in
    ./.polyrob/.env, ~/.polyrob/.env, and config/.env.* are visible — callers don't
    need their own ``load_env`` before the guard.
    """
    try:
        from core.bootstrap import load_env
        load_env(local_mode=True)
    except Exception:
        pass
    if not should_warn_no_key():
        return True
    if interactive and _can_prompt():
        try:
            from cli.commands.init import run_quick_key_setup
            run_quick_key_setup()
            from core.bootstrap import load_env
            load_env(local_mode=True)  # re-layer files; os.environ already set by setup
        except Exception:
            pass  # fail-open into the message path below
        if not should_warn_no_key():
            return True
    import click
    click.echo(no_key_message(), err=True)
    return False
