"""Goal dependency graph (DAG, T2.1): validate/add edges, cycle check, the completion sweep
and the dep-failed cascade.

A mixin of ``GoalBoard`` (split out of ``agents/task/goals/board.py``, W9 2026-10-10). It relies
on the host for ``db_path``, ``_now``, ``_id``, ``_event``, ``get`` and ``create``; every
method opens the SAME ``goals.db`` through ``core.sqlite_util`` exactly as before. The
tables (``goals``, ``goal_events``, ``goal_edges``) are created by ``GoalBoard._init_schema``.
"""
from __future__ import annotations

import json
import logging
from typing import List

from core.sqlite_util import execute_retry
from core.goal_vocab import STATUS_WAITING, STATUS_BLOCKED, KIND_GOAL
from agents.task.goals.board_types import Goal

# The board's logger name, so a log filter on agents.task.goals.board still sees these.
logger = logging.getLogger("agents.task.goals.board")


class BoardDepsMixin:
    """``goal_edges`` reads/writes and the waiting->ready transitions they drive."""

    # --- dependency graph (DAG, T2.1 Task 1) ----------------------------------

    def _validate_dep_ids(self, dep_ids: List[str], *, user_id: str) -> List[Goal]:
        """Resolve dep ids to Goal rows, enforcing existence + same-tenant +
        ``kind='goal'``.

        Raises ValueError on the first bad id — an all-or-nothing validation
        pass (a partially-validated dep set is worse than none). ``get()`` is
        NOT tenant-scoped, so every dep is re-checked against ``user_id`` here.
        The kind check matters beyond "edges are goal->goal by design":
        ``OBJ_DONE == STATUS_DONE == "done"`` (same literal), so an objective
        (or an ask, which never reaches a real 'done' status either) could
        otherwise silently satisfy a dependency it was never meant to.
        """
        deps: List[Goal] = []
        for dep_id in dep_ids:
            dep = self.get(dep_id)
            if dep is None:
                raise ValueError(f"depends_on: unknown goal id {dep_id!r}")
            if dep.user_id != user_id:
                raise ValueError(f"depends_on: goal {dep_id!r} belongs to another tenant")
            if dep.kind != KIND_GOAL:
                raise ValueError(
                    f"depends_on: {dep_id!r} is a {dep.kind!r}, not a goal — "
                    "dependency edges must target kind='goal'")
            deps.append(dep)
        return deps

    def _would_close_cycle(self, goal_id: str, dep_id: str, *, max_depth: int = 50) -> bool:
        """True if adding the edge ``goal_id -> dep_id`` ("goal_id depends on
        dep_id") would close a cycle — i.e. ``dep_id`` already transitively
        depends on ``goal_id``.

        Plain BFS over the existing ``goal_edges`` graph, depth-capped (the
        board is small; the cap guards against a pathological/corrupt edge
        set looping forever rather than raising).
        """
        frontier = {dep_id}
        visited: set = set()
        depth = 0
        while frontier and depth < max_depth:
            if goal_id in frontier:
                return True
            depth += 1
            next_frontier: set = set()
            for node in frontier:
                if node in visited:
                    continue
                visited.add(node)
                rows = execute_retry(
                    self.db_path,
                    "SELECT depends_on_id FROM goal_edges WHERE goal_id=?",
                    (node,), fetch="all",
                ) or []
                next_frontier.update(r["depends_on_id"] for r in rows)
            frontier = next_frontier
        return goal_id in frontier

    def add_dependencies(self, goal_id: str, dep_ids: List[str], *, user_id: str) -> None:
        """Record that ``goal_id`` depends on each id in ``dep_ids`` (idempotent
        per pair — ``INSERT OR IGNORE``).

        All-or-nothing: every dep is validated to exist and belong to the SAME
        tenant, and every proposed edge is checked against the existing graph
        for a self-dependency or a cycle — ALL before any edge is written.
        Already-``done`` deps are recorded too (not skipped): ``deps_satisfied``
        is a live join against current goal status, never a cached snapshot,
        so a done dep is simply always-satisfied going forward.
        """
        ids = list(dict.fromkeys(dep_ids or []))
        if not ids:
            return
        for dep_id in ids:
            if dep_id == goal_id:
                raise ValueError(f"goal {goal_id!r} cannot depend on itself")
        self._validate_dep_ids(ids, user_id=user_id)
        for dep_id in ids:
            if self._would_close_cycle(goal_id, dep_id):
                raise ValueError(
                    f"depends_on: goal {dep_id!r} would create a dependency cycle "
                    f"with {goal_id!r}")
        now = self._now()
        inserted: List[str] = []
        for dep_id in ids:
            rc = execute_retry(
                self.db_path,
                """INSERT OR IGNORE INTO goal_edges (goal_id, depends_on_id, user_id, created_at)
                    VALUES (?, ?, ?, ?)""",
                (goal_id, dep_id, user_id, now),
            )
            if rc == 1:
                inserted.append(dep_id)
        # TOCTOU repair (2026-07-23 validation): the pre-insert cycle check is
        # check-then-write, so two concurrent add_dependencies (A→B and B→A)
        # can both pass it — and a formed cycle strands both rows forever
        # (ready() excludes them, reconcile_waiting reads a waiting dep as
        # live, no janitor detects cycles). Re-verify AFTER our edges are
        # visible: the racer whose insert commits last sees the full graph, so
        # at least one racer detects the cycle, removes ITS OWN just-inserted
        # edges (never pre-existing ones), and raises — a cycle can never
        # persist past the losing caller's return.
        for dep_id in ids:
            if self._would_close_cycle(goal_id, dep_id):
                for d in inserted:
                    execute_retry(
                        self.db_path,
                        "DELETE FROM goal_edges WHERE goal_id=? AND depends_on_id=?",
                        (goal_id, d),
                    )
                raise ValueError(
                    f"depends_on: goal {dep_id!r} would create a dependency cycle "
                    f"with {goal_id!r} (detected post-insert)")
        # Edge-only writes are otherwise invisible to the wake-gate fingerprint
        # (MAX(goal_events.id)) — every mutation path must emit an event.
        self._event(goal_id, "deps_added", {"deps": ids})

    def dependencies(self, goal_id: str) -> List[str]:
        """Raw prerequisite ids for ``goal_id`` (edge list, no status join)."""
        rows = execute_retry(
            self.db_path,
            "SELECT depends_on_id FROM goal_edges WHERE goal_id=? ORDER BY created_at",
            (goal_id,), fetch="all",
        ) or []
        return [r["depends_on_id"] for r in rows]

    def dependents(self, goal_id: str) -> List[str]:
        """Ids of goals that list ``goal_id`` in their ``depends_on`` (reverse
        edges — i.e. what ``goal_id`` blocks). Public read-only wrapper over
        :meth:`_dependents_of` for callers outside this module (e.g. `goal_show`)."""
        return self._dependents_of(goal_id)

    def deps_satisfied(self, goal_id: str) -> bool:
        """True iff every prerequisite edge for ``goal_id`` currently points to
        a ``done`` goal. Always a FRESH read against ``goals.status`` — never
        cached — so callers (e.g. the completion sweep) can re-verify at write
        time rather than trust a stale snapshot."""
        row = execute_retry(
            self.db_path,
            """SELECT COUNT(*) AS n FROM goal_edges e
                JOIN goals g ON g.id = e.depends_on_id
                WHERE e.goal_id=? AND g.status != 'done'""",
            (goal_id,), fetch="one",
        )
        if not row:
            return True
        try:
            return int(row["n"]) == 0
        except (KeyError, TypeError, IndexError):
            return int(row[0]) == 0

    def _dependents_of(self, prerequisite_id: str) -> List[str]:
        """Ids of goals that list ``prerequisite_id`` in their ``depends_on``."""
        rows = execute_retry(
            self.db_path,
            "SELECT DISTINCT goal_id FROM goal_edges WHERE depends_on_id=?",
            (prerequisite_id,), fetch="all",
        ) or []
        return [r["goal_id"] for r in rows]

    def _sweep_dependents_on_completion(self, completed_goal_id: str) -> None:
        """T2.1 Task 2 completion sweep: called ONLY after ``completed_goal_id``'s
        own CAS to 'done' has already won (never on a ``stale_completion``).

        For each dependent edge, ``deps_satisfied`` is re-verified FRESH (never
        trusting a cached snapshot — a sibling prerequisite may complete between
        the check and the write) and the flip is itself a CAS
        (``WHERE status='waiting'``). Two concurrent completions of sibling
        prerequisites (e.g. via two ``GoalBoard`` instances on one db path) can
        both observe ``deps_satisfied() == True`` and both attempt the flip —
        exactly one wins (rc=1, emits the event); the other's rc=0 is harmless
        (the dependent is already ``ready``).
        """
        for dep_id in self._dependents_of(completed_goal_id):
            if not self.deps_satisfied(dep_id):
                continue
            rc = execute_retry(
                self.db_path,
                "UPDATE goals SET status='ready' WHERE id=? AND kind='goal' AND status='waiting'",
                (dep_id,),
            )
            if rc == 1:
                self._event(dep_id, "deps_satisfied", {"completed": completed_goal_id})
                continue
            # T2.1 final-review Fix 2: a dependent that was EARLIER cascaded to
            # blocked/dep_failed (a sibling prerequisite that broke the
            # breaker, or was cancelled) is still DAG-revivable once every
            # prerequisite lands 'done' — e.g. the breaker-blocked sibling is
            # owner-unblocked and later completes for real. The plan contract
            # says dep_failed is "owner/DAG-mediated"; without this the DAG
            # half was missing and a revivable dep_failed row needed a SECOND
            # manual owner unblock even after its blocker resolved itself.
            # Read-merge-write to clear block_kind, then re-verify the same
            # CAS condition at write time (dep_failed is exclusively
            # DAG-produced, so clearing it here can never clobber a more
            # specific human-facing kind).
            dep = self.get(dep_id)
            if dep is None or dep.status != STATUS_BLOCKED:
                continue
            if (dep.payload or {}).get("block_kind") != "dep_failed":
                continue
            payload = dict(dep.payload or {})
            payload.pop("block_kind", None)
            rc2 = execute_retry(
                self.db_path,
                """UPDATE goals SET status='ready', payload=?
                    WHERE id=? AND kind='goal' AND status='blocked'
                      AND COALESCE(json_extract(payload, '$.block_kind'), '') = 'dep_failed'""",
                (json.dumps(payload), dep_id),
            )
            if rc2 == 1:
                self._event(dep_id, "deps_satisfied", {"completed": completed_goal_id})

    def _cascade_dep_failed(self, prerequisite_id: str) -> None:
        """T2.1 Task 2 inverse cascade: called ONLY after ``prerequisite_id``'s
        own CAS to a terminal-bad status (cancelled, or blocked via a breaker
        trip) has already won.

        A ``waiting`` dependent can never satisfy a dead prerequisite (it will
        never reach 'done'), so it is flipped ``waiting -> blocked`` (CAS) with
        ``payload.block_kind='dep_failed'`` — visible and owner-unblockable
        (see :meth:`unblock`) rather than sitting in ``waiting`` forever.
        """
        now = self._now()
        for dep_id in self._dependents_of(prerequisite_id):
            dep = self.get(dep_id)
            if dep is None or dep.status != STATUS_WAITING:
                continue
            payload = dict(dep.payload or {})
            payload["block_kind"] = "dep_failed"
            rc = execute_retry(
                self.db_path,
                """UPDATE goals SET status='blocked', payload=?, completed_at=?
                    WHERE id=? AND kind='goal' AND status='waiting'""",
                (json.dumps(payload), now, dep_id),
            )
            if rc == 1:
                self._event(dep_id, "dep_failed", {"prerequisite": prerequisite_id})
