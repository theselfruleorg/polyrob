"""Rail proposals — consent-first offers of standing work (036 §4.3).

Nothing here creates a rail. A proposal is a row the owner accepts with one verb
(``/rail accept <key>``) or dismisses (``/rail dismiss <key>``) — and a dismissal is
LATCHED by key: the same key is never offered again. That is the owner's
"cancel means NEVER AGAIN", placed on the recurring thing.

Producers today: the agent (``rail_propose`` on the goal tool). The ``source``
column is how a later producer (a skill declaring a rail, the planner's empty
pipeline, ``polyrob init``) joins without a second store; per the owner's 036 Q3
decision such a producer may only PROPOSE, never apply.

A proposal can NAME a tool it would need (``needs``), rendered ``needs grant:
<tool>``; it can never carry one. Accepting creates the rail on the ordinary
toolset — the grant is the separate ``/rail grant`` act.

``MAX_PENDING`` open proposals per tenant, so the list never becomes a nag wall.
Store: ``rail_proposals`` in ``goals.db``; a read never creates the table.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.sqlite_util import execute_retry, init_schema

MAX_PENDING = 5
PENDING, ACCEPTED, DISMISSED = "pending", "accepted", "dismissed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rail_proposals (
    user_id     TEXT NOT NULL,
    key         TEXT NOT NULL,
    source      TEXT NOT NULL,              -- 'agent' | 'skill:<id>' | 'planner' | 'init'
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    recurrence  TEXT NOT NULL,              -- JSON, validated, never carries tools
    needs       TEXT NOT NULL DEFAULT '[]', -- JSON list of tool ids it would need
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  REAL NOT NULL,
    decided_at  REAL,
    PRIMARY KEY (user_id, key)
);
"""


@dataclass
class Proposal:
    user_id: str
    key: str
    source: str
    title: str
    body: str
    recurrence: Dict[str, Any]
    needs: List[str] = field(default_factory=list)
    status: str = PENDING
    created_at: float = 0.0


def _exists(db_path: str) -> bool:
    if not db_path or not os.path.exists(db_path):
        return False
    return execute_retry(
        db_path, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rail_proposals'",
        (), fetch="one") is not None


def _row(r) -> Proposal:
    return Proposal(user_id=r["user_id"], key=r["key"], source=r["source"], title=r["title"],
                    body=r["body"] or "", recurrence=json.loads(r["recurrence"] or "{}"),
                    needs=json.loads(r["needs"] or "[]"), status=r["status"],
                    created_at=float(r["created_at"] or 0))


def get(db_path: str, *, user_id: str, key: str) -> Optional[Proposal]:
    if not _exists(db_path):
        return None
    r = execute_retry(db_path, "SELECT * FROM rail_proposals WHERE user_id=? AND key=?",
                      (user_id, key), fetch="one")
    return _row(r) if r is not None else None


def pending(db_path: str, *, user_id: str) -> List[Proposal]:
    if not _exists(db_path):
        return []
    rows = execute_retry(
        db_path, "SELECT * FROM rail_proposals WHERE user_id=? AND status=? "
                 "ORDER BY created_at", (user_id, PENDING), fetch="all") or []
    return [_row(r) for r in rows]


def propose(db_path: str, *, user_id: str, key: str, source: str, title: str,
            body: str = "", recurrence: Dict[str, Any], needs: Optional[List[str]] = None,
            now: Optional[float] = None) -> Proposal:
    """File one proposal. Raises ``ValueError`` when the key was dismissed (latched),
    is already pending/accepted, or the tenant already has ``MAX_PENDING`` open."""
    from agents.task.goals.rails import slug, validate_recurrence
    from core.tool_grants import grantable_tool
    if not user_id:
        raise ValueError("a proposal needs a tenant")
    k = slug(key or title)
    if not k:
        raise ValueError("a proposal needs a key or a title")
    rec = validate_recurrence(dict(recurrence or {}, name=k))
    named = [str(t).strip().lower() for t in (needs or []) if str(t or "").strip()]
    bad = [t for t in named if not grantable_tool(t)]
    if bad:
        raise ValueError(f"unknown tool(s) {', '.join(bad)} in needs")
    prior = get(db_path, user_id=user_id, key=k)
    if prior is not None:
        if prior.status == DISMISSED:
            raise ValueError(f"the owner dismissed '{k}' — it is never offered again")
        raise ValueError(f"'{k}' is already {prior.status}")
    if len(pending(db_path, user_id=user_id)) >= MAX_PENDING:
        raise ValueError(f"{MAX_PENDING} proposals are already waiting on the owner — "
                         f"do not add another until one is decided")
    init_schema(db_path, _SCHEMA)
    ts = time.time() if now is None else float(now)
    execute_retry(
        db_path,
        """INSERT INTO rail_proposals (user_id, key, source, title, body, recurrence, needs,
                                       status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (user_id, k, source[:64], title[:200], (body or "")[:4000], json.dumps(rec),
         json.dumps(named), PENDING, ts))
    return Proposal(user_id, k, source, title[:200], body or "", rec, named, PENDING, ts)


def decide(db_path: str, *, user_id: str, key: str, accept: bool) -> Optional[Proposal]:
    """Mark a pending proposal accepted or dismissed (the latch). ``None`` if not pending."""
    p = get(db_path, user_id=user_id, key=key)
    if p is None or p.status != PENDING:
        return None
    rc = execute_retry(
        db_path, "UPDATE rail_proposals SET status=?, decided_at=? "
                 "WHERE user_id=? AND key=? AND status=?",
        (ACCEPTED if accept else DISMISSED, time.time(), user_id, p.key, PENDING))
    if rc != 1:
        return None
    p.status = ACCEPTED if accept else DISMISSED
    return p


__all__ = ["ACCEPTED", "DISMISSED", "MAX_PENDING", "PENDING", "Proposal", "decide", "get",
           "pending", "propose"]
