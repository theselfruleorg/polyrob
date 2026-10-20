"""Who may put a tool on a goal — ONE predicate (036 §3.3).

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
  ``none``          never — a writer that cannot carry tools (asks).
  ================  =========================================================

  An UNGATED id always passes. ``actor=None`` is the legacy/test path and passes
  unchanged; ``tests/test_board_create_grant_ratchet.py`` pins that every
  production ``board.create(`` call site names its actor, which is what turns the
  convention into a boundary. Recurring work is a cron job; its tools are the
  job's own (``payload.tools`` / ``payload.rig``), judged by the same predicate.

Pure core: ``core.tool_capabilities`` only.
"""
from __future__ import annotations

from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.tool_capabilities import ids_with

ACTOR_OWNER = "owner_seat"
ACTOR_AGENT = "agent"
ACTOR_NONE = "none"
ACTORS = (ACTOR_OWNER, ACTOR_AGENT, ACTOR_NONE)


class GrantRefused(ValueError):
    """A writer tried to put a gated tool on a goal it may not grant."""

    def __init__(self, tools: Sequence[str], actor: Optional[str], why: str):
        self.tools = list(tools)
        self.actor = actor
        self.why = why
        super().__init__(
            f"{', '.join(self.tools)} NOT granted ({why}). Only the owner grants a "
            f"money, host or high-impact tool — from an owner seat.")


def gated_ids() -> frozenset:
    """The ids a writer may not put on a goal without authority."""
    return ids_with("money") | ids_with("high_impact") | ids_with("exec")


def split_grantable(tools: Iterable[str], *, actor: Optional[str],
                    ceiling: Optional[Iterable[str]] = None):
    """``(kept, refused, why)`` — the pure half of :func:`assert_grantable`."""
    ids = [str(t) for t in (tools or []) if str(t or "").strip()]
    if actor is None or actor == ACTOR_OWNER:
        return ids, [], ""
    gated = gated_ids()
    allowed = set(ceiling or ())
    refused: List[str] = []
    why = ""
    for t in ids:
        if t not in gated:
            continue
        if actor == ACTOR_AGENT:
            if t not in allowed:
                refused.append(t)
                why = "outside what agent-written work may hold"
            continue
        refused.append(t)
        why = "this writer cannot carry tools"
    return [t for t in ids if t not in refused], refused, why


def assert_grantable(tools: Optional[Iterable[str]], *, actor: Optional[str],
                     ceiling: Optional[Iterable[str]] = None) -> List[str]:
    """Return *tools* unchanged, or raise :class:`GrantRefused`. See the module
    docstring for the actor table."""
    if actor is not None and actor not in ACTORS:
        raise GrantRefused(list(tools or []), actor, f"unknown actor {actor!r}")
    kept, refused, why = split_grantable(tools or [], actor=actor, ceiling=ceiling)
    if refused:
        raise GrantRefused(refused, actor, why)
    return kept


#: ``payload`` key a writer stamps (``GoalBoard.create``, the cron tool, the owner
#: seat's ``create_cron``): WHO wrote the row (085 W8 A4).
ACTOR_KEY = "actor"


def dispatch_recheck(tools: Optional[Iterable[str]], payload: Optional[Mapping[str, Any]], *,
                     declared: Optional[Iterable[str]],
                     agent_ceiling: Optional[Iterable[str]] = None) -> Tuple[List[str], List[str]]:
    """085 W8 A4: re-judge a row's DECLARED tool set at dispatch, keyed on the
    stored :data:`ACTOR_KEY`. Returns ``(tools to run with, refused ids)``.

    *declared* is what the row itself asks for (``payload.tools``, else its rig's
    ids); a gated id in it that the stored actor may not hold is dropped from
    *tools*. A row with no stored actor (written before the stamp) or no declared
    set passes unchanged; an unknown actor value holds no gated id (fail closed).
    The creation check is the same predicate; this catches a row written past the
    tool layer (a planted or legacy row) and a rig the creation check never judged."""
    ids = [str(t) for t in (tools or [])]
    data = payload if isinstance(payload, Mapping) else {}
    wanted = [str(t) for t in (declared or [])]
    if ACTOR_KEY not in data or not wanted:
        return ids, []
    actor = data.get(ACTOR_KEY)
    actor = actor if actor in ACTORS else ACTOR_NONE
    _kept, refused, _why = split_grantable(wanted, actor=actor, ceiling=agent_ceiling)
    return [t for t in ids if t not in refused], refused


__all__ = [
    "ACTORS", "ACTOR_AGENT", "ACTOR_KEY", "ACTOR_NONE", "ACTOR_OWNER", "GrantRefused",
    "assert_grantable", "dispatch_recheck", "gated_ids", "split_grantable",
]
