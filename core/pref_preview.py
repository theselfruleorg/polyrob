"""The one line an owner reads about a pending pref change (096 A7).

Extracted from ``core.prefs`` (god-file ratchet). Pure: the value in force is
read through the ``current`` callable the caller passes, so this module never
imports ``core.prefs`` back.
"""
from __future__ import annotations

from typing import Any, Callable


def pref_change_preview(payload: dict, key: str, current: Callable[[], Any]) -> str:
    """``remove 'x' from key`` for an operation-based removal (P2 T4: show the
    OPERATION — the applied value is recomputed against the current list at
    promote time); otherwise ``key: current → proposed``, the value in force
    beside the proposed one, not raw JSON. Fail-open to ``key = proposed``."""
    if payload.get("op") == "remove_entry":
        return f"remove {payload.get('entry')!r} from {key}"
    try:
        return f"{key}: {current()!r} → {payload.get('value')!r}"
    except Exception:
        return f"{key} = {payload.get('value')!r}"
