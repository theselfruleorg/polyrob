"""Planner bookkeeping: run marks, outcomes, empty/stall streaks and the durable stall
escalation marker, all as ``goal_events`` rows under the tenant-scoped planner key.

A mixin of ``GoalBoard`` (split out of ``agents/task/goals/board.py``, W9 2026-10-10). It relies
on the host for ``db_path``, ``_now``, ``_id``, ``_event``, ``get`` and ``create``; every
method opens the SAME ``goals.db`` through ``core.sqlite_util`` exactly as before. The
tables (``goals``, ``goal_events``, ``goal_edges``) are created by ``GoalBoard._init_schema``.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from core.sqlite_util import execute_retry

# The board's logger name, so a log filter on agents.task.goals.board still sees these.
logger = logging.getLogger("agents.task.goals.board")


class BoardPlannerMixin:
    """Planner/stall state kept as ``goal_events`` under ``__planner__[:<user_id>]``."""

    PLANNER_SENTINEL = "__planner__"

    # --- planner bookkeeping keys (FIX 5: tenant-scoped) ---------------------
    # Every planner row used to key on the bare PLANNER_SENTINEL with no user_id
    # filter — unlike every other query in this file. On a multi-tenant board one
    # tenant's planner run reset EVERY tenant's backoff and its escalation dedup
    # suppressed another tenant's stall ask. Rows are now written under
    # "__planner__:<user_id>"; READS cover that key AND the bare legacy key, so an
    # upgrade neither crashes on the old rows nor silently resets a live streak to
    # zero (which would re-arm the very escalation storm the durable marker
    # exists to stop). Legacy rows can only LENGTHEN a streak — the safe
    # direction: more backoff, stickier dedup, never a new push.

    def _planner_key(self, user_id: Optional[str]) -> str:
        return f"{self.PLANNER_SENTINEL}:{user_id}" if user_id else self.PLANNER_SENTINEL

    def _planner_read_keys(self, user_id: Optional[str]) -> tuple:
        key = self._planner_key(user_id)
        return (key,) if key == self.PLANNER_SENTINEL else (key, self.PLANNER_SENTINEL)

    def last_planner_run_at(self, user_id: Optional[str] = None) -> Optional[float]:
        keys = self._planner_read_keys(user_id)
        placeholders = ",".join("?" for _ in keys)
        row = execute_retry(
            self.db_path,
            f"SELECT MAX(created_at) AS t FROM goal_events "
            f"WHERE goal_id IN ({placeholders}) AND kind='planner_run'",
            keys, fetch="one",
        )
        try:
            t = row["t"] if row else None
        except (KeyError, TypeError, IndexError):
            t = row[0] if row else None
        return float(t) if t else None

    def mark_planner_run(self, user_id: Optional[str] = None) -> None:
        self._event(self._planner_key(user_id), "planner_run", {})

    # --- planner outcome + stall escalation (durable) ------------------------
    # The dispatcher used to keep "consecutive empty planner runs" and "already
    # escalated this stall" as instance attributes. Prod 2026-08-29: the service
    # was restarted 14 times in 36 h (maintenance-loop 2-file deploys) and every
    # restart re-armed the once-per-stall owner push. These live in goal_events
    # under the planner sentinel, so a restart changes nothing.

    def mark_planner_outcome(self, *, queued: int, live: int = 0,
                             user_id: Optional[str] = None) -> None:
        """Record a planner run: how many goals it queued, and how many goals
        were still in flight on the board afterwards (``live``). Backoff keys on
        ``queued`` alone (the planner is not producing); the stall streak needs
        BOTH to be zero (nothing queued AND nothing running/waiting/ready)."""
        self._event(self._planner_key(user_id), "planner_outcome",
                    {"queued": int(max(0, queued)), "live": int(max(0, live))})

    def _planner_outcomes_desc(self, limit: int = 50,
                               user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        keys = self._planner_read_keys(user_id)
        placeholders = ",".join("?" for _ in keys)
        rows = execute_retry(
            self.db_path,
            f"SELECT payload, created_at FROM goal_events WHERE goal_id IN ({placeholders}) "
            f"AND kind='planner_outcome' ORDER BY id DESC LIMIT ?",
            (*keys, int(limit)), fetch="all") or []
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload"] or "{}")
                ts = float(r["created_at"])
            except (KeyError, TypeError, IndexError, ValueError):
                payload = json.loads(r[0] or "{}")
                ts = float(r[1])
            out.append({"queued": int(payload.get("queued") or 0),
                        "live": int(payload.get("live") or 0), "ts": ts})
        return out

    def consecutive_empty_planner_runs(self, user_id: Optional[str] = None) -> int:
        """Length of the trailing run of planner outcomes that queued nothing."""
        n = 0
        for o in self._planner_outcomes_desc(user_id=user_id):
            if o["queued"] > 0:
                break
            n += 1
        return n

    def consecutive_stall_runs(self, user_id: Optional[str] = None) -> int:
        """Trailing planner runs that queued nothing AND left no live goal."""
        n = 0
        for o in self._planner_outcomes_desc(user_id=user_id):
            if o["queued"] > 0 or o["live"] > 0:
                break
            n += 1
        return n

    def empty_streak_started_at(self, user_id: Optional[str] = None) -> Optional[float]:
        """Timestamp of the FIRST run of the current STALL streak (queued==0 and
        live==0), or ``None`` when the latest run queued something or left work
        on the board (no streak)."""
        started: Optional[float] = None
        for o in self._planner_outcomes_desc(user_id=user_id):
            if o["queued"] > 0 or o["live"] > 0:
                break
            started = o["ts"]
        return started

    def mark_stall_escalated(self, user_id: Optional[str] = None) -> None:
        """Durable "the owner was told about this stall" marker."""
        self._event(self._planner_key(user_id), "empty_pipeline_escalated", {})

    def stall_escalation_count(self, user_id: Optional[str] = None) -> int:
        """How many stall escalations were ever recorded for this tenant. After
        :meth:`mark_stall_escalated` it is the ordinal of the current stall — the
        honest fact that makes two stalls' owner messages distinct."""
        keys = self._planner_read_keys(user_id)
        placeholders = ",".join("?" for _ in keys)
        row = execute_retry(
            self.db_path,
            f"SELECT COUNT(*) AS n FROM goal_events WHERE goal_id IN ({placeholders}) "
            f"AND kind='empty_pipeline_escalated'",
            (*keys,), fetch="one")
        if row is None:
            return 0
        try:
            return int(row["n"])
        except (KeyError, TypeError, IndexError):
            return int(row[0])

    def stall_escalated_since(self, since: Optional[float],
                              user_id: Optional[str] = None) -> bool:
        """True if a stall escalation was recorded at or after *since* (the start
        of the current empty streak). ``since=None`` (no streak) is False."""
        if since is None:
            return False
        keys = self._planner_read_keys(user_id)
        placeholders = ",".join("?" for _ in keys)
        row = execute_retry(
            self.db_path,
            f"SELECT 1 AS x FROM goal_events WHERE goal_id IN ({placeholders}) "
            f"AND kind='empty_pipeline_escalated' AND created_at>=? LIMIT 1",
            (*keys, float(since)), fetch="one")
        return row is not None
