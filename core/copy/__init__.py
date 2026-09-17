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

from core.copy._engine import build_copy_layer, strict as _strict
from core.copy.en import STRINGS

__all__ = ["t", "STRINGS", "has", "missing_keys"]

logger = logging.getLogger(__name__)

# One engine (core/copy/_engine.py) bound to this layer's vocabulary; the
# console binds the same engine to its own in webview/copy.
# ``_strict`` is this layer's seam (tests monkeypatch it); the lambda reads the
# module global at call time so the patch is what runs.
t, has, missing_keys = build_copy_layer(
    STRINGS, label="core copy", file_hint="core/copy/en.py", logger=logger,
    strict_fn=lambda: _strict())
