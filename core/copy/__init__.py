"""Core-tier copy: the strings a module below the console must say, in one place.

This is the core twin of :mod:`webview.copy`. The console has its own vocabulary
(``webview/copy/en.py``); this holds the strings that the CLI, the REPL and the
pure Python narrator share — copy that cannot live in the webview layer because
core/agents code may not import it (``tests/test_layering_ratchet.py``).

**A missing key is loud in tests and quiet in production.** Under pytest
:func:`t` raises, so a typo fails the suite; in a running process it returns the
key itself. A transcript that shows ``chat.act.deploy_running`` for an afternoon
is worse only than one that 500s because a string is missing — the same
fail-loud-where-watched, degrade-visibly-where-not asymmetry the console uses.
"""
import logging
import sys

from core.copy.en import STRINGS

__all__ = ["t", "STRINGS", "has", "missing_keys"]

logger = logging.getLogger(__name__)

_WARNED = set()


def _strict() -> bool:
    """True when a missing key should raise rather than degrade.

    The only signal is ``pytest`` being imported, matching ``webview.copy``.
    """
    return "pytest" in sys.modules


def t(key: str, **kw) -> str:
    """The string for *key*, with ``{placeholders}`` filled from *kw*.

    Raises ``KeyError`` for an unknown key under test; returns the key itself
    otherwise. A formatting failure degrades to the unformatted string rather
    than to an exception in a hot path — the sentence is still readable, just
    missing a number.
    """
    try:
        text = STRINGS[key]
    except KeyError:
        if _strict():
            raise KeyError(f"no core copy for {key!r} — add it to core/copy/en.py")
        if key not in _WARNED:
            _WARNED.add(key)
            logger.warning("core copy missing: %s", key)
        return key
    if not kw:
        return text
    try:
        return text.format(**kw)
    except (KeyError, IndexError, ValueError) as exc:
        if _strict():
            raise KeyError(f"copy {key!r} does not take {sorted(kw)}: {exc}") from exc
        logger.warning("core copy %s failed to format: %s", key, exc)
        return text


def has(key: str) -> bool:
    return key in STRINGS


def missing_keys(keys) -> list:
    """The subset of *keys* with no string, for checking a whole screen at once."""
    return sorted(k for k in keys if k not in STRINGS)
