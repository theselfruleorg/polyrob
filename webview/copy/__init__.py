"""Every word the console says, in one place.

The 040 audit found the product's voice scattered across 26 templates, 15 inline
scripts and a dozen route handlers, which is how ``MODEL``, ``TOOLS``,
``session 1f9a28e6 · tools filesystem, task, … · /help`` and
``no goals — GOALS_ENABLED=off`` all reached a person's screen. Copy that lives
beside the code that renders it is copy nobody ever reads as copy.

So the strings live in :mod:`webview.copy.en` and the templates call :func:`t`.
That makes the whole vocabulary reviewable in one file, enforceable by one test
(``tests/unit/webview/test_copy_layer_ratchet.py``), and translatable later
without touching a single template.

**A missing key is loud in tests and quiet in production.** Under pytest
:func:`t` raises, so a typo fails the suite. In a running
console it returns the key itself: a console that 500s because a string is
missing is worse than one that shows ``work.placeholder`` for an afternoon.
That asymmetry is deliberate and is the same shape as the rest of the product —
fail loudly where a human is watching, degrade visibly where one is not.
"""
import logging
import sys

from webview.copy.en import STRINGS

__all__ = ["t", "STRINGS", "has", "missing_keys"]

logger = logging.getLogger(__name__)

_WARNED = set()


def _strict() -> bool:
    """True when a missing key should raise rather than degrade.

    ⚠️ The detector is ``pytest`` being imported, and ONLY that. An earlier cut
    also honoured a ``POLYROB_TEST`` env var — a read of a name nothing in this
    tree ever sets, which ``tests/unit/core/test_flags_reverse.py`` correctly
    refused: an undocumented env read is either a flag that needs a catalog row
    or a dead branch, and this one was the second. A belt-and-braces signal
    nobody can send is not a second signal.
    """
    return "pytest" in sys.modules


def t(key: str, **kw) -> str:
    """The string for *key*, with ``{placeholders}`` filled from *kw*.

    Raises ``KeyError`` for an unknown key under test; returns the key itself
    otherwise. A formatting failure degrades to the unformatted string rather
    than to an exception in a request handler — the sentence is still readable,
    just missing a number.
    """
    try:
        text = STRINGS[key]
    except KeyError:
        if _strict():
            raise KeyError(f"no console copy for {key!r} — add it to webview/copy/en.py")
        if key not in _WARNED:
            _WARNED.add(key)
            logger.warning("console copy missing: %s", key)
        return key
    if not kw:
        return text
    try:
        return text.format(**kw)
    except (KeyError, IndexError, ValueError) as exc:
        if _strict():
            raise KeyError(f"copy {key!r} does not take {sorted(kw)}: {exc}") from exc
        logger.warning("console copy %s failed to format: %s", key, exc)
        return text


def has(key: str) -> bool:
    return key in STRINGS


def missing_keys(keys) -> list:
    """The subset of *keys* with no string. For a caller that wants to check a
    whole screen's vocabulary at once rather than discover it one render later."""
    return sorted(k for k in keys if k not in STRINGS)
