"""The copy-layer engine: one ``t``/``has``/``missing_keys`` over any STRINGS.

``core.copy`` and ``webview.copy`` are two vocabularies with one contract —
a missing key raises under pytest and degrades to the key itself in a running
process, a formatting failure degrades to the unformatted sentence. That
contract lived twice, byte for byte; it lives here once and each layer binds
its own STRINGS and its own wording for the error.
"""
from __future__ import annotations

import logging
import sys
from typing import Callable, Iterable, List, Mapping, Tuple


def strict() -> bool:
    """True when a missing key should raise rather than degrade.

    ⚠️ The detector is ``pytest`` being imported, and ONLY that. An earlier cut
    also honoured a ``POLYROB_TEST`` env var nothing in this tree ever sets,
    which ``tests/unit/core/test_flags_reverse.py`` correctly refused: an
    undocumented env read is either a flag that needs a catalog row or a dead
    branch. A belt-and-braces signal nobody can send is not a second signal.
    """
    return "pytest" in sys.modules


def build_copy_layer(strings: Mapping[str, str], *, label: str, file_hint: str,
                     logger: logging.Logger,
                     strict_fn: Callable[[], bool] = strict
                     ) -> Tuple[Callable[..., str], Callable[[str], bool],
                                Callable[[Iterable[str]], List[str]]]:
    """``(t, has, missing_keys)`` bound to *strings*.

    *label* names the layer in log lines and errors (``"core copy"``,
    ``"console copy"``); *file_hint* names the file a missing key belongs in.
    *strict_fn* is consulted on every call so a layer can expose (and a test
    can monkeypatch) its own ``_strict`` seam.
    """
    warned: set = set()

    def strict() -> bool:  # noqa: F811 — the per-layer seam wins
        return strict_fn()

    def t(key: str, **kw) -> str:
        try:
            text = strings[key]
        except KeyError:
            if strict():
                raise KeyError(f"no {label} for {key!r} — add it to {file_hint}")
            if key not in warned:
                warned.add(key)
                logger.warning("%s missing: %s", label, key)
            return key
        if not kw:
            return text
        try:
            return text.format(**kw)
        except (KeyError, IndexError, ValueError) as exc:
            if strict():
                raise KeyError(f"copy {key!r} does not take {sorted(kw)}: {exc}") from exc
            logger.warning("%s %s failed to format: %s", label, key, exc)
            return text

    def has(key: str) -> bool:
        return key in strings

    def missing_keys(keys: Iterable[str]) -> List[str]:
        """The subset of *keys* with no string, for checking a whole screen at once."""
        return sorted(k for k in keys if k not in strings)

    return t, has, missing_keys
