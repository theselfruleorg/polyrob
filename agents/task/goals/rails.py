"""Rails — standing work as configuration (036).

A RAIL is an objective row that carries ``payload.recurrence``. There is no third
container: the objective already has a title, a body, a tenant, a priority and the
owner-steered lifecycle (``active`` = ON, ``paused`` = OFF, ``dropped``). What makes
it a rail is the recurrence::

    payload.recurrence = {
        "name":     "topic-watch",          # the owner's handle (unique per tenant)
        "schedule": "every 24h",            # core.schedule vocabulary (the cron one)
        "max_live": 1,                      # legs in flight at once (default: #legs)
        "rig":      "research",             # optional base rig (core.config_policy.rigs)
        "skills":   ["..."],                # optional pinned doctrine (060 WS-5)
        "legs": [ {title, body, priority, acceptance, max_steps, independent}, ... ],
    }

⚠️ A leg NEVER carries ``tools``. :func:`validate_recurrence` refuses one. A tool
beyond the base toolset reaches a leg only through ``rail_grants``
(``core/tool_grants.py``) — an owner act — and a MONEY grant only while the money
regime is ``armed``. The seeder computes ``payload.tools = base ∪ grants`` at seed
time and writes the row through ``GoalBoard.create(actor="rail", rail_id=...)``,
so the ONE predicate checks it again.

The tick (:func:`seed_due_rails`) runs in-process from the shared autonomy runtime
(``core/autonomy_runtime.py``, beside the goal dispatcher), so it works for
``pip install polyrob``, a terminal ``rob`` and the server alike. It does three
things the retired stream script did not:

1. **Pause first** — ``allows("seed_stream")`` is read BEFORE any board write.
2. **Idle gate** — a live owner turn (in-process counter or the cross-process turn
   marker) defers the tick, exactly as ``GoalDispatcher.dispatch_once`` does.
3. **One schedule vocabulary** — ``core.schedule.parse_schedule``, the parser
   ``/cron`` uses.

Seeded legs carry ``payload.stream = payload.rail = <rail id>`` (so the board's
tag-filtered ``stream_goals`` throttle, the stream-scope suppression and the
budget exemption all apply unchanged) and ``payload.provenance = {source: "rail",
rail_id, leg}``.

036 §10b: a leg chains on the previous leg by default (it usually consumes what
the previous leg wrote); a REPORTING leg sets ``independent: true`` so a pause on
the acting leg can never silently become a pause on the accounting.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.goal_vocab import (KIND_OBJECTIVE, LIVE_STATUSES, OBJ_ACTIVE, OBJ_DROPPED,
                             OBJ_PAUSED)
from core.sqlite_util import execute_retry

logger = logging.getLogger(__name__)

RECURRENCE_KEY = "recurrence"
#: How often the in-process tick looks for a due rail.
RAILS_TICK_SEC = 300
#: A seed claim older than this is stale (a crashed process) and may be retaken.
CLAIM_TTL_SEC = 600
#: Slack on an interval schedule so a tick landing seconds early is not skipped a
#: whole tick (the stream seeder measured 4 h running at 5 h); ≤ a quarter interval.
SCHEDULE_TOLERANCE_SEC = 15 * 60
#: An active objective with no recurrence and no activity for this long renders
#: under UNDECLARED (adopt it as a rail, or drop it).
UNDECLARED_AFTER_SEC = 30 * 86400
MAX_LEGS = 8
_NAME_RE = re.compile(r"[^a-z0-9]+")
_LEG_KEYS = frozenset({"title", "body", "priority", "acceptance", "max_steps",
                       "independent"})


class RailError(ValueError):
    """A rail definition the owner must fix (named so the seat can say it)."""


# --- the definition ---------------------------------------------------------------


def slug(text: str) -> str:
    return _NAME_RE.sub("-", str(text or "").lower()).strip("-")[:48]


def normalize_schedule(spec: Any) -> str:
    """Accept the owner's words and the old manifest's ``cadence_hours``."""
    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        return f"every {int(spec)}h"
    return str(spec or "").strip()


def parse_rail_schedule(spec: str):
    from core.schedule import ScheduleError, parse_schedule
    try:
        return parse_schedule(normalize_schedule(spec))
    except ScheduleError as e:
        raise RailError(f"schedule {spec!r}: {e}") from e


def validate_recurrence(rec: Any) -> Dict[str, Any]:
    """The cleaned recurrence, or :class:`RailError`. Refuses any ``tools`` key."""
    if not isinstance(rec, dict):
        raise RailError("a rail needs a recurrence mapping")
    if "tools" in rec:
        raise RailError("a rail never carries tools — the owner grants one with "
                        "/rail grant <rail> <tool>")
    schedule = normalize_schedule(rec.get("schedule") or rec.get("cadence_hours"))
    if not schedule:
        raise RailError("a rail needs a schedule (e.g. every 24h, every monday 09:00)")
    parse_rail_schedule(schedule)
    legs_in = rec.get("legs")
    if not isinstance(legs_in, list) or not legs_in:
        raise RailError("a rail needs at least one leg (title + body)")
    if len(legs_in) > MAX_LEGS:
        raise RailError(f"a rail has at most {MAX_LEGS} legs")
    legs: List[Dict[str, Any]] = []
    for i, leg in enumerate(legs_in):
        if not isinstance(leg, dict):
            raise RailError(f"leg #{i + 1} is not a mapping")
        if "tools" in leg:
            raise RailError(f"leg #{i + 1} carries tools — a rail leg never does; the "
                            f"owner grants with /rail grant")
        title = str(leg.get("title") or "").strip()
        body = str(leg.get("body") or "").strip()
        if not title or not body:
            raise RailError(f"leg #{i + 1} needs a title and a body")
        clean: Dict[str, Any] = {"title": title[:200], "body": body[:8000]}
        for key in ("priority", "max_steps"):
            if leg.get(key) is not None:
                try:
                    clean[key] = int(leg[key])
                except (TypeError, ValueError) as e:
                    raise RailError(f"leg #{i + 1}: {key} must be a number") from e
        if leg.get("acceptance"):
            clean["acceptance"] = str(leg["acceptance"])[:2000]
        if leg.get("independent"):
            clean["independent"] = True
        legs.append(clean)
    out: Dict[str, Any] = {k: v for k, v in rec.items()
                           if k in ("name", "last_seeded_at", "seed_claim")}
    out.update({"schedule": schedule, "legs": legs})
    if rec.get("max_live") is not None:
        try:
            out["max_live"] = max(0, int(rec["max_live"]))
        except (TypeError, ValueError) as e:
            raise RailError("max_live must be a number") from e
    if rec.get("rig"):
        from core.config_policy.rigs import is_rig, rig_names
        if not is_rig(rec["rig"]):
            raise RailError(f"unknown rig {rec['rig']!r} (valid: {', '.join(rig_names())})")
        out["rig"] = str(rec["rig"]).strip().lower()
    if rec.get("skills"):
        from core.config_policy.rigs import pinned_skills
        skills = pinned_skills({"skills": rec["skills"]})
        if skills:
            out["skills"] = skills
    return out


def recurrence_of(row: Any) -> Optional[Dict[str, Any]]:
    payload = getattr(row, "payload", None) or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    rec = payload.get(RECURRENCE_KEY) if isinstance(payload, dict) else None
    return rec if isinstance(rec, dict) else None


def is_rail(row: Any) -> bool:
    return getattr(row, "kind", KIND_OBJECTIVE) == KIND_OBJECTIVE and recurrence_of(row) is not None


def rail_name(row: Any) -> str:
    rec = recurrence_of(row) or {}
    return str(rec.get("name") or slug(getattr(row, "title", "")) or getattr(row, "id", ""))


# --- reads ------------------------------------------------------------------------


def list_rails(board: Any, user_id: str, *, include_dropped: bool = False) -> List[Any]:
    """Every rail of one tenant (ON first, then OFF), from the board — the store."""
    rows = [o for o in (board.objectives(user_id=user_id) or []) if is_rail(o)]
    if not include_dropped:
        rows = [o for o in rows if o.status != OBJ_DROPPED]
    return sorted(rows, key=lambda o: (o.status != OBJ_ACTIVE, rail_name(o)))


def find_rail(board: Any, user_id: str, ref: str, *,
              include_dropped: bool = False) -> Tuple[Optional[Any], Optional[str]]:
    """``(rail, None)`` or ``(None, why)`` for a rail name, id or id prefix."""
    ref = str(ref or "").strip()
    if not ref:
        return None, "name a rail (see /rail)"
    rails = list_rails(board, user_id, include_dropped=include_dropped)
    exact = [r for r in rails if rail_name(r) == ref.lower() or r.id == ref]
    if exact:
        return exact[0], None
    pref = [r for r in rails if r.id.startswith(ref) or rail_name(r).startswith(ref.lower())]
    if len(pref) == 1:
        return pref[0], None
    if len(pref) > 1:
        return None, f"'{ref}' matches {len(pref)} rails: " + ", ".join(
            rail_name(r) for r in pref[:5])
    return None, f"no rail named '{ref}' (see /rail)"


def rail_goals(board: Any, user_id: str, rail_id: str) -> List[Any]:
    return list(board.stream_goals(user_id, rail_id) or [])


def rail_live(board: Any, user_id: str, rail_id: str) -> int:
    return sum(1 for g in rail_goals(board, user_id, rail_id)
               if getattr(g, "status", None) in LIVE_STATUSES)


def last_seeded_at(board: Any, row: Any) -> Optional[float]:
    rec = recurrence_of(row) or {}
    stamp = rec.get("last_seeded_at")
    if stamp:
        try:
            return float(stamp)
        except (TypeError, ValueError):
            pass
    goals = rail_goals(board, row.user_id, row.id)
    return max((float(g.created_at or 0) for g in goals), default=None)


def next_seed_at(board: Any, row: Any, now: Optional[float] = None) -> Optional[float]:
    """When the rail is next due by its schedule; ``None`` = never again (a spent
    one-shot) and ``now`` or earlier = due (the live ceiling is separate)."""
    now = time.time() if now is None else now
    rec = recurrence_of(row) or {}
    sched = parse_rail_schedule(rec.get("schedule") or "")
    last = last_seeded_at(board, row)
    if last is None:
        if sched.kind == "once":
            return sched.once_at.timestamp()
        return now
    nxt = sched.next_run_after(datetime.fromtimestamp(last))
    if nxt is None:
        return None
    ts = nxt.timestamp()
    if sched.kind == "interval":
        ts -= min(float(SCHEDULE_TOLERANCE_SEC), float(sched.interval_seconds) / 4.0)
    return ts


def _max_live(rec: Dict[str, Any]) -> int:
    raw = rec.get("max_live")
    return int(raw) if raw is not None else len(rec.get("legs") or [])


def rail_is_due(board: Any, user_id: str, row: Any,
                now: Optional[float] = None) -> Tuple[bool, str]:
    """``(due, reason)``. Pause, owner switch, suppression, ceiling, schedule."""
    now = time.time() if now is None else now
    from core.autonomy_control import allows
    dec = allows("seed_stream")
    if not dec.allowed:
        return False, f"rail seeding {dec.reason}"
    if row.status != OBJ_ACTIVE:
        return False, f"switched off ({row.status})"
    db = getattr(board, "db_path", None)
    if db:
        from core.goal_suppressions import SCOPE_OBJECTIVE, SCOPE_STREAM, find
        if find(db, user_id=user_id, scope=SCOPE_STREAM, value=row.id) or \
                find(db, user_id=user_id, scope=SCOPE_OBJECTIVE, value=row.id):
            return False, "switched off by the owner (/goal allow to turn it on)"
    rec = recurrence_of(row) or {}
    try:
        ceiling = _max_live(rec)
        nxt = next_seed_at(board, row, now)
    except RailError as e:
        return False, f"cannot read the schedule: {e}"
    live = rail_live(board, user_id, row.id)
    if live >= max(0, ceiling):
        return False, f"{live} leg(s) live (ceiling {ceiling})"
    from core.goal_suppressions import SCOPE_TITLE, find
    needed = sum(1 for leg in rec.get("legs") or []
                 if not find(db, user_id=user_id, scope=SCOPE_TITLE, value=leg["title"]))
    if needed > ceiling - live:
        return False, (f"cycle needs {needed} slots; {ceiling - live} free "
                       f"(ceiling {ceiling})")
    if nxt is None:
        return False, "one-shot already seeded"
    if nxt > now:
        return False, "next seed " + time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(nxt))
    return True, "due"


def base_tools(rec: Dict[str, Any]) -> List[str]:
    """The toolset a leg gets with no grant: the rail's rig, else the goal default.

    A MONEY id is never part of the base — a rig that names one (``money_rail``)
    gets it only through a grant under the armed regime, so a rail can never
    exceed the money regime by picking a rig."""
    from core.config_policy.rigs import resolve_rig_tools
    from core.tool_grants import is_money
    from agents.task.goals.dispatcher import default_goal_tools
    # Use the same precedence as dispatch, including AUTONOMOUS_RIG_DEFAULT.
    ids = resolve_rig_tools(rec, default_goal_tools()) or []
    return [t for t in ids if not is_money(t)]


def leg_tools(board: Any, user_id: str, row: Any) -> Tuple[List[str], List[str]]:
    """``(payload.tools, notes)`` for one seed of *row*.

    Always pin the money-free effective base so dispatch cannot recover an
    ungranted money tool through the default rig. Add money grants only while
    the money regime is armed; otherwise drop them and report why.
    """
    from core.tool_grants import grants_for, is_money, money_armed
    rec = recurrence_of(row) or {}
    grants = [g.tool_id for g in grants_for(getattr(board, "db_path", None),
                                            user_id=user_id, rail_id=row.id)]
    if not grants:
        return base_tools(rec), []
    notes: List[str] = []
    armed = money_armed()
    kept = []
    for t in grants:
        if is_money(t) and not armed:
            notes.append(f"{t} granted but inert: the money regime is not armed")
            continue
        kept.append(t)
    base = base_tools(rec)
    if not kept:
        return base, notes
    return list(dict.fromkeys(base + kept)), notes


def undeclared(board: Any, user_id: str, now: Optional[float] = None) -> List[Any]:
    """Active objectives that are not rails, have no live children and no activity
    in :data:`UNDECLARED_AFTER_SEC` — each needs an owner decision: adopt or drop."""
    now = time.time() if now is None else now
    try:
        last = board.objective_last_activity(user_id) or {}
    except Exception:
        last = {}
    out = []
    for o in board.objectives(user_id=user_id, status=OBJ_ACTIVE) or []:
        if is_rail(o) or (o.payload or {}).get("stream_id"):
            continue
        if any(c.status in LIVE_STATUSES for c in board.children_of(user_id, o.id)):
            continue
        seen = float(last.get(o.id) or o.created_at or 0)
        if now - seen >= UNDECLARED_AFTER_SEC:
            out.append(o)
    return out


# --- writes -----------------------------------------------------------------------


def create_rail(board: Any, user_id: str, *, title: str, body: str = "",
                recurrence: Dict[str, Any], priority: int = 5,
                created_by: str = "owner") -> Any:
    """Create a rail (an objective with a recurrence). Owner seats only."""
    rec = validate_recurrence(recurrence)
    name = slug(rec.get("name") or title)
    if not name:
        raise RailError("a rail needs a name or a title")
    existing = [r for r in list_rails(board, user_id, include_dropped=False)
                if rail_name(r) == name]
    if existing:
        raise RailError(f"a rail named '{name}' already exists (/rail edit {name} …)")
    rec["name"] = name
    return board.create_objective(
        user_id=user_id, title=str(title or name)[:200], body=body or "",
        priority=priority, force=True,
        payload={RECURRENCE_KEY: rec, "authored_by": "owner", "created_via": created_by})


def update_recurrence(board: Any, row: Any, patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge *patch* into the rail's recurrence (validated), keeping the seed stamps."""
    rec = dict(recurrence_of(row) or {})
    rec.update(patch)
    clean = validate_recurrence(rec)
    board.merge_payload(row.id, {RECURRENCE_KEY: clean})
    return clean


def _claim(board: Any, row_id: str, now: float) -> bool:
    """Per-rail CAS so two processes cannot seed the same cycle."""
    rc = execute_retry(
        board.db_path,
        """UPDATE goals SET payload=json_set(payload,'$.recurrence.seed_claim',?)
            WHERE id=? AND kind=?
              AND (json_extract(payload,'$.recurrence.seed_claim') IS NULL
                   OR json_extract(payload,'$.recurrence.seed_claim') < ?)""",
        (now, row_id, KIND_OBJECTIVE, now - CLAIM_TTL_SEC))
    return rc == 1


def _release(board: Any, row_id: str, *, seeded_at: Optional[float]) -> None:
    if seeded_at is None:
        sql = "UPDATE goals SET payload=json_remove(payload,'$.recurrence.seed_claim') WHERE id=?"
        params: tuple = (row_id,)
    else:
        sql = ("UPDATE goals SET payload=json_set(json_remove(payload,"
               "'$.recurrence.seed_claim'),'$.recurrence.last_seeded_at',?) WHERE id=?")
        params = (seeded_at, row_id)
    try:
        execute_retry(board.db_path, sql, params)
    except Exception:
        logger.debug("rail claim release failed for %s", row_id, exc_info=True)


def seed_rail(board: Any, user_id: str, row: Any,
              now: Optional[float] = None, *, due_only: bool = False) -> List[Any]:
    """Write one cycle of *row*'s legs. All-or-nothing; returns the created goals.

    Automatic ticks use due_only to recheck the current row while holding the
    claim. Direct callers retain the manual force behavior.

    ``force=True`` on every create: a recurring cycle reuses its titles by design,
    and the schedule + live ceiling are the (stricter) throttle. A leg the owner
    switched off (title suppression) is skipped and the chain runs on from the last
    leg that WAS written."""
    now = time.time() if now is None else now
    rec = recurrence_of(row) or {}
    if not _claim(board, row.id, now):
        return []
    created: List[Any] = []
    try:
        if due_only:
            fresh = board.get(row.id, user_id=user_id)
            if fresh is None or not is_rail(fresh) or not rail_is_due(board, user_id, fresh, now)[0]:
                _release(board, row.id, seeded_at=None)
                return []
            row = fresh
            rec = recurrence_of(row) or {}
        tools, notes = leg_tools(board, user_id, row)
        if not tools:
            raise RailError("rail resolves to no granted tools; refusing a wider dispatch fallback")
        base = base_tools(rec)
        previous_id: Optional[str] = None
        from core.goal_suppressions import SCOPE_TITLE, find
        for i, leg in enumerate(rec.get("legs") or []):
            if find(board.db_path, user_id=user_id, scope=SCOPE_TITLE, value=leg["title"]):
                continue
            payload: Dict[str, Any] = {
                "stream": row.id, "rail": row.id,
                "provenance": {"source": "rail", "rail_id": row.id, "leg": i},
                "max_steps": int(leg.get("max_steps") or 30),
                "authored_by": "owner",
            }
            if tools:
                payload["tools"] = list(tools)
            if rec.get("rig"):
                payload["rig"] = rec["rig"]  # legibility; `tools` wins at dispatch
            if rec.get("skills"):
                payload["skills"] = list(rec["skills"])
            if leg.get("acceptance"):
                payload["acceptance"] = leg["acceptance"]
            chained = previous_id if (previous_id and not leg.get("independent")) else None
            goal = board.create(
                user_id=user_id, title=leg["title"], body=leg["body"],
                priority=int(leg.get("priority") or row.priority or 5),
                parent_id=row.id, payload=payload, force=True,
                depends_on=[chained] if chained else None,
                actor="rail", rail_id=row.id, tool_ceiling=base)
            previous_id = goal.id
            created.append(goal)
    except BaseException:
        for g in reversed(created):
            try:
                board.cancel(g.id, user_id=user_id)
            except Exception:
                pass
        _release(board, row.id, seeded_at=None)
        raise
    _release(board, row.id, seeded_at=now if created else None)
    if created:
        try:
            from core.event_kinds import RAIL_SEEDED
            from core.event_log import emit
            emit(RAIL_SEEDED, user_id=user_id, source="rails",
                 attrs={"rail_id": row.id, "name": rail_name(row), "legs": len(created),
                        "granted": list(tools or []), "notes": notes})
        except Exception:
            pass
    return created


# --- the tick -----------------------------------------------------------------------


def _tenants_with_rails(db_path: str) -> List[str]:
    rows = execute_retry(
        db_path,
        """SELECT DISTINCT user_id FROM goals
            WHERE kind=? AND status=? AND json_extract(payload,'$.recurrence') IS NOT NULL""",
        (KIND_OBJECTIVE, OBJ_ACTIVE), fetch="all") or []
    return [r["user_id"] for r in rows]


def _owner_turn_live() -> Optional[str]:
    """Why a live owner turn defers the tick, or ``None``."""
    try:
        from core.interactive_gate import is_interactive_busy, read_turn_marker
        if is_interactive_busy():
            return "an owner turn is live in this process"
        marker = read_turn_marker()
        if marker is not None:
            return "a live owner turn (%s)" % (
                str(marker.get("kind") or "?") if isinstance(marker, dict) else "?")
    except Exception:
        logger.debug("rails idle-gate probe failed (fail-open)", exc_info=True)
    return None


def seed_due_rails(board: Any, *, now: Optional[float] = None,
                   idle_gate: bool = True) -> Dict[str, Any]:
    """One tick: seed every due rail of every tenant. Returns a summary.

    Pause FIRST — nothing below it reads or writes the board while paused."""
    from core.autonomy_control import allows
    dec = allows("seed_stream")
    if not dec.allowed:
        return {"seeded": 0, "skipped": f"paused: {dec.reason}"}
    if idle_gate:
        why = _owner_turn_live()
        if why:
            return {"seeded": 0, "skipped": f"deferred: {why}"}
    now = time.time() if now is None else now
    seeded = 0
    errors: List[str] = []
    try:
        tenants = _tenants_with_rails(board.db_path)
    except Exception as e:
        return {"seeded": 0, "skipped": f"board unreadable: {e}"}
    for uid in tenants:
        for row in list_rails(board, uid):
            try:
                due, _why = rail_is_due(board, uid, row, now)
                if not due:
                    continue
                seeded += len(seed_rail(board, uid, row, now, due_only=True))
            except Exception as e:
                errors.append(f"{rail_name(row)}: {e}")
                logger.warning("rail %s did not seed: %s", rail_name(row), e)
    return {"seeded": seeded, "errors": errors}


class RailsTicker:
    """The in-process rails loop (started by ``core.autonomy_runtime``)."""

    def __init__(self, board: Any, interval_seconds: int = RAILS_TICK_SEC):
        self.board = board
        self.interval_seconds = interval_seconds

    async def tick_once(self) -> Dict[str, Any]:
        import asyncio
        return await asyncio.to_thread(seed_due_rails, self.board)

    async def run_forever(self, stop_event=None) -> None:
        from core.tickers import IntervalTicker
        await IntervalTicker(self.tick_once, self.interval_seconds).run_forever(
            stop_event=stop_event)


def build_rails_ticker(*, data_dir: str) -> RailsTicker:
    import os
    from agents.task.goals.board import GoalBoard
    return RailsTicker(GoalBoard(os.path.join(data_dir, "goals.db")))


# --- import / export (036 §4.4) -------------------------------------------------------


def export_manifest(board: Any, user_id: str) -> Dict[str, Any]:
    """Every rail as the old ``streams.yaml`` schema, with its ACTIVE grants as
    ``tools`` on the legs — so an operator keeps their manifest in their own repo."""
    from core.tool_grants import grants_for
    out = []
    for row in list_rails(board, user_id):
        rec = recurrence_of(row) or {}
        granted = [g.tool_id for g in grants_for(board.db_path, user_id=user_id,
                                                 rail_id=row.id)]
        legs = []
        for leg in rec.get("legs") or []:
            item = dict(leg)
            if granted:
                item["tools"] = list(granted)
            legs.append(item)
        entry = {"id": rail_name(row), "objective": {"title": row.title, "body": row.body},
                 "schedule": rec.get("schedule"), "goals": legs,
                 "enabled": row.status == OBJ_ACTIVE}
        if rec.get("max_live") is not None:
            entry["max_live_goals"] = rec["max_live"]
        if rec.get("rig"):
            entry["rig"] = rec["rig"]
        out.append(entry)
    return {"streams": out}


def import_manifest(board: Any, user_id: str, doc: Dict[str, Any], *,
                    granted_by: str = "owner:cli") -> Dict[str, Any]:
    """Create rails from a manifest mapping. ``tools`` in the file become PENDING
    grants — never applied; the owner confirms each with ``/rail grant``.

    An existing rail of the same name is left alone (reported as ``kept``)."""
    from core.tool_grants import grant_rail_tool, grantable_tool
    streams = doc.get("streams") if isinstance(doc, dict) else None
    if not isinstance(streams, list) or not streams:
        raise RailError("the file needs a non-empty 'streams' list")
    created, kept, pending = [], [], []
    for i, s in enumerate(streams):
        if not isinstance(s, dict):
            raise RailError(f"stream #{i + 1} is not a mapping")
        name = slug(s.get("id") or (s.get("objective") or {}).get("title"))
        obj = s.get("objective") or {}
        legs = []
        tools: List[str] = []
        for g in s.get("goals") or []:
            if not isinstance(g, dict):
                continue
            tools.extend(str(t) for t in (g.get("tools") or []))
            legs.append({k: v for k, v in g.items() if k in _LEG_KEYS})
        rec = {"name": name, "schedule": s.get("schedule") or s.get("cadence_hours") or "every 4h",
               "legs": legs}
        if s.get("max_live_goals") is not None:
            rec["max_live"] = s["max_live_goals"]
        if s.get("rig"):
            rec["rig"] = s["rig"]
        found, _ = find_rail(board, user_id, name)
        if found is not None and rail_name(found) == name:
            kept.append(name)
            row = found
        else:
            row = create_rail(board, user_id, title=str(obj.get("title") or name),
                              body=str(obj.get("body") or ""), recurrence=rec,
                              created_by="import")
            created.append(name)
            if s.get("enabled") is False:
                board.set_objective_status(row.id, OBJ_PAUSED, user_id=user_id)
        base = set(base_tools(rec))
        from core.tool_grants import gated_ids
        for t in dict.fromkeys(tools):
            if t in base or t not in gated_ids() or not grantable_tool(t):
                continue
            grant_rail_tool(board.db_path, user_id=user_id, rail_id=row.id, tool_id=t,
                            granted_by=granted_by, note="imported — awaiting confirm",
                            pending=True)
            pending.append(f"{name}:{t}")
    return {"created": created, "kept": kept, "pending_grants": pending}


__all__ = [
    "RECURRENCE_KEY", "RailError", "RailsTicker", "base_tools", "build_rails_ticker",
    "create_rail", "export_manifest", "find_rail", "import_manifest", "is_rail",
    "last_seeded_at", "leg_tools", "list_rails", "next_seed_at", "rail_goals",
    "rail_is_due", "rail_live", "rail_name", "recurrence_of", "seed_due_rails",
    "seed_rail", "slug", "undeclared", "update_recurrence", "validate_recurrence",
]
