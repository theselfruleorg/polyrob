"""Goal-board value types and pure helpers (split out of ``board.py``, W9 2026-10-10).

``agents.task.goals.board`` re-exports every name here; import from there.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.goal_vocab import (
    STATUS_READY, KIND_GOAL, OBJ_ACTIVE, OBJ_PAUSED, OBJ_DROPPED, OBJ_DONE,
)

#: How many ready rows one fair-dispatch pass considers. The dispatcher asks for
#: 2-4 slots against a board of a few hundred rows, so scan a bounded window
#: rather than the table. A stream whose goals all sit below this window is one
#: whose neighbours are already saturated — the next tick sees it.
READY_SCAN_LIMIT = 200

# kind: rows are goals (dispatchable), objectives (standing, never dispatched),
# or asks (owner-facing needs, never dispatched)
# ask lifecycle (§7.2b) — disjoint from goal statuses so nothing dispatches them
# Task 9 (G-2): a tool_approval ask's owner-declined outcome. Disjoint from the
# goal-status STATUS_CANCELLED string on purpose — an ask is never a goal.
# 2026-08-18: the ask answered itself — every goal it blocked reached a terminal
# state, so the owner no longer has a decision to make. Distinct from FULFILLED
# on purpose: "fulfilled" claims the owner acted, and an ask that expired because
# the work resolved itself must never be recorded as an owner decision. Prod held
# 44 open asks (oldest a month) with several naming goals that had since
# succeeded — that queue is what the owner has to read.

# objective lifecycle (disjoint from goal statuses so nothing dispatches them)
_OBJECTIVE_STATUSES = {OBJ_ACTIVE, OBJ_PAUSED, OBJ_DROPPED, OBJ_DONE}


class DuplicateGoalError(ValueError):
    """A new goal's title is a near-duplicate of a recent goal."""

    def __init__(self, match_id: str, match_title: str, similarity: float):
        self.match_id = match_id
        self.match_title = match_title
        self.similarity = similarity
        super().__init__(
            f"near-duplicate of goal {match_id} '{match_title}' (similarity {similarity:.2f})")


from core.goal_vocab import normalize_title  # noqa: E402,F401 — the ONE title key


class SuppressedGoalError(ValueError):
    """The owner switched this goal OFF (034 §3.5 — ``/goal cancel``).

    Raised by ``create`` BEFORE the dedup check, and ``force=True`` does not
    bypass it: the stream seeder forces every create, and the owner's "never
    again" must beat the seeder."""

    def __init__(self, suppression):
        self.suppression = suppression
        what = {"stream": "stream ", "objective": "objective "}.get(
            getattr(suppression, "scope", ""), "")
        label = getattr(suppression, "label", "") or getattr(suppression, "key", "")
        super().__init__(
            f"the owner switched {what}'{label}' OFF — do not create it again or a "
            f"variant of it. Only the owner turns it back on (/goal allow).")


def _trigrams(s: str) -> set:
    padded = f"  {s} "
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


def title_similarity(a: str, b: str) -> float:
    ta, tb = _trigrams(normalize_title(a)), _trigrams(normalize_title(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


#: Payload flag on the dispatcher's automatic "Unblock goal: …" ask, so an
#: agent-filed ask about the same goal can replace it (see ``create_ask``).
ASK_AUTO_UNBLOCK = "auto_unblock"


def _ask_goal_keys(blocks: List[str], payload: Optional[Dict[str, Any]]) -> set:
    """The goal ids an ask is about: what it blocks, plus the goal rail it came from."""
    keys = {str(b) for b in (blocks or []) if b}
    rail = str((payload or {}).get("rail_id") or "")
    if rail.startswith("goal:") and len(rail) > 5:
        keys.add(rail[5:])
    return keys


@dataclass
class Goal:
    id: str
    user_id: str
    title: str
    body: str = ""
    kind: str = KIND_GOAL
    status: str = STATUS_READY
    priority: int = 5
    parent_id: Optional[str] = None
    claim_lock: Optional[str] = None
    claim_token: Optional[str] = None
    claim_expires: Optional[float] = None
    consecutive_failures: int = 0
    max_retries: int = 2
    last_failure_error: Optional[str] = None
    session_id: Optional[str] = None
    result: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    last_heartbeat_at: Optional[float] = None

    @classmethod
    def from_row(cls, row) -> "Goal":
        d = dict(row)
        d["payload"] = json.loads(d.get("payload") or "{}")
        return cls(**d)
