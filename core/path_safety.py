"""Shared path-confinement helper (single source of truth).

Promoted from agents/task/agent/messages/context_references._is_within_root so the
coding + filesystem tools confine the same way. Uses realpath on both sides so an
in-root symlink (or `..` segment) can't smuggle a write outside the allowed root —
which the previous abspath().startswith(root) checks did not catch.
"""
from __future__ import annotations

import os


def is_within_root(path: str, root: str) -> bool:
    """True iff ``path`` resolves to a location inside ``root`` (no symlink/`..` escape)."""
    try:
        rp = os.path.realpath(path)
        rr = os.path.realpath(root)
        return rp == rr or os.path.commonpath([rp, rr]) == rr
    except Exception:
        return False
