"""Short id prefixes on every owner seat — the ONE resolver (proposal 125 S14).

Every chat listing shows truncated ids (`a1b2c3d4`), so requiring the full uuid
back would make the listings unusable. An ambiguous prefix is an error, never a
guess: these verbs cancel goals, stop jobs and settle invoices.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

#: The "stop for good" flag in its typed forms (a phone keyboard turns `--` into a dash).
FOREVER_FLAGS = ("--forever", "—forever", "–forever")


def resolve_prefix(prefix: str, ids: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a short id PREFIX against IDS -> ``(full_id, error)``."""
    p = (prefix or "").strip()
    if not p:
        return None, "no id given"
    if p in ids:
        return p, None
    matches = [i for i in ids if i.startswith(p)]
    if not matches:
        return None, f"no match for `{p}`"
    if len(matches) > 1:
        return None, f"`{p}` matches {len(matches)} ids — use more characters"
    return matches[0], None
