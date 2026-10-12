"""Status snapshot — the ONE builder behind every "how am I doing" surface.

2026-08-28: the owner's Telegram ``/status`` rendered "Goals: 0 open, 0 running
· kill switch: clear" while the primary provider was credit-dead, 91 of 92
owner notices had been suppressed by the daily cap, two asks were open (one
for 13 days) and a goal was blocked on the owner. Every line was individually
true; the message was a lie. Seven renderers (``/status``, ``/mode``,
``polyrob doctor``, ``polyrob autonomy status``, the webview ``/system`` page,
the daily digest and the agent's own ``agent_status`` action) each assembled
their own partial view, and each dropped a section silently when its read
failed — so "unknown" rendered exactly like "zero".

This module is the single source of truth those surfaces now render from.

Rules it enforces (the standing rule of the 2026-08-28 investigation):

- **Every section is typed and always present.** A section that cannot be
  computed is ``unavailable(<reason>)``, never absent, never a zero.
- **Health is mandatory and ranked first.** Credit sentinel per provider,
  whether any provider can serve, the delivery cap (suppressed notices), open
  asks, blocked goals, pending approvals, budget-exhausted objectives, dead
  loops, open surface circuits, tool timeouts, degraded run outcomes, the kill
  switch. Any item ⇒ the whole snapshot is ``degraded``; an unreadable health
  source ⇒ ``partial`` (we could not verify), never ``ok``.
- **Reads the durable telemetry log**, not just live objects — that is where
  the 24h truth lives (``telemetry_events.db``).
- **Tenant-scoped** (``user_id``) like every other store.
- **Cheap by default**: no network read unless ``include_balances=True``
  (mirrors ``build_ledger``).
- **Money figures are labelled.** Treasury is cash flow (income − spend,
  open positions NOT included); runtime cost is the owner's compute bill.
  The two are never summed (the two-ledgers rule).

Layering: this lives in ``core`` and must not import ``agents.*`` (the
layering ratchet's core→agents allowlist is shrink-only), so the goal board,
the cron store and the telemetry log are read through their SQLite files with
``core.sqlite_util`` — read-only, existence-guarded (a missing DB is reported,
never created). The literals are pinned against the owning modules'
constants by ``tests/unit/core/test_status_snapshot.py``.

W9 (2026-10-10): this file is the ASSEMBLER — the readers (``_rows``,
``_read_telemetry``), ``_build_core`` and ``build_status_snapshot``. The types
and literals live in ``core/status_types.py``; each section builder lives in a
``core/status_<part>.py`` module (session, work, rails, loops, delivery, rooms,
identity, plus the older tools/knowledge/rules/packs and the contributed
slots). Every name stays importable from here, and a section reads its stores
through ``core.status_snapshot._rows`` at call time.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from core.status_types import (  # noqa: E402,F401
    ASK_KIND_TOKEN_IDENTITY, HealthItem, MISSED_IN_SNAPSHOT, MISSED_TEXT_CHARS,
    OVERALL_DEGRADED, OVERALL_OK, OVERALL_PARTIAL, SECTION_ORDER, SEVERITY_CRIT,
    SEVERITY_WARN, STATE_DEGRADED, STATE_OK, STATE_UNAVAILABLE, Section, StatusSnapshot,
    _ASK_OPEN, _KIND_ASK, _KIND_GOAL, _KIND_OBJECTIVE, _K_AUTONOMY_STARTED,
    _K_AUTONOMY_TICK, _K_CREDIT_SENTINEL, _K_CRON_RUN, _K_GOAL_RUN, _K_OWNER_NOTICE,
    _K_RAIL_PRECONDITION_FAILED, _K_RUN_DEGRADED, _K_SELF_WAKE, _K_SURFACE_POLL_ERROR,
    _K_TOOL_DENIED, _K_TOOL_TIMEOUT, _K_USER_DELIVERY, _K_WALLET_SPEND, _OBJ_ACTIVE,
    _PAUSE_HONOURED_OUTCOMES, _ST_BLOCKED, _ST_READY, _ST_RUNNING, _ST_WAITING,
    _SUPPRESSED_PREFIX, _TOOL_APPROVAL_ASK_KIND, _age, _flag_command, _guarded, _hhmm,
    _payload, _tele_rows, _telemetry_db_path, _unavailable, has_own_surface,
)



# --- raw store readers (read-only, existence-guarded) -------------------------

def _rows(db_path: str, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Read-only query. Raises when the file is absent — the caller renders
    that as ``unavailable`` (a status view must never CREATE a store)."""
    if not db_path or not os.path.exists(db_path):
        raise FileNotFoundError(f"{os.path.basename(db_path or '?')} not found at {db_path}")
    from core.sqlite_util import execute_retry
    out = execute_retry(db_path, sql, params, fetch="all") or []
    return [dict(r) for r in out]


def _read_telemetry(user_id: str, data_dir: str, since_ts: float) -> Section:
    """One bounded read of the durable event log for the window; the other
    sections aggregate over ``data['rows']`` (memory_* kinds excluded — they
    are the bulk of the log and carry no health signal; 045: inbound_routed
    and access_denied excluded too — they are the highest-volume perimeter
    kinds under group load and would crowd out every other kind's rows in
    this 20000-row window. The security section counts them separately via
    core.security_digest's own GROUP BY query, not this fetch)."""
    path = _telemetry_db_path(data_dir)
    rows = _rows(
        path,
        "SELECT ts, kind, user_id, session_id, source, attrs FROM telemetry_events "
        "WHERE ts >= ? AND kind NOT IN "
        "('memory_write','memory_recall','inbound_routed','access_denied') "
        "AND (user_id = ? OR user_id = '') ORDER BY ts DESC LIMIT 20000",
        (float(since_ts), str(user_id)))
    for r in rows:
        try:
            r["attrs"] = json.loads(r.get("attrs") or "{}") or {}
        except Exception:
            r["attrs"] = {}
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    return Section(name="telemetry", data={"rows": rows, "counts": counts, "path": path})


# W9 (2026-10-10): the types, literals and section builders live in their own
# modules; this file stays the ONE assembler and re-exports every name (callers,
# tests and monkeypatch targets keep working).
from core.status_rails import (  # noqa: E402,F401
    _distinct_runs, _effect_violations, _email_tool_drop_note, _external_rail_health,
    _rail_cost_bits, _rail_gate_health, _rail_verdict_line, _rail_verdicts,
)
from core.status_session import (  # noqa: E402,F401
    _providers_section, _release_window, _session_section,
)
from core.status_work import (  # noqa: E402,F401
    _approvals_section, _objective_budget, _work_section,
)
from core.status_loops import (  # noqa: E402,F401
    _live_started_loops, _loops_section, _pid_alive,
)
from core.status_delivery import (  # noqa: E402,F401
    RECENT_NOTICE_WINDOW_SEC, _POLL_ERROR_WARN_AT, _delivery_section, _digest_health,
    _missed_bodies, _outbound_queue_db, _outbound_queue_lines, _recent_notices,
    _surface_poll_health,
)
from core.status_rooms import (  # noqa: E402,F401
    _apps_section, _groups_section,
)
from core.status_identity import (  # noqa: E402,F401
    X_OAUTH2_RELOGIN_REMEDY, _browser_rail_line, _identity_reach_lines,
    _identity_section, _posture_section, _security_section, _x_oauth2_relogin_text,
    _x_session_line, x_oauth2_line, x_oauth2_store_changed_since,
)


# --- assembly ----------------------------------------------------------------

def _assemble(user_id: str, now: float, window_sec: int, sections: Dict[str, Section]) -> StatusSnapshot:
    health: List[HealthItem] = []
    unavailable: List[str] = []
    for name in SECTION_ORDER:
        sec = sections[name]
        if not sec.available:
            unavailable.append(f"{name} ({sec.reason})")
        health.extend(sec.health)
    health.sort(key=lambda h: 0 if h.severity == SEVERITY_CRIT else 1)
    return StatusSnapshot(user_id=user_id, generated_at=now, window_sec=window_sec,
                          sections=sections, health=health, unavailable_sources=unavailable)


def _build_core(user_id: str, *, data_dir: Optional[str], task_agent: Any,
                session_id: Optional[str], container: Any, window_hours: int):
    """The sections the snapshot builds ITSELF (every SECTION_ORDER name that
    is not a ``core.status_sections`` slot)."""
    from core.instance import resolve_instance_id
    from core.status_knowledge import knowledge_section
    from core.status_tools import tools_section
    from core.status_packs import packs_section
    from core.status_rules import rules_section
    from core.runtime_paths import cron_db_path, data_dir_or_home, goals_db_path
    data_dir = data_dir_or_home(data_dir)
    now = time.time()
    window_sec = int(max(1, window_hours) * 3600)
    uid = str(user_id or "")
    tele = _guarded("telemetry", _read_telemetry, uid, data_dir, now - window_sec)
    goals_db, cron_db = goals_db_path(data_dir), cron_db_path(data_dir)
    if container is None:
        container = getattr(task_agent, "container", None)
    sections: Dict[str, Section] = {
        "session": _guarded("session", _session_section, task_agent, session_id),
        "providers": _guarded("providers", _providers_section, tele, now),
        "work": _guarded("work", _work_section, uid, goals_db, tele, now),
        "approvals": _guarded("approvals", _approvals_section, uid, data_dir, goals_db),
        "loops": _guarded("loops", _loops_section, uid, cron_db, tele, now, goals_db, data_dir),
        "tools": _guarded("tools", tools_section, uid, data_dir, now),
        # What it KNOWS, beside what it can DO (the 2026-09-22 knowledge
        # review: nineteen sections and none of them was this one).
        "knowledge": _guarded("knowledge", knowledge_section, uid, data_dir, now),
        "delivery": _guarded("delivery", _delivery_section, uid, data_dir, tele, container, now,
                             session_id),
        "posture": _guarded("posture", _posture_section),
        "security": _guarded("security", _security_section, uid, data_dir, now, window_sec),
        "identity": _guarded("identity", _identity_section,
                             resolve_instance_id(), data_dir),
        # 060 WS-7: is a written rule IN EFFECT? (per instruction surface)
        "rules": _guarded("rules", rules_section, uid, data_dir, cron_db, tele),
        "apps": _guarded("apps", _apps_section, uid, data_dir),
        "groups": _guarded("groups", _groups_section, uid, data_dir),
        # 067 P2: installed / loaded / disabled / refused packs, with reasons.
        "packs": _guarded("packs", packs_section),
    }
    if not uid:
        for name in ("work", "approvals", "loops", "tools", "knowledge", "delivery", "rules",
                     "apps", "security"):
            sections[name] = Section(name=name, state=STATE_UNAVAILABLE,
                                     reason="no tenant (empty user_id)")
    return uid, data_dir, now, window_sec, sections


def build_status_snapshot(user_id: str, *, data_dir: Optional[str] = None,
                          task_agent: Any = None, session_id: Optional[str] = None,
                          container: Any = None, include_balances: bool = False,
                          include_money: bool = True, window_hours: int = 24,
                          ledger: Any = None, liquidity_enumerate_fn=None) -> StatusSnapshot:
    """The ONE builder. Synchronous: doctor / CLI / the per-turn agent note call
    it directly; async surfaces (Telegram, webview, the agent action) run it in
    a worker thread (``asyncio.to_thread``) so the event loop stays free. The
    money section is read through the sync bridge (``core.activity_evidence``)
    — a network balance probe only when ``include_balances``; ``ledger`` lets a
    caller that already holds a ``build_ledger`` result (or the exception it
    raised) pass it in.

    067 P5a: the SLOTS (``core.status_sections.CONTRIBUTED_SLOTS``: wallet,
    custody, money, economics, liquidity, collectibles, creations,
    room_actions) come from their registered providers, built in SECTION_ORDER
    (money before economics: the runway line reads the ledger money read); an
    empty slot is one ``not installed`` line."""
    from core.status_sections import CONTRIBUTED_SLOTS, SectionContext, build_slot
    uid, resolved_dir, now, window_sec, core_sections = _build_core(
        user_id, data_dir=data_dir, task_agent=task_agent, session_id=session_id,
        container=container, window_hours=window_hours)
    ctx = SectionContext(uid=uid, data_dir=resolved_dir, now=now, window_sec=window_sec,
                         include_money=include_money, include_balances=include_balances,
                         ledger=ledger, liquidity_reader=liquidity_enumerate_fn)
    sections: Dict[str, Section] = {}
    # ⚠️ Insertion order IS the render order — `test_every_section_is_always_present`
    # asserts `tuple(snap.sections) == SECTION_ORDER`.
    for name in SECTION_ORDER:
        sections[name] = (build_slot(name, ctx) if name in CONTRIBUTED_SLOTS
                          else core_sections[name])
    return _assemble(uid, now, window_sec, sections)


#: 067 P5a: the builders that moved to the slot provider modules, still
#: importable from here (callers and monkeypatch targets keep working).
_MOVED = {
    "_positions_line": "core.status_money",
    "_money_section": "core.status_money",
    "CREATION_VERBS": "core.status_money",
    "_creations_section": "core.status_money",
    "moves_section": "core.status_money",
    "_collectibles_section": "core.status_money",
}


def __getattr__(name: str):
    module = _MOVED.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value
