"""The ONE remedy grammar for a flag that is off (026 P4).

Five idioms used to coexist across the hint sites — ``set X=true``, the ``=1``
dialect (``set KB_ENABLED=1``), bare multi-line ``Set …=true`` blocks,
two-remedy tails — and only two of them named the real verb. None said which
file, or that a restart applies. Every owner-facing hint now calls this:

    >>> flag_remedy("KB_ENABLED")
    'enable: `polyrob config set KB_ENABLED true --global` (takes effect: restart)'

A flag with its own feature verb names the verb instead
(``AUTONOMY_ENABLED`` -> ``polyrob autonomy on``).

``tests/test_remedy_grammar_ratchet.py`` forbids a new ``set KEY=true`` literal
anywhere outside this module (shrink-only allowlist for the stragglers).

Pure core: stdlib only.
"""
from __future__ import annotations

from typing import Dict, Tuple

#: flag -> (verb that turns it ON, verb that turns it OFF). A flag listed here
#: is named by its verb, never by `config set`.
FEATURE_VERBS: Dict[str, Tuple[str, str]] = {
    "AUTONOMY_ENABLED": ("polyrob autonomy on", "polyrob autonomy off"),
    "AUTONOMY_HALT": ("polyrob autonomy halt", "polyrob autonomy resume"),
}

_TRUE = frozenset({"true", "1", "on", "yes"})
_FALSE = frozenset({"false", "0", "off", "no"})


def flag_command(key: str, want: str = "true", *, scope_hint: bool = True) -> str:
    """The bare command that sets ``key`` to ``want`` (no prose around it).

    Booleans are always written ``true``/``false`` — the ``=1`` dialect dies
    here. ``scope_hint`` appends ``--global`` (the home file every process on
    this machine reads)."""
    w = str(want).strip()
    wl = w.lower()
    verbs = FEATURE_VERBS.get(key)
    if verbs and wl in _TRUE:
        return verbs[0]
    if verbs and wl in _FALSE:
        return verbs[1]
    if wl in _TRUE:
        w = "true"
    elif wl in _FALSE:
        w = "false"
    cmd = f"polyrob config set {key} {w}"
    return cmd + (" --global" if scope_hint else "")


def flag_remedy(key: str, *, want: str = "true", scope_hint: bool = True) -> str:
    """One remedy line in the house grammar:
    ``enable: `polyrob config set KEY true --global` (takes effect: restart)``.
    """
    wl = str(want).strip().lower()
    action = "enable" if wl in _TRUE else ("disable" if wl in _FALSE else "set")
    cmd = flag_command(key, want, scope_hint=scope_hint)
    if key == "AUTONOMY_HALT":
        return f"{action}: `{cmd}` (live, no restart)"
    return f"{action}: `{cmd}` (takes effect: restart)"


def flags_remedy(*keys: str, want: str = "true", scope_hint: bool = True) -> str:
    """Several flags that must ALL be on — one line, commands joined by ``&&``."""
    cmds = " && ".join(flag_command(k, want, scope_hint=scope_hint) for k in keys)
    wl = str(want).strip().lower()
    action = "enable" if wl in _TRUE else ("disable" if wl in _FALSE else "set")
    return f"{action}: `{cmds}` (takes effect: restart)"


__all__ = ["FEATURE_VERBS", "flag_command", "flag_remedy", "flags_remedy"]
