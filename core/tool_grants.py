"""Who may put a tool on a goal — ONE predicate and the rail grant store (036 §3.2/§3.3).

``payload.tools`` is consumed VERBATIM at dispatch (``dispatcher._resolve_tools``)
and at the cron read (``cron/runner.py::resolve_cron_tools``). Until 036 every
production writer decided for itself whether that was allowed. This module is the
one place the decision lives:

* :func:`assert_grantable` — called by ``GoalBoard.create`` (the choke point every
  writer passes through) and by the cron payload read. It refuses a GATED tool id
  (``money`` | ``high_impact`` | ``exec``, derived from
  ``core/tool_capabilities.py`` — never a hand list, so a new money tool is
  covered on the day it is registered) unless the actor may carry it:

  ================  =========================================================
  actor             may carry a gated id when …
  ================  =========================================================
  ``owner_seat``    always — the owner asking from an authenticated seat IS the
                    operator grant (``/trade``, ``polyrob goals create
                    --tools``, a genuine owner chat turn).
  ``agent``         the id is inside the caller's ``ceiling``
                    (``allowed_self_goal_tools()``) — the agent never
                    self-grants past it.
  ``rail``          the id is in the rail's base toolset (``ceiling``) OR a
                    ``rail_grants`` row covers ``(user, rail, tool)``. A MONEY
                    id additionally needs the ``armed`` money regime
                    (``core/config_policy/money_regime.py``): a rail seeds
                    unattended work, and unattended money is exactly what the
                    regime decides.
  ``none``          never — a writer that cannot carry tools (asks, objectives).
  ================  =========================================================

  An UNGATED id always passes. ``actor=None`` is the legacy/test path and passes
  unchanged; ``tests/test_board_create_grant_ratchet.py`` pins that every
  production ``board.create(`` call site names its actor, which is what turns the
  convention into a boundary.

* The ``rail_grants`` table (``goals.db``) — written only by
  :func:`grant_rail_tool`, which the owner verbs call (``/rail grant … confirm``,
  ``polyrob rails grant``). No agent tool reaches it; ``goals.db`` is not an agent
  file surface. Read only at seed time (``agents/task/goals/rails.py``).
  Revocation is immediate and needs no ceremony (035's asymmetry: a rule that
  removes a permission may apply at once; one that grants may not).

  A grant is never wider than the regime: a money grant written while the regime
  is not ``armed`` is stored but INERT — the seeder drops it and says why — so
  turning the regime down takes the tool away from the next seed without a second
  act. A grant can name only a classified tool id.

A READ never creates the table (an absent table reads as "no grants"); only a
write creates it (``core.sqlite_util.init_schema``).

Pure core: stdlib + ``core.sqlite_util`` + ``core.tool_capabilities``.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from core.event_kinds import RAIL_GRANT, RAIL_REVOKE
from core.sqlite_util import execute_retry, init_schema
from core.tool_capabilities import TOOL_CAPABILITIES, ids_with

ACTOR_OWNER = "owner_seat"
ACTOR_AGENT = "agent"
ACTOR_RAIL = "rail"
ACTOR_NONE = "none"
ACTORS = (ACTOR_OWNER, ACTOR_AGENT, ACTOR_RAIL, ACTOR_NONE)

#: ``tool_manage`` is classified but aspirational (never registrable), so a grant
#: naming it would be a promise nothing keeps.
_UNGRANTABLE = frozenset({"tool_manage"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rail_grants (
    user_id    TEXT NOT NULL,
    rail_id    TEXT NOT NULL,            -- the objective id
    tool_id    TEXT NOT NULL,
    granted_by TEXT NOT NULL,            -- owner principal + seat
    granted_at REAL NOT NULL,
    note       TEXT,
    pending    INTEGER NOT NULL DEFAULT 0,   -- 1 = imported, awaiting the owner's confirm
    PRIMARY KEY (user_id, rail_id, tool_id)
);
"""


class GrantRefused(ValueError):
    """A writer tried to put a gated tool on a goal it may not grant."""

    def __init__(self, tools: Sequence[str], actor: Optional[str], why: str):
        self.tools = list(tools)
        self.actor = actor
        self.why = why
        super().__init__(
            f"{', '.join(self.tools)} NOT granted ({why}). Only the owner grants a "
            f"money, host or high-impact tool — from an owner seat, or on a rail "
            f"with /rail grant.")


@dataclass(frozen=True)
class RailGrant:
    user_id: str
    rail_id: str
    tool_id: str
    granted_by: str
    granted_at: float
    note: str = ""
    pending: bool = False


def gated_ids() -> frozenset:
    """The ids a writer may not put on a goal without authority."""
    return ids_with("money") | ids_with("high_impact") | ids_with("exec")


def is_money(tool_id: str) -> bool:
    return tool_id in ids_with("money")


def grantable_tool(tool_id: str) -> bool:
    """A rail grant may name a classified, registrable tool id only."""
    return tool_id in TOOL_CAPABILITIES and tool_id not in _UNGRANTABLE


def money_armed() -> bool:
    """The live money regime is ``armed``. Fail-closed."""
    try:
        from core.config_policy.money_regime import autonomous_money_armed
        return autonomous_money_armed()
    except Exception:
        return False


def _table_exists(db_path: Optional[str]) -> bool:
    if not db_path or not os.path.exists(db_path):
        return False
    row = execute_retry(
        db_path, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rail_grants'",
        (), fetch="one")
    return row is not None


def _row(r) -> RailGrant:
    return RailGrant(user_id=r["user_id"], rail_id=r["rail_id"], tool_id=r["tool_id"],
                     granted_by=r["granted_by"] or "", granted_at=float(r["granted_at"] or 0),
                     note=r["note"] or "", pending=bool(r["pending"]))


def grants_for(db_path: Optional[str], *, user_id: str, rail_id: str,
               include_pending: bool = False) -> List[RailGrant]:
    """Every grant on one rail (active only unless *include_pending*)."""
    if not _table_exists(db_path):
        return []
    sql = "SELECT * FROM rail_grants WHERE user_id=? AND rail_id=?"
    if not include_pending:
        sql += " AND pending=0"
    rows = execute_retry(db_path, sql + " ORDER BY granted_at", (user_id, rail_id),
                         fetch="all") or []
    return [_row(r) for r in rows]


def all_grants(db_path: Optional[str], *, user_id: str) -> List[RailGrant]:
    """Every grant (active and pending) for one tenant, newest first."""
    if not _table_exists(db_path):
        return []
    rows = execute_retry(
        db_path, "SELECT * FROM rail_grants WHERE user_id=? ORDER BY granted_at DESC",
        (user_id,), fetch="all") or []
    return [_row(r) for r in rows]


def grant_rail_tool(db_path: str, *, user_id: str, rail_id: str, tool_id: str,
                    granted_by: str, note: str = "", pending: bool = False,
                    now: Optional[float] = None) -> RailGrant:
    """Write (or confirm) one grant. The ONLY writer of ``rail_grants``.

    The caller is an owner verb and has already checked the owner seat; this
    function refuses what no seat may grant (an unclassified id). It emits a
    ``rail_grant`` audit event (fail-open)."""
    if not user_id:
        raise ValueError("a grant needs a tenant user_id")
    tool = str(tool_id or "").strip().lower()
    if not grantable_tool(tool):
        raise ValueError(f"unknown tool {tool_id!r} — a grant names a tool id "
                         f"(e.g. defi_trade, shell, publish)")
    if not rail_id:
        raise ValueError("a grant needs a rail")
    init_schema(db_path, _SCHEMA)
    ts = time.time() if now is None else float(now)
    execute_retry(
        db_path,
        """INSERT INTO rail_grants (user_id, rail_id, tool_id, granted_by, granted_at, note,
                                    pending)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(user_id, rail_id, tool_id) DO UPDATE SET
               granted_by=excluded.granted_by, granted_at=excluded.granted_at,
               note=excluded.note, pending=excluded.pending""",
        (user_id, rail_id, tool, granted_by or "owner", ts, (note or "")[:500],
         1 if pending else 0))
    _audit(RAIL_GRANT, user_id, rail_id, tool, granted_by, pending=pending)
    return RailGrant(user_id, rail_id, tool, granted_by or "owner", ts, note or "", pending)


def revoke_rail_tool(db_path: str, *, user_id: str, rail_id: str, tool_id: str,
                     revoked_by: str = "owner") -> bool:
    """Remove one grant. Immediate; True when a row was removed."""
    if not _table_exists(db_path):
        return False
    n = execute_retry(
        db_path, "DELETE FROM rail_grants WHERE user_id=? AND rail_id=? AND tool_id=?",
        (user_id, rail_id, str(tool_id or "").strip().lower()))
    if n:
        _audit(RAIL_REVOKE, user_id, rail_id, str(tool_id).lower(), revoked_by)
    return bool(n)


def revoke_all(db_path: str, *, user_id: str, rail_id: str,
               revoked_by: str = "owner") -> int:
    """Remove every grant on one rail (``/rail drop``)."""
    if not _table_exists(db_path):
        return 0
    rows = grants_for(db_path, user_id=user_id, rail_id=rail_id, include_pending=True)
    n = execute_retry(db_path, "DELETE FROM rail_grants WHERE user_id=? AND rail_id=?",
                      (user_id, rail_id)) or 0
    for g in rows:
        _audit(RAIL_REVOKE, user_id, rail_id, g.tool_id, revoked_by)
    return int(n)


def _audit(kind: str, user_id: str, rail_id: str, tool_id: str, by: str,
           **extra) -> None:
    """One audit record per grant change. Fail-open: a telemetry write must never
    turn an owner's grant into an error."""
    try:
        from core.event_log import emit
        emit(kind, user_id=user_id, source="rails",
             attrs={"rail_id": rail_id, "tool_id": tool_id, "by": by, **extra})
    except Exception:
        pass


def split_grantable(tools: Iterable[str], *, actor: Optional[str],
                    rail_id: Optional[str] = None, user_id: Optional[str] = None,
                    db_path: Optional[str] = None,
                    ceiling: Optional[Iterable[str]] = None):
    """``(kept, refused, why)`` — the pure half of :func:`assert_grantable`."""
    ids = [str(t) for t in (tools or []) if str(t or "").strip()]
    if actor is None or actor == ACTOR_OWNER:
        return ids, [], ""
    gated = gated_ids()
    allowed = set(ceiling or ())
    refused: List[str] = []
    why = ""
    granted: Optional[set] = None
    armed: Optional[bool] = None
    for t in ids:
        if t not in gated:
            continue
        if actor == ACTOR_AGENT:
            if t not in allowed:
                refused.append(t)
                why = "outside what agent-written work may hold"
            continue
        if actor == ACTOR_RAIL:
            if granted is None:
                granted = {g.tool_id for g in grants_for(
                    db_path, user_id=user_id or "", rail_id=rail_id or "")} if rail_id else set()
            if t not in allowed and t not in granted:
                refused.append(t)
                why = "no rail grant covers it"
                continue
            if is_money(t):
                if armed is None:
                    armed = money_armed()
                if not armed:
                    refused.append(t)
                    why = "the money regime is not armed"
            continue
        refused.append(t)
        why = "this writer cannot carry tools"
    return [t for t in ids if t not in refused], refused, why


def assert_grantable(tools: Optional[Iterable[str]], *, actor: Optional[str],
                     rail_id: Optional[str] = None, user_id: Optional[str] = None,
                     db_path: Optional[str] = None,
                     ceiling: Optional[Iterable[str]] = None) -> List[str]:
    """Return *tools* unchanged, or raise :class:`GrantRefused`. See the module
    docstring for the actor table."""
    if actor is not None and actor not in ACTORS:
        raise GrantRefused(list(tools or []), actor, f"unknown actor {actor!r}")
    kept, refused, why = split_grantable(tools or [], actor=actor, rail_id=rail_id,
                                         user_id=user_id, db_path=db_path, ceiling=ceiling)
    if refused:
        raise GrantRefused(refused, actor, why)
    return kept


__all__ = [
    "ACTORS", "ACTOR_AGENT", "ACTOR_NONE", "ACTOR_OWNER", "ACTOR_RAIL", "GrantRefused",
    "RailGrant", "all_grants", "assert_grantable", "gated_ids", "grant_rail_tool",
    "grantable_tool", "grants_for", "is_money", "money_armed", "revoke_all",
    "revoke_rail_tool", "split_grantable",
]
