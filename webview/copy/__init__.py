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

from core.copy._engine import build_copy_layer, strict as _strict
from webview.copy.en import STRINGS

__all__ = ["t", "STRINGS", "has", "missing_keys"]

logger = logging.getLogger(__name__)

# One engine (core/copy/_engine.py) bound to the console's vocabulary; the
# core tier binds the same engine to its own in core/copy.
# ``_strict`` is this layer's seam (tests monkeypatch it); the lambda reads the
# module global at call time so the patch is what runs.
t, has, missing_keys = build_copy_layer(
    STRINGS, label="console copy", file_hint="webview/copy/en.py", logger=logger,
    strict_fn=lambda: _strict())
