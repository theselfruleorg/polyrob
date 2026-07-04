"""TTY-safe interactive model picker.

Non-TTY/piped/CI callers (e.g. `polyrob run` invoked from a script or cron) get the
resolved default WITHOUT prompting -- this must never block on stdin. A real TTY gets
a numbered menu grouped by provider, mirroring the `/toolset` and `/persona`
list-then-choose pattern. No new dependency -- `click` is already a CLI dependency.
"""
from __future__ import annotations

import sys
from typing import Optional

import click

from modules.llm.available_models import available_models, steer_notes


def _resolved_default(env, choices: list) -> Optional[tuple]:
    """Best-effort (provider, model) to preselect, from the shared runtime resolver.

    ``resolve_runtime_config`` is the one precedence ladder the CLI/server both use
    (explicit > pinned > cli_store_default > first-keyed-provider > last-resort).
    Called here with no explicit/pinned/cli_store args, so in practice it only ever
    exercises the last two legs -- both of which return ``model=None`` (provider-only;
    the caller is expected to fill in a model). When it doesn't name a model, resolve
    to *that provider's own* flagged default from ``choices`` (``ModelChoice.
    is_default``) rather than leaving ``model=None``: a bare ``(provider, None)`` can
    never equal a real ``ModelChoice`` tuple, so the caller's index lookup would
    silently fall back to whatever model happens to be FIRST in that provider's
    *registration order* in the registry -- which is not always the provider's actual
    default (verified: for an OpenAI-only env, registration-order-first is
    'gpt-5.1' but the flagged default is 'gpt-5'; same divergence for Gemini). Without
    this backfill, the star / blank-Enter default would silently point at the wrong
    model for those providers.

    Wrapped in try/except so any signature drift in ``resolve_runtime_config``
    degrades to "no preselect" (nothing starred; blank-Enter falls back to the first
    listed model), never crashes the picker.
    """
    try:
        from core.runtime_config import resolve_runtime_config
        p, m = resolve_runtime_config(None, None, env=env)
    except Exception:
        return None
    if not p:
        return None
    if m:
        return (p, m)
    default_choice = next((c for c in choices if c.provider == p and c.is_default), None)
    return (default_choice.provider, default_choice.model) if default_choice else None


def pick_model(env=None, *, preselect: "tuple[str, str] | None" = None,
                non_tty_default: "tuple[str, str] | None" = None,
                input_fn=input, isatty_fn=None) -> "tuple[str, str] | None":
    """Interactively pick a (provider, model). TTY-safe: never prompts off a real TTY.

    *env* is passed straight through to ``available_models``/``steer_notes``/
    ``resolve_runtime_config`` (each defaults to ``os.environ`` internally when
    ``env`` is None, same convention as the rest of the CLI). *input_fn*/*isatty_fn*
    are injectable so tests can drive the menu without a real TTY; defaults are
    ``input``/``sys.stdin.isatty``.

    Returns ``(provider, model)``, or ``None`` on cancel ('q'), invalid input, or when
    no models are available for the given *env* (e.g. no usable API key).
    """
    isatty = isatty_fn if isatty_fn is not None else sys.stdin.isatty
    choices = available_models(env)
    if not choices:
        for n in steer_notes(env):
            click.echo(click.style(n, fg="yellow"), err=True)
        return None

    default = preselect or _resolved_default(env, choices)
    default_idx = next((i for i, c in enumerate(choices)
                        if default and (c.provider, c.model) == tuple(default)), 0)

    if not isatty():
        # Never block a non-interactive caller (scripts, `polyrob run` in CI, piped
        # stdin, cron) on a prompt -- that is the entire point of this branch. Note
        # this check happens strictly BEFORE any input_fn(...) call below.
        return non_tty_default or (default if default else
                                    (choices[default_idx].provider, choices[default_idx].model))

    # Render a grouped-by-provider numbered menu.
    last_provider = None
    for i, c in enumerate(choices):
        if c.provider != last_provider:
            click.echo(f"  {c.provider}")
            last_provider = c.provider
        star = "★" if i == default_idx else " "
        caps = ",".join(t for t, on in (("tools", c.supports_tools), ("vision", c.supports_vision)) if on)
        click.echo(f"    {i + 1}) {star} {c.display_name}   {c.pricing_hint}  [{caps}]")
    for n in steer_notes(env):
        click.echo(click.style("  " + n, fg="yellow"))
    click.echo("  c) type a custom model string")

    raw = input_fn(
        f"Pick a number (Enter = keep {choices[default_idx].display_name}), "
        "'c' custom, 'q' cancel: "
    ).strip()
    if raw == "":
        return (choices[default_idx].provider, choices[default_idx].model)
    if raw.lower() == "q":
        return None
    if raw.lower() == "c":
        prov = input_fn("Provider: ").strip()
        mod = input_fn("Model string: ").strip()
        return (prov, mod) if prov and mod else None
    if raw.isdigit() and 1 <= int(raw) <= len(choices):
        c = choices[int(raw) - 1]
        return (c.provider, c.model)
    return None
