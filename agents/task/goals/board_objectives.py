"""Objectives: standing never-dispatched rows goals attach to — children, stream rows,
the per-objective goal budget, status and last activity.

A mixin of ``GoalBoard`` (split out of ``agents/task/goals/board.py``, W9 2026-10-10). It relies
on the host for ``db_path``, ``_now``, ``_id``, ``_event``, ``get`` and ``create``; every
method opens the SAME ``goals.db`` through ``core.sqlite_util`` exactly as before. The
tables (``goals``, ``goal_events``, ``goal_edges``) are created by ``GoalBoard._init_schema``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.sqlite_util import execute_retry
from core.goal_vocab import KIND_GOAL, KIND_OBJECTIVE
from agents.task.goals.board_types import _OBJECTIVE_STATUSES, Goal

# The board's logger name, so a log filter on agents.task.goals.board still sees these.
logger = logging.getLogger("agents.task.goals.board")


class BoardObjectivesMixin:
    """``kind='objective'`` rows and the goals parented to them."""

    def children_of(self, user_id: str, objective_id: str,
                    *, live_only: bool = True) -> List[Goal]:
        """Goals attached to *objective_id*. ``live_only`` drops cancelled/dropped
        rows — the budget counts live work, not history."""
        sql = ("SELECT * FROM goals WHERE kind=? AND user_id=? AND parent_id=?")
        params: List[Any] = [KIND_GOAL, user_id, objective_id]
        if live_only:
            sql += " AND status NOT IN ('cancelled','dropped')"
        rows = execute_retry(self.db_path, sql + " ORDER BY created_at",
                             tuple(params), fetch="all") or []
        return [Goal.from_row(r) for r in rows]

    def stream_goals(self, user_id: str, stream_id: str) -> List[Goal]:
        """Every goal row tagged for *stream_id*, tenant-scoped, filtered in SQL.

        The stream throttles (``streams.stream_live_goals`` /
        ``streams.stream_last_seeded_at``) used to derive this from
        ``list(user_id=..., limit=1000)``. That is unsafe: ``list`` ends
        ``ORDER BY priority DESC, created_at LIMIT ?``, so the rows evicted from a
        bounded window are the LOWEST-priority ones — and a manifest stream's legs
        sit BELOW the board default priority of 5 by design. Unrelated ordinary
        rows therefore push a stream's own LIVE rows out of the window first, and
        both throttles invert from "wait" to "seed again". Filtering by tag in SQL
        cannot be evicted by anything.

        TWO tags are matched, not one. ``payload.stream`` is what
        ``streams.seed_stream`` writes; ``payload.cycle`` is what the legacy
        per-stream seeder (the deleted ``seed_trading_cycle.py``, whose ``CYCLE_TAG``
        is literally ``"treasury-trading"`` — the same string as the manifest
        stream id) wrote. During the cutover BOTH seeders can be armed at once,
        and a throttle blind to the other one's tag green-lights a second,
        overlapping cycle of the same stream — for the trading stream, two
        concurrent grants of ``defi_trade``. Once the legacy seeder is deleted
        nothing writes ``payload.cycle`` and the second clause matches nothing,
        so this stays harmless rather than needing to be undone.
        """
        rows = execute_retry(
            self.db_path,
            """SELECT * FROM goals
                WHERE kind=? AND user_id=?
                  AND (json_extract(payload,'$.stream')=?
                       OR json_extract(payload,'$.cycle')=?)
                ORDER BY created_at""",
            (KIND_GOAL, user_id, stream_id, stream_id), fetch="all") or []
        return [Goal.from_row(r) for r in rows]

    def objective_budget(self, objective: Goal) -> int:
        """Live-goal cap for *objective*. 0 disables. The objective's own
        ``payload.goal_budget`` wins over the deployment default, so a
        deliberately long-running objective can state its number.

        A STREAM objective — one carrying ``payload.stream_id``, written by the
        declarative manifest seeder (``agents/task/goals/streams.py``) — is
        ALWAYS uncapped. This budget is a LIFETIME tally: ``children_of(...,
        live_only=True)`` drops only ``cancelled``/``dropped``, so every ``done``
        child counts against it forever and nothing ever sweeps them. That is
        exactly right for a BOUNDED PROJECT (it is what stops the planner opening
        "Round 9" on an objective that never finishes) and exactly wrong for a
        PERMANENT stream, whose whole contract is to re-seed the same cycle for
        as long as it stands: a 3-leg cycle under a budget of 12 jams after four
        cycles and the stream dies silently — including the trading stream, whose
        legs are the only operator-granted path to a money verb. A manifest
        stream is already throttled by its own ``max_live_goals`` +
        ``cadence_hours``, which bound CONCURRENT work rather than lifetime work
        and are a strictly better rail for standing work. A manifest
        ``objective.goal_budget`` is still recorded on the row (it takes effect
        again if the stream identity is ever removed); it is simply not enforced
        while the row IS a stream.
        """
        # 036 §3.1: a RAIL (``payload.recurrence``) is standing work too — it is
        # bounded by its own ``max_live`` + schedule, never a lifetime tally.
        # The rule lives once in core (the status snapshot reads it too).
        from core.goal_vocab import objective_goal_budget
        return objective_goal_budget(objective.payload or {})

    def _check_objective_budget(self, user_id: str, parent_id: Optional[str]) -> None:
        """Refuse a new child once an objective has spent its budget.

        An objective with no completion criterion and no cap forces the planner
        to invent the next increment forever: prod's "Explore the x402 agent
        economy" accumulated 32 children over a month and produced Rounds 1..9
        while its actual purpose (revenue) produced $0. The cap turns "keep
        going" into "come back to me" — the refusal message says so explicitly,
        because the agent's next move should be an ask, not another round.

        Fail-OPEN on any lookup error: a budget check must never be the reason a
        legitimate goal cannot be filed.
        """
        if not parent_id:
            return
        try:
            objective = self.get(parent_id)
            if objective is None or objective.kind != KIND_OBJECTIVE:
                return
            budget = self.objective_budget(objective)
            if budget <= 0:
                return
            live = len(self.children_of(user_id, parent_id))
        except Exception:
            logger.debug("objective budget check skipped for %s", parent_id, exc_info=True)
            return
        if live >= budget:
            raise ValueError(
                f"objective {parent_id} has spent its goal budget ({live}/{budget} live "
                f"goals). Do NOT open another round on it. Either finish or cancel the "
                f"open goals, or raise an ask explaining what decision you need from the "
                f"owner to complete the objective.")

    def objectives(self, *, user_id: str, status: Optional[str] = None) -> List[Goal]:
        sql = "SELECT * FROM goals WHERE kind=? AND user_id=?"
        params: List[Any] = [KIND_OBJECTIVE, user_id]
        if status is not None:
            sql += " AND status=?"; params.append(status)
        sql += " ORDER BY priority DESC, created_at"
        rows = execute_retry(self.db_path, sql, tuple(params), fetch="all") or []
        return [Goal.from_row(r) for r in rows]

    def set_objective_status(self, objective_id: str, status: str,
                             *, user_id: Optional[str] = None) -> bool:
        if status not in _OBJECTIVE_STATUSES:
            raise ValueError(f"invalid objective status {status!r} (use {sorted(_OBJECTIVE_STATUSES)})")
        sql = "UPDATE goals SET status=? WHERE id=? AND kind=?"
        params: tuple = (status, objective_id, KIND_OBJECTIVE)
        if user_id is not None:
            sql += " AND user_id=?"; params = params + (user_id,)
        rc = execute_retry(self.db_path, sql, params)
        if rc == 1:
            self._event(objective_id, f"objective_{status}", {})
        return rc == 1

    def children(self, parent_id: str, *, user_id: Optional[str] = None) -> List[Goal]:
        """T2.1 Task 5 hardening: ``user_id`` is an OPTIONAL tenant filter,
        default ``None`` (today's cross-tenant listing, unchanged for existing
        callers). Passing it restricts results to that tenant's own children —
        `children()` was not self-defending (a caller could enumerate another
        tenant's sub-goals by a known parent id)."""
        sql = "SELECT * FROM goals WHERE parent_id=?"
        params: List[Any] = [parent_id]
        if user_id is not None:
            sql += " AND user_id=?"
            params.append(user_id)
        sql += " ORDER BY created_at"
        rows = execute_retry(self.db_path, sql, tuple(params), fetch="all") or []
        return [Goal.from_row(r) for r in rows]

    def objective_last_activity(self, user_id: str) -> Dict[str, float]:
        """Newest child-goal activity per objective, as a unix timestamp.

        "Activity" is ``started_at`` when the child has run and ``created_at`` when
        it has not — a queued-but-never-dispatched child still means the planner has
        already served that objective. Cancelled and dropped children do not count.
        An objective with no live child is ABSENT from the map; the caller reads
        absent as 0.0, which sorts it to the front of the starvation order.
        """
        rows = execute_retry(
            self.db_path,
            """SELECT parent_id AS k, MAX(COALESCE(started_at, created_at)) AS t
                 FROM goals
                WHERE kind='goal' AND user_id=? AND parent_id IS NOT NULL
                  AND status NOT IN ('cancelled','dropped')
                GROUP BY parent_id""",
            (user_id,),
            fetch="all",
        ) or []
        out: Dict[str, float] = {}
        for r in rows:
            try:
                key, t = r["k"], r["t"]
            except (KeyError, TypeError, IndexError):
                key, t = r[0], r[1]
            if key and t is not None:
                out[str(key)] = float(t)
        return out
