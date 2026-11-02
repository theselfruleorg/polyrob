"""095 W4 — the ONE signal that an owner's live self-facts changed on disk.

The writers of the preferences file, ``owner.md`` and the SELF doc call
:func:`notify`; a running agent compares :func:`epoch` (and the files' mtimes,
which also catch a write by ANOTHER process) at its step boundary and pushes the
current text as a ``<state-delta>`` part (``agents/task/agent/core/state_delta.py``).
``core`` never imports ``agents`` (layering ratchet), so the signal lives here and
the agent tier only reads it.
"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()
_EPOCH = 0


def notify(kind: str = "") -> int:
    """A self-fact writer committed a change (``kind``: ``prefs`` | ``owner_doc``
    | ``self_doc``; informational). Returns the new epoch. Never raises."""
    global _EPOCH
    with _LOCK:
        _EPOCH += 1
        return _EPOCH


def epoch() -> int:
    """How many self-fact writes this process has seen (monotonic)."""
    return _EPOCH
