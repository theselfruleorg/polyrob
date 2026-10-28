"""Work › Goals — the goal board readers (2026-10-27).

``GET /api/webgate/goals/board`` — every live goal of the tenant (no newest-30
window), the recent finished ones, the open decisions, the dependency edges and
each goal's last activity: what the board view draws in one read.

``GET /api/webgate/goals/{goal_id}/detail`` — one goal's full record: what it
is, when it is done, its history (``goal_events``), its runs (the ``goal_run``
rows of the event log), what it waits on, what waits on it, its sub-goals and
the decisions that hold it.

Both are READS. The board writes through the routes that already exist:
``POST /api/webgate/goals`` (create), ``POST /api/webgate/goals/{id}/{verb}``
(``pause|retry|stop|drop``) and the Inbox's decision route
(``GoalBoard.decide_ask``, the ONE writer of an answer). Nothing here moves a
goal between statuses.

⚠️ Three rules, the same ones ``api_goals`` keeps:

1. ``GoalBoard.list`` is the dispatcher's claim queue and never backs a view;
   the board reads ``list_recent`` / ``status_counts`` / ``asks``.
2. A failed read is NOT an empty board. The main read failing answers 200 with
   ``error``; a side read failing (edges, events, runs) is named in
   ``unreadable`` and the rest still draws.
3. Every read is scoped to the console's tenant (``_effective_user_id``); a
   goal id of another tenant is a 404 miss.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.goal_vocab import (KIND_GOAL, LIVE_STATUS_ORDER, STATUS_BLOCKED,
                             STATUS_CANCELLED, STATUS_DONE, STATUS_READY,
                             has_own_surface)

logger = logging.getLogger(__name__)

router = APIRouter()

#: The most live goals one board read returns. A real board holds tens; past
#: this the answer says ``truncated`` rather than pretend it is whole.
LIVE_READ_CAP = 500
#: Finished goals per read unless the caller asks for more (``finished=N``).
FINISHED_DEFAULT = 20
FINISHED_MAX = 200
#: Referenced goals (a dependency or a parent) not on the board are looked up
#: by id for their title, up to this many; past it the answer names the rest.
TITLE_LOOKUP_CAP = 200
#: History and runs the detail returns, newest first.
EVENTS_CAP = 100
RUNS_CAP = 20
#: The statuses an open ask HOLDS — the same rule ``core.goal_owner_verbs.reason``
#: keeps: a running, waiting or finished goal is not "waiting on your answer"
#: because an old ask still names it.
HELD_STATUSES = (STATUS_READY, STATUS_BLOCKED)


def _pages():
    from webview import pages
    return pages


def _origin(goal: Any) -> Dict[str, Optional[str]]:
    """Who asked for it — ``core.goal_legibility.origin`` — plus the granted
    mandate's name, which the console words around."""
    from core.goal_legibility import ORIGIN_GRANTED, origin
    o = origin(goal)
    name = None
    if o == ORIGIN_GRANTED:
        p = goal.payload or {}
        name = str(p.get("stream") or p.get("cycle") or "") or None
    return {"origin": o.lower(), "origin_name": name}


def _cost(goal: Any) -> Optional[float]:
    """The last run's AI cost (``payload.cost_usd``, written per run by the
    dispatcher), or ``None`` when no run recorded one — never a guessed 0."""
    raw = (goal.payload or {}).get("cost_usd")
    try:
        return round(float(raw), 4) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _ask_keys(ask: Any) -> List[str]:
    from agents.task.goals.board_types import _ask_goal_keys
    p = ask.payload or {}
    return sorted(_ask_goal_keys(p.get("blocks_goal_ids") or [], p))


def _held_map(asks: List[Any]) -> Dict[str, List[Any]]:
    """``{goal_id: [open asks that name it]}`` — ``blocks_goal_ids`` plus the
    ask's own ``goal:<id>`` rail, for the board AND the detail alike."""
    held: Dict[str, List[Any]] = {}
    for a in asks:
        for gid in _ask_keys(a):
            held.setdefault(gid, []).append(a)
    return held


def _holds(goal: Any, held: Dict[str, List[Any]]) -> List[Any]:
    return held.get(goal.id, []) if goal.status in HELD_STATUSES else []


def _ask_dict(ask: Any) -> dict:
    p = ask.payload or {}
    return {
        "id": ask.id, "kind": "ask", "title": ask.title, "body": ask.body,
        "status": ask.status, "created_at": ask.created_at,
        "holds": _ask_keys(ask), "own_place": has_own_surface(p),
        "ask_kind": p.get("ask_kind") or None,
    }


def _card(goal: Any, *, held: List[Any], deps: List[str], blocks: List[str],
          last: Optional[float]) -> dict:
    from core.goal_legibility import status_reason
    row = _pages()._goal_dict(goal)
    row.pop("result", None)        # the detail carries it; the board need not
    row.update(_origin(goal))
    row.update({
        "kind": "goal",
        "status_reason": status_reason(goal, held),
        "parent_id": goal.parent_id,
        "started_at": goal.started_at,
        "last_activity": last,
        "cost_usd": _cost(goal),
        "failures": goal.consecutive_failures,
        "max_retries": goal.max_retries,
        "depends_on": deps,
        "blocks": blocks,
        "held_by": [a.id for a in held],
    })
    return row


def _limits() -> dict:
    from core.config_policy import AutonomyConfig
    from core.config_policy.capability_toggles import autonomous_run_budget_usd
    return {"run_usd": autonomous_run_budget_usd(),
            "max_steps": AutonomyConfig.autonomous_max_steps()}


def _finished_n(raw: Optional[str]) -> int:
    try:
        n = int(raw) if raw not in (None, "") else FINISHED_DEFAULT
    except (TypeError, ValueError):
        n = FINISHED_DEFAULT
    return max(0, min(FINISHED_MAX, n))


@router.get("/api/webgate/goals/board")
async def api_goal_board(request: Request, finished: Optional[str] = None):
    """The whole board for the tenant — see the module docstring."""
    pages = _pages()
    enabled = pages.AutonomyConfig.goals_enabled()
    user_id = pages._effective_user_id(request)  # 403 outside the try — never fail-open
    n_finished = _finished_n(finished)
    try:
        board = pages._webgate_goal_board()
        live = board.list_recent(user_id=user_id, statuses=LIVE_STATUS_ORDER,
                                 limit=LIVE_READ_CAP + 1)
        done = board.list_recent(user_id=user_id,
                                 statuses=(STATUS_DONE, STATUS_CANCELLED),
                                 limit=n_finished + 1,
                                 newest="finished") if n_finished else []
        counts = board.status_counts(user_id=user_id)
        asks = board.asks(user_id=user_id, status="open")
    except Exception as exc:
        return JSONResponse({"enabled": enabled, "columns": list(LIVE_STATUS_ORDER),
                             "goals": [], "finished": [], "asks": [], "counts": {},
                             "error": pages._safe_reason(exc)})
    unreadable: Dict[str, str] = {}
    try:
        edges = board.edges(user_id=user_id)
    except Exception as exc:
        edges = []
        unreadable["dependencies"] = pages._safe_reason(exc)
    try:
        last = board.last_event_times(user_id=user_id)
    except Exception as exc:
        last = {}
        unreadable["history"] = pages._safe_reason(exc)

    deps: Dict[str, List[str]] = {}
    blocks: Dict[str, List[str]] = {}
    for gid, dep in edges:
        deps.setdefault(gid, []).append(dep)
        blocks.setdefault(dep, []).append(gid)
    held = _held_map(asks)

    truncated = len(live) > LIVE_READ_CAP
    live = live[:LIVE_READ_CAP]
    # At the cap a bigger ask returns the same rows: no "show more" that adds none.
    more_finished = len(done) > n_finished and n_finished < FINISHED_MAX
    done = done[:n_finished]

    def card(g):
        return _card(g, held=_holds(g, held), deps=deps.get(g.id, []),
                     blocks=blocks.get(g.id, []), last=last.get(g.id))

    goals = [card(g) for g in live]
    finished_rows = [card(g) for g in done]
    # A dependency or parent that is not on the board (an older finished goal)
    # still gets its title, looked up by id within THIS tenant.
    shown = {g["id"] for g in goals} | {g["id"] for g in finished_rows}
    wanted: List[str] = []
    for g in goals + finished_rows:
        for ref in g["depends_on"] + g["blocks"] + ([g["parent_id"]] if g["parent_id"] else []):
            if ref not in shown and ref not in wanted:
                wanted.append(ref)
    titles: Dict[str, dict] = {}
    try:
        for ref in wanted[:TITLE_LOOKUP_CAP]:
            other = board.get(ref, user_id=user_id)
            if other is not None:
                titles[ref] = {"title": other.title, "status": other.status}
    except Exception as exc:
        unreadable["titles"] = pages._safe_reason(exc)
    if len(wanted) > TITLE_LOOKUP_CAP:
        from webview.copy import t
        unreadable["titles"] = t("work.board.titles_capped",
                                 count=len(wanted) - TITLE_LOOKUP_CAP)
    return JSONResponse({
        "enabled": enabled,
        "columns": list(LIVE_STATUS_ORDER),
        "goals": goals,
        "finished": finished_rows,
        "more_finished": more_finished,
        "truncated": truncated,
        "asks": [_ask_dict(a) for a in asks],
        "counts": counts,
        "titles": titles,
        "limits": _limits(),
        "unreadable": unreadable,
    })


def _runs(goal_id: str, user_id: str) -> List[dict]:
    """The ``goal_run`` rows for one goal, newest first. Raises when the log
    cannot be read; ``[]`` when it holds none (or does not exist yet).

    The query filters by the goal id IN SQL, so an old goal's runs are found
    however many runs came after them. It does not go through
    ``TelemetryEventLog.query``, which answers ``[]`` on a read error — the one
    answer a person must not see for an unreadable log."""
    import json

    from core.event_log import open_event_log
    from core.sqlite_util import execute_retry
    from webview.activity import _data_dir, _sessions_data_root
    log = open_event_log(_sessions_data_root() or _data_dir())
    if log is None:
        return []
    from agents.task.runtime.stop_reason import describe_stop_reason
    rows = execute_retry(
        log.db_path,
        "SELECT ts, session_id, attrs FROM telemetry_events "
        "WHERE kind='goal_run' AND user_id=? AND CASE WHEN json_valid(attrs) "
        "THEN json_extract(attrs, '$.goal_id') END = ? "
        "ORDER BY ts DESC, id DESC LIMIT ?",
        (str(user_id), str(goal_id), RUNS_CAP), fetch="all") or []
    out = []
    for row in rows:
        try:
            attrs = json.loads(row["attrs"]) if row["attrs"] else {}
        except (TypeError, ValueError):
            attrs = {}
        out.append({
            "ts": row["ts"], "outcome": attrs.get("outcome"),
            "reason": attrs.get("reason"), "stop_reason": attrs.get("stop_reason"),
            # The run's own words for why it stopped (083's typed stop_reason).
            "stop_text": (describe_stop_reason(attrs["stop_reason"])
                          if attrs.get("stop_reason") else None),
            "steps": attrs.get("steps"), "spend_usd": attrs.get("spend_usd"),
            "duration_sec": attrs.get("duration_sec"),
            "session_id": row["session_id"] or attrs.get("session_id") or None,
        })
    return out


def _ref(board: Any, gid: str, user_id: str) -> dict:
    other = board.get(gid, user_id=user_id)   # a foreign id reads as unknown
    return {"id": gid, "title": other.title if other else None,
            "status": other.status if other else None}


@router.get("/api/webgate/goals/{goal_id}/detail")
async def api_goal_detail(request: Request, goal_id: str):
    """One goal's full record — see the module docstring."""
    pages = _pages()
    user_id = pages._effective_user_id(request)
    try:
        board = pages._webgate_goal_board()
        goal = board.get(goal_id, user_id=user_id)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": pages._safe_reason(exc)})
    if goal is None or goal.kind != KIND_GOAL:
        return JSONResponse({"ok": False, "message": f"no goal {goal_id} for this tenant"},
                            status_code=404)
    unreadable: Dict[str, str] = {}

    def side(name, fn, default):
        try:
            return fn()
        except Exception as exc:
            unreadable[name] = pages._safe_reason(exc)
            return default

    # The board's own rule for which asks hold a goal (``_held_map``), so the
    # detail never disagrees with the card it opened from.
    held = _holds(goal, _held_map(side("decisions", lambda: board.asks(
        user_id=user_id, status="open"), [])))
    events = side("history", lambda: board.events(goal.id, last=EVENTS_CAP), [])
    runs = side("runs", lambda: _runs(goal.id, user_id), [])
    deps = side("dependencies", lambda: [_ref(board, d, user_id)
                                         for d in board.dependencies(goal.id)], [])
    blocks = side("dependencies", lambda: [_ref(board, d, user_id)
                                           for d in board.dependents(goal.id)], [])
    children = side("sub_goals", lambda: [
        {"id": c.id, "title": c.title, "status": c.status}
        for c in board.children(goal.id, user_id=user_id)], [])
    parent = side("parent", lambda: _ref(board, goal.parent_id, user_id), None) \
        if goal.parent_id else None

    p = goal.payload or {}
    attempts = [a for a in (p.get("attempts") or []) if isinstance(a, dict)]
    sessions: List[str] = []
    for sid in ([pages._goal_session_id(goal)] + [r.get("session_id") for r in runs]
                + [a.get("session_id") for a in attempts]):
        if sid and sid not in sessions:
            sessions.append(str(sid))

    from core.goal_legibility import status_reason
    body = _card(goal, held=held, deps=[d["id"] for d in deps],
                 blocks=[b["id"] for b in blocks], last=None)
    body.update({
        "ok": True,
        "body": goal.body,
        "acceptance": p.get("acceptance") or None,
        "acceptance_checks": p.get("acceptance_checks") or [],
        "outcome": p.get("outcome") or None,
        "result": goal.result,
        "last_failure_error": goal.last_failure_error,
        "attempts": attempts[-5:],
        "status_reason": status_reason(goal, held),
        "events": [{"kind": e.get("kind"), "ts": e.get("created_at"),
                    "payload": e.get("payload") or {}}
                   for e in reversed(events[-EVENTS_CAP:])],
        "runs": runs,
        "waits_on": deps, "waited_on_by": blocks,
        "sub_goals": children, "parent": parent,
        "decisions": [_ask_dict(a) for a in held],
        "sessions": sessions,
        "limits": _limits(),
        "unreadable": unreadable,
    })
    if events:
        body["last_activity"] = events[-1].get("created_at")
    return JSONResponse(body)


from webview.contributions import register_console_router  # noqa: E402

register_console_router(router, destination="work", source="webview.goal_board_routes")
