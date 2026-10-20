"""What the owner switched OFF (034 §3.5 / §11.1) — one table, three read sites.

``/goal cancel <id>`` used to delete a row, and the owner meant "not this
again". ``create()``'s dedup skipped cancelled rows, so the same title came
back. His stop landed nowhere that anything read.

This is the place it lands. ONE table in ``goals.db``, written only by an owner
verb (never by the model), and read by ``GoalBoard.create``: a suppressed title
is REFUSED, before and independent of the dedup check; ``force=True`` does not
bypass it. The goal views list it (the OFF section).

``/goal allow <id|title>`` revokes. Tenant-scoped throughout. :func:`stop_goal`
is the ONE owner stop (once, or ``forever`` = suppress) every path calls.

A READ never creates the table (an absent table reads as "nothing is off");
only :func:`suppress` creates it (``core.sqlite_util.init_schema``).

Two scopes share the table (work order 0011 — an owner stop is STATE, not prose):
``title`` (a goal title, read by ``GoalBoard.create``) and ``cron_lane`` (a cron
lane — the job's first pinned skill, else its rig — read by
``CronService.schedule``). The store is ``goals.db`` beside ``cron.db``:
:func:`stops_db_path` / :func:`stops_db_beside`.

Pure core: stdlib + ``core.sqlite_util`` (+ ``core.runtime_paths`` for the path).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

from core.goal_vocab import normalize_title
from core.sqlite_util import execute_retry, init_schema

SCOPE_TITLE = "title"
#: 0011: an owner stop of a cron lane (key = the lane, lowercased).
SCOPE_CRON_LANE = "cron_lane"
SCOPES = (SCOPE_TITLE, SCOPE_CRON_LANE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS goal_suppressions (
    user_id      TEXT NOT NULL,
    scope        TEXT NOT NULL,          -- 'title' | 'cron_lane'
    key          TEXT NOT NULL,          -- normalised title | lane
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
    if scope == SCOPE_TITLE:
        return normalize_title(v)
    return v.lower() if scope == SCOPE_CRON_LANE else v


def stops_db_path(data_dir: Optional[str] = None) -> str:
    """The stop store (``goals.db``) of *data_dir* (or the data home)."""
    from core.runtime_paths import goals_db_path
    return goals_db_path(data_dir)


def stops_db_beside(sibling_db_path: str) -> str:
    """The stop store in the same data home as *sibling_db_path* (``cron.db``)."""
    import os
    return stops_db_path(os.path.dirname(os.path.abspath(sibling_db_path)))


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


def active(db_path: str, *, user_id: str, now: Optional[float] = None,
           scope: Optional[str] = None) -> List[Suppression]:
    """Every suppression in force for ``user_id``, newest first (only *scope*'s
    rows when given — the goal views pass ``title``, the job views ``cron_lane``)."""
    if not _table_exists(db_path):
        return []
    ts = time.time() if now is None else float(now)
    rows = execute_retry(
        db_path,
        """SELECT * FROM goal_suppressions
            WHERE user_id=? AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at DESC""",
        (user_id, ts), fetch="all") or []
    out = [_row(r) for r in rows]
    return [s for s in out if s.scope == scope] if scope else out


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
    rows = active(db_path, user_id=user_id, now=now, scope=SCOPE_TITLE)
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
             now: Optional[float] = None) -> Optional[Suppression]:
    """The suppression that forbids creating this goal, or ``None``."""
    if not title:
        return None
    return find(db_path, user_id=user_id, scope=SCOPE_TITLE, value=str(title), now=now)


def lane_stop(db_path: str, *, user_id: str, lane: str,
              now: Optional[float] = None) -> Optional[Suppression]:
    """The owner's stop of cron lane *lane* in force, or ``None`` (no lane = none)."""
    if not str(lane or "").strip():
        return None
    return find(db_path, user_id=user_id, scope=SCOPE_CRON_LANE, value=lane, now=now)


def stopped_at_text(s: Suppression) -> str:
    """When the owner set *s*, in UTC (``2026-10-15 08:09 UTC``)."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(s.created_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def describe(s: Suppression) -> str:
    """One owner-readable line for the OFF section."""
    reason = f" — “{s.reason}”" if s.reason else ""
    return f"{s.label}{reason}"


@dataclass(frozen=True)
class StopResult:
    """What one owner stop did. ``message`` is the one owner sentence every
    seat shows (chat, REPL, CLI, console, the agent's ``goal_cancel``)."""
    ok: bool
    stopped: bool          # the row moved to ``cancelled`` by THIS call
    suppressed: bool       # the title is switched off (``--forever``)
    was_running: bool
    message: str


def stop_goal(board, *, user_id: str, goal, forever: bool = False,
              reason: str = "") -> StopResult:
    """080 D4 — the ONE stop: once by default, ``forever`` explicit, the SAME
    on every path (``/goal stop``, the REPL, ``polyrob goals stop``, the
    console Stop / Drop it, the agent's ``goal_cancel``).

    *board* is injected (core may not import ``agents.*``): it needs ``cancel``
    and ``db_path``. Once = the row is cancelled and nothing else changes, so the
    title may be created again. Forever = also switch the title OFF here, so
    ``board.create(force=True)`` refuses it until ``/goal allow``. Never raises; a failed write is named in ``message``."""
    gid = str(getattr(goal, "id", "") or "")[:8]
    title = str(getattr(goal, "title", "") or "")
    status = str(getattr(goal, "status", "") or "")
    running = status == "running"
    stopped = False
    if status not in ("done", "cancelled"):
        try:
            stopped = bool(board.cancel(goal.id, user_id=user_id))
        except Exception as exc:
            return StopResult(False, False, False, running,
                              f"Could not stop {gid} ({type(exc).__name__}) — see /goals.")
        if not stopped:
            return StopResult(False, False, False, running,
                              f"Could not stop {gid} — see /goals.")
    run_note = (" It is running right now — this run finishes; no new one starts."
                if running and stopped else "")
    if not forever:
        head = (f"{gid} stopped." if stopped else f"{gid} is already {status}.")
        return StopResult(True, stopped, False, running,
                          f"{head}{run_note} Only this goal stopped: it may be "
                          f"created again. Never again: /goal stop {gid} --forever")
    try:
        suppress(board.db_path, user_id=user_id, scope=SCOPE_TITLE, value=title,
                 label=title, reason=(reason or "").strip())
    except Exception as exc:
        return StopResult(stopped, stopped, False, running,
                          f"{gid} stopped, but I could NOT switch it off for good "
                          f"({type(exc).__name__}: {str(exc)[:80]}) — it may be "
                          f"created again.")
    head = f"{gid} stopped" if stopped else f"{gid} is already {status}"
    return StopResult(True, stopped, True, running,
                      f"{head} and switched OFF: “{title}” will not be created "
                      f"again — not by a job or by me.{run_note} "
                      f"Turn it back on: /goal allow {gid}")


__all__ = [
    "SCOPES", "SCOPE_CRON_LANE", "SCOPE_TITLE", "StopResult",
    "Suppression", "active", "allow", "blocking", "describe", "find", "lane_stop",
    "match", "stop_goal", "stopped_at_text", "stops_db_beside", "stops_db_path",
    "suppress",
]
