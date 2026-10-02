"""What the owner switched OFF (034 §3.5 / §11.1) — one table, three read sites.

``/goal cancel <id>`` used to delete a row, and the owner meant "not this
again". The seeder reseeded the title four hours later (``cancelled`` is not a
live status, so a cancel LOWERED the stream throttle), ``create()``'s dedup
skipped cancelled rows, and the planner had no section that said "the owner
stopped this". His stop landed nowhere that anything read.

This is the place it lands. ONE table in ``goals.db``, written only by an owner
verb (never by the model), and read by exactly three sites:

1. ``agents.task.goals.streams.stream_is_due`` / ``seed_stream`` — a suppressed
   stream is not due, a suppressed leg is not seeded;
2. ``GoalBoard.create`` — a suppressed title is REFUSED, before and independent
   of the dedup check; ``force=True`` does not bypass it;
3. ``agents.task.goals.planner.build_planner_prompt`` — a ``SUPPRESSED`` section.

``/goal allow <id|title>`` revokes. Tenant-scoped throughout.

A READ never creates the table (an absent table reads as "nothing is off");
only :func:`suppress` creates it (``core.sqlite_util.init_schema``).

Pure core: stdlib + ``core.sqlite_util``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

from core.goal_vocab import normalize_title
from core.sqlite_util import execute_retry, init_schema

SCOPE_TITLE = "title"
SCOPE_OBJECTIVE = "objective"
SCOPE_STREAM = "stream"
SCOPES = (SCOPE_TITLE, SCOPE_OBJECTIVE, SCOPE_STREAM)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS goal_suppressions (
    user_id      TEXT NOT NULL,
    scope        TEXT NOT NULL,          -- 'title' | 'objective' | 'stream'
    key          TEXT NOT NULL,          -- normalised title | objective_id | stream_id
    label        TEXT NOT NULL DEFAULT '',   -- what the owner sees (the title as written)
    reason       TEXT NOT NULL DEFAULT '',   -- the owner's own words, verbatim
    created_at   REAL NOT NULL,
    expires_at   REAL,                   -- NULL = until revoked
    PRIMARY KEY (user_id, scope, key)
);
"""


@dataclass(frozen=True)
class Suppression:
    user_id: str
    scope: str
    key: str
    label: str
    reason: str
    created_at: float
    expires_at: Optional[float] = None


def _key(scope: str, value: str) -> str:
    v = str(value or "").strip()
    return normalize_title(v) if scope == SCOPE_TITLE else v


def _table_exists(db_path: str) -> bool:
    import os
    if not db_path or not os.path.exists(db_path):
        return False
    row = execute_retry(
        db_path, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='goal_suppressions'",
        (), fetch="one")
    return row is not None


def _row(r) -> Suppression:
    return Suppression(user_id=r["user_id"], scope=r["scope"], key=r["key"],
                       label=r["label"] or r["key"], reason=r["reason"] or "",
                       created_at=float(r["created_at"] or 0),
                       expires_at=(float(r["expires_at"]) if r["expires_at"] is not None
                                   else None))


def suppress(db_path: str, *, user_id: str, scope: str, value: str,
             label: str = "", reason: str = "", expires_at: Optional[float] = None,
             now: Optional[float] = None) -> Suppression:
    """Switch ``value`` OFF for ``user_id`` (idempotent: a second call refreshes it)."""
    if scope not in SCOPES:
        raise ValueError(f"unknown suppression scope: {scope!r}")
    if not user_id:
        raise ValueError("a suppression needs a tenant user_id")
    key = _key(scope, value)
    if not key:
        raise ValueError("nothing to switch off: empty key")
    init_schema(db_path, _SCHEMA)
    ts = time.time() if now is None else float(now)
    execute_retry(
        db_path,
        """INSERT INTO goal_suppressions
               (user_id, scope, key, label, reason, created_at, expires_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(user_id, scope, key) DO UPDATE SET
               label=excluded.label, reason=excluded.reason,
               created_at=excluded.created_at, expires_at=excluded.expires_at""",
        (user_id, scope, key, (label or str(value))[:200], (reason or "")[:500], ts,
         expires_at))
    return Suppression(user_id, scope, key, (label or str(value))[:200], reason or "",
                       ts, expires_at)


def allow(db_path: str, *, user_id: str, scope: str, value: str) -> bool:
    """Turn ``value`` back ON. True when a suppression was removed."""
    if not _table_exists(db_path):
        return False
    n = execute_retry(
        db_path, "DELETE FROM goal_suppressions WHERE user_id=? AND scope=? AND key=?",
        (user_id, scope, _key(scope, value)))
    return bool(n)


def active(db_path: str, *, user_id: str, now: Optional[float] = None) -> List[Suppression]:
    """Every suppression in force for ``user_id``, newest first."""
    if not _table_exists(db_path):
        return []
    ts = time.time() if now is None else float(now)
    rows = execute_retry(
        db_path,
        """SELECT * FROM goal_suppressions
            WHERE user_id=? AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at DESC""",
        (user_id, ts), fetch="all") or []
    return [_row(r) for r in rows]


def find(db_path: str, *, user_id: str, scope: str, value: str,
         now: Optional[float] = None) -> Optional[Suppression]:
    """The suppression in force for (scope, value), or ``None``."""
    if not value or not _table_exists(db_path):
        return None
    ts = time.time() if now is None else float(now)
    r = execute_retry(
        db_path,
        """SELECT * FROM goal_suppressions
            WHERE user_id=? AND scope=? AND key=?
              AND (expires_at IS NULL OR expires_at > ?)""",
        (user_id, scope, _key(scope, value), ts), fetch="one")
    return _row(r) if r is not None else None


def match(db_path: str, *, user_id: str, text: str,
          now: Optional[float] = None) -> List[Suppression]:
    """Suppressions whose key or label matches ``text`` (exact normalised title
    first, then a prefix) — how ``/goal allow <title>`` finds its row."""
    rows = active(db_path, user_id=user_id, now=now)
    want = normalize_title(text or "")
    if not want:
        return []
    exact = [s for s in rows
             if s.key == want or normalize_title(s.label) == want or s.key == text.strip()]
    if exact:
        return exact
    return [s for s in rows
            if s.key.startswith(want) or normalize_title(s.label).startswith(want)]


def blocking(db_path: str, *, user_id: str, title: str = "",
             objective_id: Optional[str] = None, stream_id: Optional[str] = None,
             now: Optional[float] = None) -> Optional[Suppression]:
    """The first suppression that forbids creating this goal, or ``None``."""
    for scope, value in ((SCOPE_TITLE, title), (SCOPE_STREAM, stream_id),
                         (SCOPE_OBJECTIVE, objective_id)):
        if value:
            hit = find(db_path, user_id=user_id, scope=scope, value=str(value), now=now)
            if hit is not None:
                return hit
    return None


def describe(s: Suppression) -> str:
    """One owner-readable line for the OFF section."""
    what = {SCOPE_TITLE: "", SCOPE_STREAM: "stream ", SCOPE_OBJECTIVE: "objective "}.get(
        s.scope, "")
    reason = f" — “{s.reason}”" if s.reason else ""
    return f"{what}{s.label}{reason}"


__all__ = [
    "SCOPES", "SCOPE_OBJECTIVE", "SCOPE_STREAM", "SCOPE_TITLE", "Suppression",
    "active", "allow", "blocking", "describe", "find", "match", "suppress",
]
