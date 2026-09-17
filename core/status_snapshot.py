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
never created). The literals below are pinned against the owning modules'
constants by ``tests/unit/core/test_status_snapshot.py``.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STATE_OK = "ok"
STATE_DEGRADED = "degraded"
STATE_UNAVAILABLE = "unavailable"

OVERALL_OK = "ok"
OVERALL_DEGRADED = "degraded"
OVERALL_PARTIAL = "partial"

SEVERITY_CRIT = "crit"
SEVERITY_WARN = "warn"

# Goal-board literals — the ONE spelling (core.goal_vocab; the board imports
# the same names, and the contract test still pins them equal).
from core.goal_vocab import (  # noqa: E402
    KIND_GOAL as _KIND_GOAL, KIND_OBJECTIVE as _KIND_OBJECTIVE, KIND_ASK as _KIND_ASK,
    STATUS_READY as _ST_READY, STATUS_RUNNING as _ST_RUNNING,
    STATUS_BLOCKED as _ST_BLOCKED, STATUS_WAITING as _ST_WAITING,
    STATUS_TRIAGE as _ST_TRIAGE, STATUS_DONE as _ST_DONE,
    STATUS_CANCELLED as _ST_CANCELLED, ASK_OPEN as _ASK_OPEN, OBJ_ACTIVE as _OBJ_ACTIVE,
)
_TOOL_APPROVAL_ASK_KIND = "tool_approval"
# Telemetry kinds (pinned to core/event_kinds.py by the contract test).
_K_USER_DELIVERY, _K_OWNER_NOTICE = "user_delivery", "owner_notice"
_K_CREDIT_SENTINEL, _K_CRON_RUN, _K_GOAL_RUN = "credit_sentinel", "cron_run", "goal_run"
_K_SELF_WAKE, _K_TOOL_TIMEOUT, _K_TOOL_DENIED = "self_wake", "tool_timeout", "tool_denied"
_K_RUN_DEGRADED, _K_AUTONOMY_TICK = "run_outcome_degraded", "autonomy_tick"
_K_SOCIAL_WRITE, _K_WALLET_SPEND = "social_write", "wallet_spend"
_K_AUTONOMY_STARTED = "autonomy_started"
_K_SURFACE_POLL_ERROR = "surface_poll_error"
#: 031: outcomes that mean "the starter honoured the pause" (not a violation)
_PAUSE_HONOURED_OUTCOMES = frozenset({"paused", "skipped", "held", "dropped"})
_SUPPRESSED_PREFIX = "[suppressed by daily proactive-message cap"

#: ⚠️ The aggregation gate, not just a display order. `_assemble` iterates THIS
#: tuple, so a section missing from it contributes NO health item and reports no
#: unavailability — it is present in `sections` and silent everywhere that
#: matters. A new section belongs here in the same commit that adds it.
SECTION_ORDER = ("session", "providers", "work", "approvals", "loops",
                 "delivery", "posture", "security", "identity", "apps",
                 "groups", "room_actions", "creations", "collectibles", "liquidity",
                 "wallet", "money")


@dataclass
class HealthItem:
    key: str
    text: str
    remedy: str = ""
    severity: str = SEVERITY_WARN


@dataclass
class Section:
    name: str
    state: str = STATE_OK
    reason: str = ""
    lines: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)
    health: List[HealthItem] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.state != STATE_UNAVAILABLE


@dataclass
class StatusSnapshot:
    user_id: str
    generated_at: float
    window_sec: int
    sections: Dict[str, Section]
    health: List[HealthItem]
    unavailable_sources: List[str]

    @property
    def overall(self) -> str:
        if self.health:
            return OVERALL_DEGRADED
        if self.unavailable_sources:
            return OVERALL_PARTIAL
        return OVERALL_OK

    def section(self, name: str) -> Section:
        return self.sections[name]


def _unavailable(name: str, exc: BaseException) -> Section:
    reason = f"{type(exc).__name__}: {str(exc)[:160]}".strip()
    return Section(name=name, state=STATE_UNAVAILABLE, reason=reason)


def _guarded(name: str, fn, *args, **kwargs) -> Section:
    try:
        sec = fn(*args, **kwargs)
        if sec.health and sec.state == STATE_OK:
            sec.state = STATE_DEGRADED
        return sec
    except Exception as e:  # every reader failure becomes a typed, visible state
        logger.debug("status_snapshot: section %s unavailable: %s", name, e, exc_info=True)
        return _unavailable(name, e)


def _hhmm(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    return time.strftime("%m-%d %H:%MZ", time.gmtime(float(ts)))


def _age(ts: Optional[float], now: float) -> str:
    if not ts:
        return "?"
    d = max(0.0, now - float(ts))
    if d < 3600:
        return f"{int(d // 60)}m"
    if d < 86400:
        return f"{d / 3600:.1f}h"
    return f"{d / 86400:.1f}d"


# --- raw store readers (read-only, existence-guarded) -------------------------

def _rows(db_path: str, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Read-only query. Raises when the file is absent — the caller renders
    that as ``unavailable`` (a status view must never CREATE a store)."""
    if not db_path or not os.path.exists(db_path):
        raise FileNotFoundError(f"{os.path.basename(db_path or '?')} not found at {db_path}")
    from core.sqlite_util import execute_retry
    out = execute_retry(db_path, sql, params, fetch="all") or []
    return [dict(r) for r in out]


def _pid_alive(pid: int) -> bool:
    """Is *pid* a live process on THIS host? Fail-OPEN — unknown means alive.

    ``os.kill(pid, 0)`` sends no signal; it asks the kernel whether the process
    exists. ``ProcessLookupError`` is the ONLY answer that means gone:
    ``PermissionError`` means it exists under another uid (prod runs several
    units), and anything else is a platform we cannot ask. Both keep the row,
    because dropping one narrows the set of loops anybody checks, and narrowing
    on a guess is exactly the silent failure this read exists to prevent.
    """
    try:
        pid = int(pid)
    except Exception:
        return True
    if pid <= 0:
        # `os.kill(0, 0)` addresses the whole process GROUP, and a negative pid
        # a group by id. Neither is a process, so neither is probed.
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def _live_started_loops(rows: List[Dict[str, Any]]) -> tuple[Optional[List[str]], int]:
    """``(loops, unreadable_rows)`` — the loops every LIVE process started (043 T3).

    *rows* are ``autonomy_started`` rows, NEWEST FIRST. One row is one process
    start, so the newest row is simply the last process to boot — which on a
    local-mode box is routinely a short-lived `rob` REPL, not the service. Take
    the newest row PER PID, drop a pid the kernel says is gone, and union what
    survives. A row with no usable pid is legacy (written before the field
    existed) and is always kept: it cannot be attributed, and an unattributable
    row is not evidence that a loop stopped.

    The loops are ``None`` when there is no row at all, which the caller renders
    as the legacy gate-derived fallback. A row whose ``attrs`` cannot be parsed
    is COUNTED and returned, never merely skipped: it means some process's loops
    are not being checked, and that is the exact silent narrowing this read
    exists to end.

    ⚠️ Residual, accepted: pids are recycled and are meaningless across hosts
    (an ops read of a COPIED db). Recycling can only keep a stale row, which
    WIDENS the expected set — at worst an extra loop is reported silent, never
    a live loop left unchecked. A copied db can narrow; that read is rare and
    the health block still says what was checked.
    """
    newest_per_pid: Dict[Any, List[str]] = {}
    unreadable = 0
    for r in rows or []:
        try:
            attrs = json.loads(r.get("attrs") or "{}") or {}
            if not isinstance(attrs, dict):
                raise ValueError("attrs is not an object")
        except Exception:
            # Counted, never merely skipped: a row we cannot read is a process
            # whose loops nobody is checking, and the caller says so out loud.
            unreadable += 1
            continue
        pid = attrs.get("pid")
        key = pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None
        if key in newest_per_pid:
            continue                      # rows are newest-first; first wins
        newest_per_pid[key] = [str(x) for x in (attrs.get("loops") or [])]
    if not newest_per_pid:
        return (None, unreadable)
    out: set = set()
    for key, loops in newest_per_pid.items():
        if key is None or _pid_alive(key):
            out.update(loops)
    return (sorted(out), unreadable)


def _telemetry_db_path(data_dir: str) -> str:
    from core.event_log import telemetry_db_path
    return telemetry_db_path(data_dir)


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
        "SELECT ts, kind, user_id, source, attrs FROM telemetry_events "
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


def _tele_rows(tele: Section, kind: str) -> List[Dict[str, Any]]:
    return [r for r in (tele.data.get("rows") or []) if r.get("kind") == kind]


# --- sections ----------------------------------------------------------------

def _session_section(task_agent: Any, session_id: Optional[str]) -> Section:
    sec = Section(name="session")
    if not session_id:
        sec.lines.append("no active session")
        sec.data["bound"] = False
        return sec
    short = session_id[:12] + ("…" if len(session_id) > 12 else "")
    sec.data.update({"bound": True, "session_id": session_id})
    get_orch = getattr(task_agent, "get_orchestrator", None)
    orch = get_orch(session_id) if callable(get_orch) else None
    if orch is None:
        sec.lines.append(f"bound ({short}) — idle (not resident)")
        sec.data["state"] = "idle"
        return sec
    # Real liveness: run_session holds the per-session execution lock for the
    # whole run, so `locked()` means "a step is executing right now". Queued
    # input (the old signal) is a different fact and is reported as such.
    locks = getattr(task_agent, "_session_execution_locks", None)
    lock = locks.get(session_id) if isinstance(locks, dict) else None
    has_pending = getattr(task_agent, "_session_has_pending_input", None)
    pending = bool(has_pending(session_id)) if callable(has_pending) else False
    if lock is not None:
        state = "running" if lock.locked() else ("queued input" if pending else "idle")
    else:
        state = "state unknown (no execution lock)" if not pending else "queued input"
    extra = []
    agent_obj = next(iter((getattr(orch, "agents", None) or {}).values()), None)
    if agent_obj is not None:
        st = getattr(agent_obj, "state", None)
        n_steps = getattr(st, "n_steps", None)
        if state == "running" and n_steps is not None:
            state = f"running (step {n_steps})"
        model = getattr(agent_obj, "model_name", None)
        if model:
            extra.append(f"model={model}")
        mm = getattr(agent_obj, "message_manager", None)
        if mm is not None and hasattr(mm, "get_context_usage_percent"):
            pct = mm.get_context_usage_percent()
            if pct is not None:
                extra.append(f"ctx={pct:.0f}%")
    sec.data["state"] = state
    sec.lines.append(f"bound ({short}) — {state}" + (" " + " ".join(extra) if extra else ""))
    return sec


def _providers_section(tele: Section, now: float) -> Section:
    sec = Section(name="providers")
    from core.credit_sentinel import credit_sentinel_enabled, credit_sentinel_status
    latched = credit_sentinel_status()  # raises => whole section unavailable (honest)
    sec.data["sentinel"] = latched
    sec.data["sentinel_enabled"] = credit_sentinel_enabled()
    from core.runtime_config import (operator_provider_pin, resolve_live_provider,
                                     usable_providers_with_credentials)
    try:
        usable = list(usable_providers_with_credentials())
    except Exception:
        usable = []
    pin = operator_provider_pin()
    live = resolve_live_provider(pin)
    sec.data.update({"usable": usable, "pin": pin, "live": live})
    for name, entry in sorted(latched.items()):
        who = "ALL providers" if name == "*" else name
        blocks_live_provider = name == "*" or name == live
        release = entry.get("release_ts")
        until = (f"until ~{_hhmm(release)}" if release
                 else f"auto-release {_hhmm(float(entry.get('ts') or 0) + _release_window())}")
        reason = (entry.get("reason") or "").strip().replace("\n", " ")[:140]
        serving_note = ""
        if not blocks_live_provider and live:
            serving_note = f"; does NOT block live provider {live}"
        sec.health.append(HealthItem(
            key=f"credit_sentinel:{name}",
            severity=SEVERITY_CRIT if blocks_live_provider else SEVERITY_WARN,
            text=f"credit sentinel TRIPPED for {who} since {_hhmm(entry.get('ts'))} "
                 f"({until})" + (f": {reason}" if reason else "") + serving_note,
            remedy="top up the account, or remove <data>/CREDIT_SENTINEL after topping up"))
    if live is None and usable:
        sec.health.append(HealthItem(
            key="no_live_provider", severity=SEVERITY_CRIT,
            text="NO configured provider can serve (all credit-dead) — every paid tick is skipped",
            remedy="add credits or connect another provider (`polyrob auth add`)"))
    line = f"live provider: {live or 'NONE'}"
    if pin:
        line += f" (pin {pin}{' — dead, rerouted' if pin != live else ''})"
    line += f"; credentialed: {', '.join(usable) if usable else 'none detected'}"
    if not sec.data["sentinel_enabled"]:
        line += "; credit sentinel DISABLED"
    sec.lines.append(line)
    if tele.available:
        trips = len(_tele_rows(tele, _K_CREDIT_SENTINEL))
        cron = _tele_rows(tele, _K_CRON_RUN)
        rerouted = sum(1 for r in cron if (r["attrs"] or {}).get("outcome") == "provider_rerouted")
        skipped = sum(1 for r in cron if (r["attrs"] or {}).get("outcome") == "skipped"
                      and (r["attrs"] or {}).get("reason") == "credit_sentinel")
        sec.data.update({"trips_window": trips, "rerouted_window": rerouted,
                         "sentinel_skips_window": skipped})
        bits = [f"{trips} sentinel trip(s)", f"{rerouted} cron run(s) rerouted off the pin"]
        if skipped:
            bits.append(f"{skipped} tick(s) skipped as credit-dead")
        sec.lines.append("24h: " + ", ".join(bits))
        if rerouted and not latched:
            sec.health.append(HealthItem(
                key="provider_rerouted", severity=SEVERITY_WARN,
                text=f"{rerouted} cron run(s) in 24h had to reroute off the pinned provider",
                remedy="check the pinned provider's credits / DEFAULT_PROVIDER"))
    else:
        sec.lines.append(f"24h trip/reroute counts unavailable ({tele.reason})")
    return sec


def _release_window() -> float:
    try:
        from core.credit_sentinel import _release_hours
        return float(_release_hours()) * 3600
    except Exception:
        return 6 * 3600


def _objective_budget(payload: Dict[str, Any]) -> int:
    """Mirror of ``GoalBoard.objective_budget`` (a stream is uncapped; own
    ``goal_budget`` wins; else OBJECTIVE_GOAL_BUDGET, default 25)."""
    if payload.get("stream_id"):
        return 0
    own = str(payload.get("goal_budget") if payload.get("goal_budget") is not None else "").strip()
    if own.lstrip("-").isdigit():
        return max(0, int(own))
    from core.env import int_env
    return int_env("OBJECTIVE_GOAL_BUDGET", 25)


def _payload(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(row.get("payload") or "{}") or {}
    except Exception:
        return {}


def _work_section(user_id: str, goals_db: str, tele: Section, now: float) -> Section:
    sec = Section(name="work")
    counts = {r["status"]: int(r["n"]) for r in _rows(
        goals_db, "SELECT status, COUNT(*) AS n FROM goals WHERE kind=? AND user_id=? "
                  "GROUP BY status", (_KIND_GOAL, user_id))}
    ready, running = counts.get(_ST_READY, 0), counts.get(_ST_RUNNING, 0)
    blocked_n, waiting = counts.get(_ST_BLOCKED, 0), counts.get(_ST_WAITING, 0)
    sec.data["counts"] = counts
    # Open asks (tool-approval asks belong to the approvals section).
    asks = []
    for r in _rows(goals_db, "SELECT id, title, body, payload, created_at FROM goals "
                             "WHERE kind=? AND status=? AND user_id=? ORDER BY created_at",
                   (_KIND_ASK, _ASK_OPEN, user_id)):
        p = _payload(r)
        if p.get("ask_kind") == _TOOL_APPROVAL_ASK_KIND:
            continue
        asks.append({"id": r["id"], "title": r["title"], "created_at": r["created_at"],
                     "blocks": list(p.get("blocks_goal_ids") or [])})
    sec.data["open_asks"] = asks
    blocked = [{"id": r["id"], "title": r["title"],
                "failures": int(r.get("consecutive_failures") or 0),
                "max_retries": int(r.get("max_retries") or 0),
                "error": (r.get("last_failure_error") or "")[:160]}
               for r in _rows(goals_db, "SELECT id, title, consecutive_failures, max_retries, "
                                        "last_failure_error FROM goals WHERE kind=? AND status=? "
                                        "AND user_id=? ORDER BY created_at",
                              (_KIND_GOAL, _ST_BLOCKED, user_id))]
    sec.data["blocked"] = blocked
    objectives = []
    for r in _rows(goals_db, "SELECT id, title, payload FROM goals WHERE kind=? AND status=? "
                             "AND user_id=? ORDER BY priority DESC, created_at",
                   (_KIND_OBJECTIVE, _OBJ_ACTIVE, user_id)):
        p = _payload(r)
        budget = _objective_budget(p)
        live = int(_rows(goals_db, "SELECT COUNT(*) AS n FROM goals WHERE kind=? AND user_id=? "
                                   "AND parent_id=? AND status NOT IN ('cancelled','dropped')",
                         (_KIND_GOAL, user_id, r["id"]))[0]["n"])
        objectives.append({"id": r["id"], "title": r["title"], "budget": budget,
                           "live": live, "stream": bool(p.get("stream_id")),
                           "exhausted": bool(budget and live >= budget)})
    sec.data["objectives"] = objectives
    exhausted = [o for o in objectives if o["exhausted"]]
    line = (f"{ready} ready, {running} running, {blocked_n} blocked, {waiting} waiting · "
            f"{len(objectives)} objective(s) active")
    if exhausted:
        line += f" ({len(exhausted)} at lifetime goal budget)"
    sec.lines.append(line)
    if tele.available:
        runs = _tele_rows(tele, _K_GOAL_RUN)
        by = {}
        for r in runs:
            o = (r["attrs"] or {}).get("outcome") or "?"
            by[o] = by.get(o, 0) + 1
        sec.data["runs_window"] = by
        sec.lines.append("24h goal runs: " + (", ".join(f"{k}={v}" for k, v in sorted(by.items()))
                                             or "none"))
    else:
        sec.lines.append(f"24h goal-run counts unavailable ({tele.reason})")
    for a in asks:
        age = _age(a["created_at"], now)
        blocks = f", blocks {len(a['blocks'])} goal(s)" if a["blocks"] else ""
        sec.health.append(HealthItem(
            key=f"ask:{a['id']}", severity=SEVERITY_CRIT if a["blocks"] else SEVERITY_WARN,
            text=f"open ask ({age} old{blocks}): {a['title'][:110]}",
            remedy=f"/asks · /fulfill {a['id']} once done"))
    for b in blocked:
        sec.health.append(HealthItem(
            key=f"blocked:{b['id']}", severity=SEVERITY_WARN,
            text=f"goal BLOCKED ({b['failures']}/{b['max_retries']} failures): {b['title'][:100]}",
            remedy=f"/goal retry {b['id']} after the cause is fixed, or /goal cancel {b['id']}"))
    for o in exhausted:
        sec.health.append(HealthItem(
            key=f"objective_budget:{o['id']}", severity=SEVERITY_WARN,
            text=f"objective at lifetime goal budget ({o['live']}/{o['budget']}) — the planner "
                 f"can open no more work on it: {o['title'][:80]}",
            remedy=f"/goal objective drop {o['id']} if finished, or raise its goal_budget"))
    return sec


def _approvals_section(user_id: str, data_dir: str, goals_db: str) -> Section:
    sec = Section(name="approvals")
    items: List[Dict[str, Any]] = []
    tool_asks = [r for r in _rows(goals_db, "SELECT id, title, payload FROM goals WHERE kind=? "
                                            "AND status=? AND user_id=?",
                                  (_KIND_ASK, _ASK_OPEN, user_id))
                 if _payload(r).get("ask_kind") == _TOOL_APPROVAL_ASK_KIND]
    for r in tool_asks:
        items.append({"kind": _TOOL_APPROVAL_ASK_KIND, "id": f"tap-{r['id']}",
                      "preview": (_payload(r).get("tool_name") or r["title"] or "")[:80]})
    from core import self_evolution
    from core.instance import resolve_instance_id
    for it in self_evolution.list_pending(user_id, home_dir=data_dir,
                                          instance_id=resolve_instance_id()) or []:
        items.append({"kind": it.get("kind"), "id": it.get("id"),
                      "preview": (it.get("preview") or "")[:80]})
    corr_db = os.path.join(data_dir, "correspondents.db")
    if os.path.exists(corr_db):
        from core.surfaces.correspondents import CorrespondentRegistry
        from core.surfaces.owner_admin import pending_correspondent_items
        for it in pending_correspondent_items(CorrespondentRegistry(corr_db), user_id):
            items.append({"kind": it.get("kind"), "id": it.get("id"),
                          "preview": (it.get("preview") or "")[:80]})
    sec.data["items"] = items
    sec.lines.append(f"{len(items)} pending approval(s)" if items else "no pending approvals")
    if items:
        kinds = sorted({str(i.get("kind")) for i in items})
        sec.health.append(HealthItem(
            key="pending_approvals", severity=SEVERITY_CRIT,
            text=f"{len(items)} decision(s) waiting on you ({', '.join(kinds)})",
            # 2026-09-15: `/approve <id>` made the reader copy an id off a
            # phone — a chat client links the verb and not its argument. The
            # tappable token per item lives in `/pending`; the whole queue is
            # one token of its own.
            remedy="/pending · /approve_all · /reject_all"))
    return sec


def _loops_section(user_id: str, cron_db: str, tele: Section, now: float,
                   goals_db: Optional[str] = None, data_dir: Optional[str] = None) -> Section:
    sec = Section(name="loops")
    from core.autonomy_control import read_state
    from core.config_policy import AutonomyConfig
    # 031: the ONE pause record (fail-closed read, the caller's data dir first).
    # Rendered FIRST on every seat (status_render.pause_headline) and as a health
    # item; an autonomous activity recorded AFTER the pause is a violation.
    st = read_state(data_dir)
    sec.data["pause"] = st.to_dict()
    sec.data["halted"] = bool(st.paused and "all" in st.scopes)
    if st.paused:
        who = f"{st.set_by} via {st.via}" if st.set_by else st.source
        sec.health.append(HealthItem(
            key="paused", severity=SEVERITY_WARN,
            text=f"autonomy PAUSED ({', '.join(st.scopes)}) since {_hhmm(st.since)} by {who}",
            remedy="/resume when you want it back"))
        if tele.available and st.since:
            kinds = {_K_GOAL_RUN: "goal_run", _K_CRON_RUN: "cron_run", _K_SELF_WAKE: "self_wake",
                     _K_SOCIAL_WRITE: "social_write", _K_WALLET_SPEND: "wallet_spend"}
            seen = []
            for k, label in kinds.items():
                rows = [r for r in _tele_rows(tele, k)
                        if float(r.get("ts") or 0) > float(st.since)
                        and (r.get("attrs") or {}).get("outcome") not in _PAUSE_HONOURED_OUTCOMES]
                if rows:
                    seen.append(f"{label} ×{len(rows)} (last {_hhmm(rows[0].get('ts'))})")
            if seen:
                sec.health.append(HealthItem(
                    key="pause_violation", severity=SEVERITY_CRIT,
                    text="PAUSE VIOLATED — autonomous activity after the pause: " + "; ".join(seen),
                    remedy="check `journalctl -u polyrob.service` for the session; file an incident"))
    # Live actors for the headline. A missing goal board is a real fact about a
    # fresh install, not a failure of THIS section: report it as unknown with
    # its reason rather than making the whole loops section unreadable.
    live: Dict[str, Any] = {"running_goals": None, "running_cron": None, "loops_alive": {}}
    try:
        rg = _rows(goals_db or "", "SELECT COUNT(*) AS n FROM goals WHERE status='running' "
                                   "AND kind='goal' AND user_id=?", (user_id,))
        live["running_goals"] = int((rg[0] or {}).get("n") or 0) if rg else 0
    except Exception as e:
        live["goals_reason"] = f"{type(e).__name__}: {str(e)[:80]}"
    try:
        live["running_cron"] = len(_rows(
            cron_db, "SELECT id FROM cron_jobs WHERE status='running' AND user_id=?", (user_id,)))
    except Exception as e:
        live["cron_reason"] = f"{type(e).__name__}: {str(e)[:80]}"
    sec.data["live_actors"] = live
    # The cron store may not exist yet (fresh install, or a posture without
    # cron). That is a fact about cron, not about the pause state above: report
    # it inside the section rather than making the whole section unreadable.
    try:
        jobs = _rows(cron_db, "SELECT id, task, enabled, status, next_run_at, last_run_at, "
                              "payload FROM cron_jobs WHERE user_id=?", (user_id,))
    except Exception as e:
        sec.data["cron_reason"] = f"{type(e).__name__}: {str(e)[:120]}"
        sec.lines.append(f"cron: unavailable ({sec.data['cron_reason']})")
        jobs = []
    if "cron_reason" not in sec.data:
        enabled = [j for j in jobs if int(j.get("enabled") or 0)]
        upcoming = sorted(j["next_run_at"] for j in enabled if j.get("next_run_at"))
        sec.data.update({"cron_total": len(jobs), "cron_enabled": len(enabled),
                         "next_cron": upcoming[0] if upcoming else None})
        line = f"cron: {len(enabled)} enabled / {len(jobs) - len(enabled)} disabled"
        if upcoming:
            line += f"; next {upcoming[0][:16]}"
        sec.lines.append(line)
    if tele.available:
        cron_runs = _tele_rows(tele, _K_CRON_RUN)
        by = {}
        for r in cron_runs:
            o = (r["attrs"] or {}).get("outcome") or "?"
            by[o] = by.get(o, 0) + 1
        wakes = len(_tele_rows(tele, _K_SELF_WAKE))
        sec.data.update({"cron_runs_window": by, "self_wakes_window": wakes})
        sec.lines.append("24h: cron " + (", ".join(f"{k}={v}" for k, v in sorted(by.items()))
                                         or "no runs") + f"; self-wakes={wakes}")
        # Loop liveness from the supervisor heartbeat (autonomy_tick). A loop
        # that should run but has no fresh heartbeat is reported as such — a
        # dead ticker must never render identically to a healthy one.
        latest: Dict[str, Dict[str, Any]] = {}
        for r in _tele_rows(tele, _K_AUTONOMY_TICK):  # rows are newest-first
            loop = (r["attrs"] or {}).get("loop") or "?"
            latest.setdefault(loop, {"ts": r["ts"], "alive": (r["attrs"] or {}).get("alive")})
        sec.data["heartbeats"] = latest
        sec.data["live_actors"]["loops_alive"] = {
            loop: bool(hb.get("alive")) for loop, hb in latest.items()}
        gate_expected = []
        try:
            from core.autonomy_runtime import _cron_enabled  # the runtime's own gate
            if _cron_enabled():
                gate_expected.append("cron")
        except Exception as e:
            sec.lines.append(f"cron gate unreadable ({type(e).__name__}) — cron liveness not checked")
        if AutonomyConfig.goals_enabled():
            gate_expected.append("goals")
        # 043 A8/A42: the runtime records which loops it actually started
        # (`autonomy_started`) — up to 8 (cron/goals/curator/sandbox_reap/
        # surface_gc/quiet_release/settlement/bridges), not just the two this
        # section can gate-derive on its own. Union with the gate-derived pair
        # (never narrower than the legacy check) and fall back to gate-derived
        # alone when no such row exists yet (an old prod box that predates this
        # record, or a process that hasn't started).
        #
        # ⚠️ 043 T3: read the recent rows and union the LIVE PROCESSES, never
        # "whoever wrote last". Each row is one process start, so on a box
        # where the owner opens a `rob` REPL beside the service — what local
        # mode is FOR — the REPL's newer row named its own loops and the
        # service's curator/settlement/bridge checks silently stopped
        # happening. See `_live_started_loops`.
        #
        # ⚠️ Read UNBOUNDED here, NOT via `_tele_rows(tele, ...)` — `tele` is
        # the 24h-windowed read (`_read_telemetry`, `since_ts=now-window`) and
        # `autonomy_started` is written exactly ONCE per process start, so
        # after 24h of uptime (prod's normal state) it falls out of the
        # window and `expected` would silently decay back to cron/goals with
        # no signal anything narrowed. A raw newest-row query (mirrors
        # `_creations_section`) has no such horizon. An unreadable store keeps
        # today's fallback AND raises a health item — the narrowing must
        # never be silent.
        #
        # ⚠️ The window is ONE ROW PER PROCESS, never the newest N rows and
        # never a time slice — both of those lose a LIVE process's row:
        #   * a count window (this was `LIMIT 20`) is evicted by process
        #     STARTS, so a couple of dozen short `rob` REPL runs push the
        #     service's own row out and `expected` narrows back to
        #     gate-derived with no signal — a quieter copy of the very defect
        #     this read exists to fix;
        #   * a time floor is worse, and is exactly the horizon the paragraph
        #     above forbids: one row is written per process start, so a
        #     service up longer than the window (prod's normal state) falls
        #     out of it.
        # Collapsing by pid is the only bound that cannot evict a living
        # process: a thousand REPL runs contribute a thousand groups, and the
        # months-old service row is still its own group's newest.
        #
        # The GROUP BY collapses ONLY rows that carry a pid. Everything else —
        # a legacy pid-less row, unparseable text, a JSON array, NULL — is its
        # own group and reaches Python untouched, so no row `_live_started_loops`
        # would keep is lost in SQL and the unreadable COUNT stays exact. The
        # grouping RULE lives in Python (one implementation); this is a cost
        # pre-filter that agrees with it, not a second copy of it. `LIMIT` is
        # a backstop on distinct PROCESSES, not on rows.
        started_loops = None
        try:
            db = _telemetry_db_path(data_dir)
            started_rows = _rows(
                db, "SELECT MAX(ts) AS ts, attrs FROM telemetry_events "
                    "WHERE kind=? GROUP BY CASE WHEN json_valid(attrs) "
                    "AND json_type(attrs)='object' "
                    "AND json_extract(attrs,'$.pid') IS NOT NULL "
                    "THEN 'pid:' || json_extract(attrs,'$.pid') "
                    "ELSE 'row:' || ts END "
                    "ORDER BY ts DESC LIMIT 500", (_K_AUTONOMY_STARTED,))
            started_loops, unreadable_rows = _live_started_loops(started_rows)
            if unreadable_rows:
                sec.health.append(HealthItem(
                    key="loops_expected_partial", severity=SEVERITY_WARN,
                    text=f"{unreadable_rows} autonomy_started row(s) could not be "
                         f"parsed — whatever loops those processes started are NOT "
                         f"being checked for liveness",
                    remedy="inspect telemetry_events.db for corrupt autonomy_started attrs"))
        except Exception as e:
            sec.health.append(HealthItem(
                key="loops_expected_unreadable", severity=SEVERITY_WARN,
                text=f"could not read the autonomy_started record ({type(e).__name__}) — "
                     f"loop liveness is narrowed to cron/goals only, not the full set this "
                     f"process actually started",
                remedy="check telemetry_events.db exists and is readable"))
        if started_loops is not None:
            expected = sorted(set(started_loops) | set(gate_expected))
        else:
            expected = gate_expected
        from core.env import int_env
        stale_after = max(900, 2 * int_env("AUTONOMY_HEARTBEAT_INTERVAL_SEC", 300))
        hb_bits = []
        for loop in expected:
            hb = latest.get(loop)
            if hb is None:
                hb_bits.append(f"{loop}: no heartbeat in 24h")
                sec.health.append(HealthItem(
                    key=f"loop_silent:{loop}", severity=SEVERITY_WARN,
                    text=f"{loop} loop: no liveness heartbeat recorded in 24h (dead, or "
                         f"this process does not emit heartbeats)",
                    remedy="check the service log; restart if the loop task exited"))
            elif hb.get("alive") is False or now - float(hb["ts"]) > stale_after:
                hb_bits.append(f"{loop}: last heartbeat {_age(hb['ts'], now)} ago"
                               + ("" if hb.get("alive") else " (task exited)"))
                sec.health.append(HealthItem(
                    key=f"loop_silent:{loop}", severity=SEVERITY_WARN,
                    text=f"{loop} loop looks DEAD — last heartbeat {_age(hb['ts'], now)} ago"
                         + ("" if hb.get("alive") else " (task exited)"),
                    remedy="restart the service (`systemctl restart polyrob`)"))
            else:
                hb_bits.append(f"{loop}: alive ({_age(hb['ts'], now)} ago)")
        if hb_bits:
            sec.lines.append("loops: " + "; ".join(hb_bits))
    else:
        sec.lines.append(f"24h run/heartbeat counts unavailable ({tele.reason})")
    return sec


#: Poll faults in 24h below this are ordinary long-poll turbulence, not news.
_POLL_ERROR_WARN_AT = 6


def _surface_poll_health(tele: Section) -> List[HealthItem]:
    """Inbound TRANSPORT faults on the polling surfaces, in the window.

    2026-09-15 prod review, C11: 76 `telegram get_updates failed` in 7 days —
    34 request timeouts, 28 connection resets, 14 Bad Gateway. The poller
    recovers each time, and that is precisely why no seat ever said so: "the
    agent went quiet for a while" had no reading anywhere. Recovery is not the
    same as health.

    A WARN with a count, not an outage alarm — below ``_POLL_ERROR_WARN_AT``
    this is ordinary turbulence and says nothing.
    """
    if not tele.available:
        return []
    rows = _tele_rows(tele, _K_SURFACE_POLL_ERROR)
    if len(rows) < _POLL_ERROR_WARN_AT:
        return []
    by_surface: Dict[str, int] = {}
    for r in rows:
        sid = r.get("source") or "?"
        by_surface[sid] = by_surface.get(sid, 0) + 1
    where = ", ".join(f"{k}={v}" for k, v in
                      sorted(by_surface.items(), key=lambda kv: -kv[1]))
    return [HealthItem(
        key="surface_poll_errors", severity=SEVERITY_WARN,
        text=f"{len(rows)} inbound poll failure(s) in 24h ({where}) — the agent "
             f"was intermittently unreachable",
        remedy="`polyrob doctor` and the service log; usually upstream turbulence")]


def _digest_health(user_id: str, data_dir: str) -> List[HealthItem]:
    """Is the owner digest ENABLED but has no producer scheduled?

    2026-09-15 prod review, C4. The delivery rail records every suppressed
    owner message as an ``owner_notice`` on the promise that it is "rolled into
    the digest". On prod ``OWNER_DIGEST_ENABLED=true`` and the ONE cron job
    carrying ``payload.digest`` was ``cancelled`` — so 889 suppressed messages
    had no roll-up channel and `/missed` (last 5) was the whole recovery
    surface. An enabled flag with no producer reports healthy and runs nothing.

    An unreadable cron store is reported as UNKNOWN, never as scheduled: "I
    could not look" is a different fact from "it is fine".
    """
    from core.runtime_paths import cron_db_path
    from core.config_policy import AutonomyConfig
    try:
        if not AutonomyConfig.owner_digest_enabled():
            return []
    except Exception:
        return []
    try:
        jobs = _rows(cron_db_path(data_dir),
                     "SELECT status, enabled, payload FROM cron_jobs WHERE user_id=?",
                     (user_id,))
    except Exception as e:
        return [HealthItem(
            key="digest_unknown", severity=SEVERITY_WARN,
            text=f"owner digest is enabled but the cron store is unreadable "
                 f"({type(e).__name__}) — cannot tell whether it is scheduled",
            remedy="`polyrob doctor` for the store path, then `polyrob cron list`")]
    for row in jobs:
        if not _payload(row).get("digest"):
            continue
        if str(row.get("status") or "") in ("cancelled", "done") or not row.get("enabled"):
            continue
        return []
    return [HealthItem(
        key="digest_unscheduled", severity=SEVERITY_WARN,
        text="owner digest is enabled but no digest job is scheduled — suppressed "
             "messages have no roll-up channel",
        remedy="`polyrob cron digest 'every day 08:00'`")]


def _delivery_section(user_id: str, data_dir: str, tele: Section, container: Any,
                      now: float) -> Section:
    sec = Section(name="delivery")
    from core.surfaces.user_delivery import _reserved_slots, effective_daily_cap
    cap = int(effective_daily_cap(user_id, data_dir))
    sec.data["cap"] = cap
    if not tele.available:
        # The cap gate's memory IS the telemetry log: without it we cannot say
        # how many messages were suppressed — say exactly that.
        raise RuntimeError(f"delivery rail memory unreadable ({tele.reason})")
    outcomes: Dict[str, int] = {}
    by_source: Dict[str, int] = {}
    for r in _tele_rows(tele, _K_USER_DELIVERY):
        o = (r["attrs"] or {}).get("outcome") or "?"
        outcomes[o] = outcomes.get(o, 0) + 1
        if o == "capped":
            src = r.get("source") or "?"
            by_source[src] = by_source.get(src, 0) + 1
    consumed = outcomes.get("sent", 0) + outcomes.get("fallback", 0)
    capped = outcomes.get("capped", 0)
    suppressed_notices = sum(
        1 for r in _tele_rows(tele, _K_OWNER_NOTICE)
        if str((r["attrs"] or {}).get("text") or "").startswith(_SUPPRESSED_PREFIX))
    sec.data.update({"outcomes": outcomes, "consumed": consumed, "capped": capped,
                     "capped_by_source": by_source, "suppressed_notices": suppressed_notices,
                     "reserved_slots": _reserved_slots()})
    sec.lines.append(
        f"owner messages 24h: {consumed}/{cap} cap used, {capped} suppressed, "
        f"{outcomes.get('fallback', 0)} fallback, {outcomes.get('paused', 0)} paused, "
        f"{outcomes.get('deduped', 0)} deduped, {outcomes.get('rate_limited', 0)} rate-limited, "
        f"{outcomes.get('quiet_held', 0)} held (quiet hours)")
    if capped:
        top = ", ".join(f"{k}={v}" for k, v in sorted(by_source.items(), key=lambda kv: -kv[1])[:3])
        sec.health.append(HealthItem(
            key="delivery_capped", severity=SEVERITY_WARN,
            text=f"{capped} owner message(s) suppressed by the daily cap in 24h "
                 f"({consumed}/{cap} used; by source: {top})",
            remedy="/missed to read them; raise the cap with `/config set delivery.daily_cap N`"))
    sec.health.extend(_digest_health(user_id, data_dir))
    sec.health.extend(_surface_poll_health(tele))
    timeouts = len(_tele_rows(tele, _K_TOOL_TIMEOUT))
    degraded = len(_tele_rows(tele, _K_RUN_DEGRADED))
    denied = len(_tele_rows(tele, _K_TOOL_DENIED))
    sec.data.update({"tool_timeouts": timeouts, "run_degraded": degraded, "tool_denied": denied})
    sec.lines.append(f"24h governance: {timeouts} tool timeout(s), {denied} tool denial(s), "
                     f"{degraded} degraded run outcome(s)")
    if timeouts:
        sec.health.append(HealthItem(
            key="tool_timeouts", severity=SEVERITY_WARN,
            text=f"{timeouts} tool timeout(s) in 24h", remedy="/recap to see which runs"))
    if degraded:
        sec.health.append(HealthItem(
            key="run_outcome_degraded", severity=SEVERITY_WARN,
            text=f"{degraded} run(s) finished DEGRADED in 24h", remedy="/recap"))
    # Surface circuits (live objects; only when a container is at hand).
    if container is not None:
        from core.surfaces.health import surface_health
        rows = surface_health(container)
        sec.data["surfaces"] = rows
        for r in rows:
            if r.get("circuit") == "OPEN":
                sec.health.append(HealthItem(
                    key=f"circuit:{r['surface_id']}", severity=SEVERITY_CRIT,
                    text=f"surface {r['surface_id']} circuit is OPEN — sends are being dropped",
                    remedy=f"`polyrob surface resume {r['surface_id']}` once the platform is back"))
            if r.get("dead_targets"):
                sec.lines.append(f"{r['surface_id']}: {r['dead_targets']} dead target(s)")
    return sec


def _apps_section(user_id: str, data_dir: str) -> Section:
    """032: the durable app service — pending approvals lead (CRIT), failures WARN.
    Read-only over app_services.db; an absent registry is "no apps", never created."""
    sec = Section(name="apps")
    db = os.getenv("APP_SERVICES_DB_PATH") or os.path.join(data_dir, "app_services.db")
    if not os.path.exists(db):
        sec.lines.append("no apps")
        sec.data["apps"] = []
        return sec
    rows = _rows(db, "SELECT slug, status, public_url, host_port, last_health, "
                     "last_failure_error FROM app_services WHERE user_id=? ORDER BY created_at",
                 (user_id,))
    sec.data["apps"] = rows
    if not rows:
        sec.lines.append("no apps")
        return sec
    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
    sec.lines.append(", ".join(f"{n} {s}" for s, n in sorted(by.items())))
    for r in rows:
        where = r.get("public_url") or (f"127.0.0.1:{r['host_port']}" if r.get("host_port") else "-")
        sec.lines.append(f"{r['slug']}: {r['status']} {where}"
                         + (f" (health {_hhmm(r['last_health'])})" if r.get("last_health") else ""))
    pending = [r["slug"] for r in rows if r["status"] == "pending"]
    if pending:
        sec.health.append(HealthItem(
            key="apps_pending", severity=SEVERITY_CRIT,
            text=f"{len(pending)} app(s) waiting on your approval ({', '.join(pending)})",
            remedy="/apps approve <slug> · polyrob apps approve <slug>"))
    failed = [r for r in rows if r["status"] == "failed"]
    if failed:
        sec.health.append(HealthItem(
            key="apps_failed", severity=SEVERITY_WARN,
            text=f"{len(failed)} app(s) failed: " + "; ".join(
                f"{r['slug']} ({str(r.get('last_failure_error') or '')[:60]})" for r in failed),
            remedy="/apps logs <slug> · redeploy a fixed tree"))
    return sec


def _room_actions_section(user_id: str, data_dir: str) -> Section:
    """046: paid room actions — what is awaiting payment, and what is OWED.

    ⚠️ A CREDIT is money we HOLD against a service that was never delivered, so
    it leads as CRIT. Read-only and existence-guarded: an absent store is "no
    paid actions", never CREATED by a status read.
    """
    sec = Section(name="room_actions")
    db = os.path.join(data_dir, "room_actions.db")
    if not os.path.exists(db):
        sec.lines.append("no paid actions")
        sec.data.update(pending=0, credits_owed=0, applied=0, credits=[])
        return sec
    rows = _rows(db, "SELECT offer_id, surface, chat_id, verb, price_usd, "
                     "status, reason FROM room_action_offers "
                     "ORDER BY created_at DESC LIMIT 200")
    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
    credits = [r for r in rows if r["status"] == "credited"]
    sec.data.update(
        pending=by.get("pending", 0), paid=by.get("paid", 0),
        applied=by.get("applied", 0), credits_owed=len(credits),
        credits=[{"offer_id": r["offer_id"], "verb": r["verb"],
                  "chat_id": r["chat_id"], "price_usd": r["price_usd"],
                  "reason": r["reason"]} for r in credits[:10]])
    if not rows:
        sec.lines.append("no paid actions")
        return sec
    sec.lines.append(", ".join(f"{n} {st}" for st, n in sorted(by.items())))
    # A PAID offer whose effect has not landed is an obligation in flight, not a
    # failure yet — named, but not escalated.
    in_flight = by.get("paid", 0)
    if in_flight:
        sec.lines.append(f"{in_flight} paid action(s) awaiting the effect")
    if credits:
        owed = sum(float(r.get("price_usd") or 0) for r in credits)
        sec.health.append(HealthItem(
            key="room_action_credits_owed", severity=SEVERITY_CRIT,
            text=(f"{len(credits)} paid room action(s) took money and could not "
                  f"be applied (${owed:.2f}): "
                  + "; ".join(f"{r['offer_id']} ({str(r.get('reason') or '')[:50]})"
                              for r in credits[:3])),
            remedy="/paid offers · polyrob owner paid list"))
    return sec


def _groups_section(user_id: str, data_dir: str) -> Section:
    """044 T18: rooms this instance is present in — allowlist + mode + traffic.

    Instance-level, not tenant-scoped: ``group_allowlist.db`` has no
    ``user_id`` column (one bot presence per chat, not per-tenant — a room's
    overlay lives under the OWNER tenant, read via ``chat_policy.load``).
    Read-only, existence-guarded: an absent allowlist is "no rooms", never
    created by this read. ``surfaces.db`` (the room ledger + traffic counters)
    is guarded the SAME way (044 T18 fix round 1, Important 2) — constructing
    ``GroupLedger``/``RoomCaps`` runs their DDL, which would create the file on
    a plain status read; when it is absent every room reports
    ``ledger_rows=0``/``replies_today=0`` with ``note="surfaces.db absent"``
    instead.
    """
    sec = Section(name="groups")
    allow_db = os.path.join(data_dir, "group_allowlist.db")
    if not os.path.exists(allow_db):
        sec.lines.append("no rooms")
        sec.data["rooms"] = []
        return sec
    from core.surfaces.chat_policy import load_for_chat
    from core.surfaces.group_allowlist import GroupAllowlist
    rows = GroupAllowlist(allow_db).list_all()
    active = [r for r in rows if r.get("status") == "active"]
    # 044 T21: a room the bot was KICKED from is `left`, not `active`. Named
    # here, or a room that stopped answering reads as a room that never existed.
    left = [{"surface": r["surface"], "chat_id": r["chat_id"]}
            for r in rows if r.get("status") == "left"]
    sec.data["left_rooms"] = left
    left_line = (f"{len(left)} room(s) LEFT (bot removed): "
                 + ", ".join(f"{r['surface']}:{r['chat_id']}" for r in left)) if left else ""
    if not active:
        sec.lines.append("no rooms")
        if left_line:
            sec.lines.append(left_line)
        sec.data["rooms"] = []
        return sec
    surfaces_db = os.path.join(data_dir, "surfaces.db")
    ledger = caps = None
    if os.path.exists(surfaces_db):
        from core.surfaces.group_ledger import GroupLedger
        from core.surfaces.room_caps import RoomCaps
        ledger = GroupLedger(surfaces_db)
        caps = RoomCaps(surfaces_db)
    now = time.time()
    out_rows = []
    for r in active:
        surface, chat_id = r["surface"], r["chat_id"]
        pol = load_for_chat(data_dir, surface, chat_id)
        if ledger is not None and caps is not None:
            last = ledger.tail(surface, chat_id, limit=1)
            replies_today = caps.replies_since(surface, chat_id, now - 86400)
            ledger_rows = ledger.count(surface, chat_id)
            last_write = last[0].ts if last else None
            note = ""
        else:
            replies_today = 0
            ledger_rows = 0
            last_write = None
            note = "surfaces.db absent"
        out_rows.append({
            "surface": surface, "chat_id": chat_id,
            "name": pol.name or r.get("note") or chat_id,
            "mode": pol.mode,
            "replies_today": replies_today,
            "ledger_rows": ledger_rows,
            "last_write": last_write,
            "note": note,
        })
    sec.data["rooms"] = out_rows
    sec.lines.append(f"{len(out_rows)} room(s)")
    for row in out_rows:
        line = (f"{row['surface']}:{row['chat_id']} \"{row['name']}\" mode={row['mode']} "
                f"replies_today={row['replies_today']} ledger={row['ledger_rows']} "
                f"last={_hhmm(row['last_write'])}")
        if row["note"]:
            line += f" ({row['note']})"
        sec.lines.append(line)
    if left_line:
        sec.lines.append(left_line)
    return sec


def _wallet_section(data_dir: Optional[str]) -> Section:
    """What the agent is holding, from the cache — NEVER a network read (039 D).

    Deliberately separate from `money`. That section is CASH FLOW: income minus
    spend, which cannot see a held bag and is not a balance. This one is the bag.
    Summing or conflating them is how a treasury report describes a position it
    does not have.

    Cache-only is what keeps `include_balances=False` honest and what lets the
    per-turn `<live-health>` note carry a wallet line without a turn ever waiting
    on four JSON-RPC round trips.
    """
    from core.env import bool_env
    from core.wallet import balance_cache
    if not bool_env("AGENT_WALLET_ENABLED", False):
        # A deployment with no wallet has nothing to report, and reporting that as
        # `unavailable` would mark every status PARTIAL forever over a feature
        # nobody turned on. A status must degrade for real conditions only.
        return Section(name="wallet", state=STATE_OK,
                       lines=["wallet: not enabled (AGENT_WALLET_ENABLED)"],
                       data={"enabled": False})
    snap = balance_cache.read(data_dir)
    if snap is None:
        # The wallet IS on and no snapshot exists, so the reader has not run. That
        # is a real gap: the agent would answer a balance question from context.
        return Section(name="wallet", state=STATE_UNAVAILABLE,
                       reason="no balance snapshot yet")
    lines = balance_cache.render_lines(snap)
    totals = balance_cache.total_native_by_symbol(snap)
    unknown = [c.chain for c in snap.chains if c.native is None]
    sec = Section(name="wallet",
                  state=STATE_DEGRADED if (unknown or snap.stale) else STATE_OK,
                  lines=lines,
                  data={"address": snap.address, "age_sec": snap.age_sec,
                        "stale": snap.stale, "totals": totals,
                        "unknown_chains": unknown})
    if snap.stale:
        sec.reason = f"snapshot is {int(snap.age_sec // 60)}m old"
        sec.health.append(HealthItem(
            key="wallet_stale",
            text=f"wallet balances are {int(snap.age_sec // 60)}m old — do not "
                 f"quote them as current",
            remedy="the balance reader runs on the autonomy runtime; check it is up"))
    if unknown:
        # A chain that could not be read is NOT a chain with nothing on it, and the
        # difference is the whole point of carrying `None` through.
        sec.health.append(HealthItem(
            key="wallet_unreadable",
            text=f"balance unreadable on {', '.join(unknown)} — treat as UNKNOWN, "
                 f"never as zero",
            remedy="check the pinned RPC for those chains"))
    return sec


def _posture_section() -> Section:
    from core.config_policy.posture_card import build_posture_card, render_posture_card
    rows = build_posture_card()
    return Section(name="posture", lines=render_posture_card(rows), data={"rows": rows})


def _security_section(user_id: str, data_dir: str, now: float,
                      window_sec: int) -> Section:
    """045: the perimeter, the refusals and the threat flags (lanes 1-3).

    Counted by core/security_digest.py — this section only shapes the result and
    raises health items. An unreadable store propagates out of here so _guarded
    renders `unavailable(<reason>)`: 'no store' must never look like 'zero
    denials'.
    """
    from core.security_digest import build_security_rollup
    r = build_security_rollup(user_id, data_dir=data_dir,
                              since_ts=now - window_sec, window_sec=window_sec)
    sec = Section(name="security")
    sec.data.update({
        "inbound": r.inbound, "denied": r.denied, "refused": r.refused,
        "flagged": r.flagged, "rate_limited": r.rate_limited,
        "top_senders": r.top_senders,
        "top_denial_reasons": r.top_denial_reasons,
        "top_refusal_reasons": r.top_refusal_reasons,
    })
    sec.lines.append(f"{r.inbound} inbound / {r.denied} denied · "
                     f"{r.refused} refused · {r.flagged} flagged")
    if r.flagged:
        sec.health.append(HealthItem(
            key="injection_flagged", severity=SEVERITY_WARN,
            text=f"{r.flagged} threat-scan flag(s) in the window",
            remedy="review injection_flagged rows in telemetry_events"))
    return sec


def _identity_section(instance_id: str, data_dir: Optional[str]) -> Section:
    """WHO the agent is: its instance and the frozen Mindprint face + voice.

    The avatar system has been complete since 2026-07-19 and reached no status
    surface at all, and ``core.instance.voice_signature()`` had zero callers
    outside ``polyrob pfp say``. On 2026-09-15 prod had no avatar and no seat
    said so.

    ⚠️ An absent avatar is ``ok``, not ``degraded``, and raises NO health item.
    Setup is optional; a permanent WARN for an optional step is the noise that
    teaches an owner to skip the health block.

    ⚠️ Read-only, and it never CREATES the identity directory — a status surface
    that writes is how an empty store becomes a real one.
    """
    from core.instance import load_erc8004_record, load_pfp_meta, pfp_dir

    sec = Section(name="identity")
    if not data_dir:
        raise ValueError("no data dir to read the instance identity from")
    sec.data["instance_id"] = instance_id

    # 046: the ERC-8004 on-chain identity, when one was actually registered.
    # Reported BEFORE the avatar branches below return, so an instance with no
    # avatar still shows its on-chain identity and vice versa.
    record = load_erc8004_record(data_dir, instance_id)
    sec.data["erc8004"] = record
    if record:
        sec.lines.append(
            f"erc-8004: agentId {record.get('agent_id')} on "
            f"{record.get('chain')} ({record.get('registry')})")
    else:
        # Optional, like the avatar — an absent identity is a FACT the owner
        # should be able to read, not an empty space, and it raises no health
        # item because a permanent WARN for an optional step is the noise that
        # teaches an owner to skip the health block.
        sec.lines.append("erc-8004: not registered (optional)")

    # 2026-09-17: the agent's OWN reachable identities — the address it sends
    # mail AS and the X account it posts AS. Until now no seat could answer
    # "do I have an email / an X account", so a bootstrap skill had to guess.
    # Env PRESENCE only, never a value; the X browser-session record lives in
    # the tools tier and is checked by the agent's own `x_login_check` verb.
    _identity_reach_lines(sec, data_dir)

    d = pfp_dir(data_dir, instance_id)
    png = d / "pfp.png"
    if not png.is_file():
        sec.data["avatar"] = "none"
        sec.lines.append(f"{instance_id} — avatar not set up "
                         f"(optional: `polyrob pfp generate`, then `keep`)")
        return sec

    meta = load_pfp_meta(data_dir, instance_id)
    if not isinstance(meta, dict):
        # The face exists but its record does not parse. Reporting "kept" would
        # claim traits we cannot read; reporting "none" would deny a file that
        # is right there. Both are confident lies, so say the true third thing.
        sec.data["avatar"] = "unreadable"
        sec.lines.append(f"{instance_id} — avatar present but its record is "
                         f"unreadable ({d / 'pfp.json'})")
        return sec

    kept = bool(meta.get("locked", True))
    traits = meta.get("traits") if isinstance(meta.get("traits"), dict) else {}
    voice = meta.get("voice") if isinstance(meta.get("voice"), dict) else {}
    sec.data.update({
        "avatar": "kept" if kept else "draft",
        "seed_hex": meta.get("seed_hex"),
        "tier": traits.get("tier"),
        "traits": traits,
        "voice": voice,
        "generator": meta.get("generator"),
        "rendered_by": meta.get("rendered_by"),
        "path": str(png),
    })

    head = f"{instance_id} — avatar {'kept' if kept else 'DRAFT (not kept yet)'}"
    if meta.get("seed_hex"):
        head += f", seed {meta['seed_hex']}"
    if traits.get("tier"):
        head += f", {traits['tier']}"
    sec.lines.append(head)
    shown = [f"{k} {traits[k]}" for k in ("head", "eyes", "mouth", "antenna", "aura")
             if traits.get(k)]
    if shown:
        sec.lines.append("face: " + ", ".join(shown))
    if voice:
        sec.lines.append(
            "voice: pitch {p} · rate {r} · timbre {t}".format(
                p=voice.get("pitch", "?"), r=voice.get("rate", "?"),
                t=voice.get("timbre", "?")))
    if not kept:
        sec.lines.append("re-roll with `polyrob pfp randomize`, accept with "
                         "`polyrob pfp keep` (permanent)")
    return sec


def _identity_reach_lines(sec: Section, data_dir: Optional[str]) -> None:
    """Append `email:` and `x:` facts to the identity section (read-only).

    email — `resolve_agent_email` (explicit override → provisioned AgentMail
    inbox → legacy GMAIL login), or `none` with the remedy. An unset
    `AGENTMAIL_API_KEY` is named so the owner knows which lever provisions one.
    x — the API rail is "configured" when the four OAuth 1.0a keys are all
    present (`TWITTER_ENABLED` decides whether WRITES are armed); the handle is
    `TWITTER_BOT_USERNAME` when set. Nothing here reads a secret VALUE.
    """
    from core.instance import resolve_agent_email
    email = None
    try:
        email = resolve_agent_email(data_home=Path(data_dir) if data_dir else None)
    except Exception as e:
        sec.lines.append(f"email: unreadable ({type(e).__name__})")
    else:
        sec.data["email"] = email
        if email:
            sec.lines.append(f"email: {email}")
        else:
            has_key = bool((os.environ.get("AGENTMAIL_API_KEY") or "").strip())
            sec.lines.append(
                "email: none — " + ("AGENTMAIL_API_KEY is set; the inbox is provisioned "
                                    "the first time the email tool initialises"
                                    if has_key else
                                    "set AGENTMAIL_API_KEY (own inbox) or GMAIL_EMAIL/"
                                    "GMAIL_APP_PASSWORD (SMTP)"))
    api_keys = ("TWITTER_API_KEY", "TWITTER_API_SECRET_KEY",
                "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")
    present = [k for k in api_keys if (os.environ.get(k) or "").strip()]
    handle = (os.environ.get("TWITTER_BOT_USERNAME") or "").strip().lstrip("@")
    from core.env import bool_env
    writes = bool_env("TWITTER_ENABLED", False)
    sec.data["x_api"] = "configured" if len(present) == 4 else (
        "partial" if present else "none")
    sec.data["x_handle"] = handle or None
    if len(present) == 4:
        sec.lines.append(
            f"x: api configured{' @' + handle if handle else ''} — writes "
            f"{'ON' if writes else 'OFF (TWITTER_ENABLED)'}; browser rail: ask x_login_check")
    elif present:
        missing = [k for k in api_keys if k not in present]
        sec.lines.append(f"x: api PARTIAL — missing {', '.join(missing)}")
    else:
        sec.lines.append("x: no api keys — create an X developer app for the agent's "
                         "account and set TWITTER_API_KEY/SECRET_KEY/ACCESS_TOKEN/"
                         "ACCESS_TOKEN_SECRET (polls + search need the API rail); "
                         "browser rail: x_login_check")


def _positions_line(data_dir: Optional[str]) -> str:
    """One line naming how many positions the LEDGER records as open.

    The treasury figure is cash flow; it cannot see a held bag. Saying only
    "open positions NOT included" leaves the owner unable to tell an empty book
    from two unsellable positions — on 2026-08-28 it was two, and on 2026-08-25
    that same blind spot was published to X as "book flat". This is the cheap
    half of the fix (a file read, no network); verifying the count against chain
    stays with the `reconcile` verb, which is what the remedy points at.

    UNKNOWN is never zero: an unreadable/absent ledger says so.
    """
    from core.position_ledger import read_open_positions
    rows, err = read_open_positions(data_dir)
    if err:
        return f"open positions: UNKNOWN — {err} (not the same as none)"
    if not rows:
        return "open positions: none recorded in the ledger"
    syms = ", ".join(r.symbol for r in rows[:5]) + ("…" if len(rows) > 5 else "")
    return (f"open positions: {len(rows)} recorded in the ledger ({syms}) — "
            f"ledger-recorded, NOT verified against chain; run `reconcile`")


def _money_section(user_id: str, ledger: Any, data_dir: Optional[str] = None) -> Section:
    """``ledger`` is the ``build_ledger`` dict, or the exception it raised."""
    if isinstance(ledger, BaseException):
        raise ledger
    if not isinstance(ledger, dict) or not ledger:
        raise RuntimeError("ledger returned no data")
    sec = Section(name="money", data={"ledger": ledger})
    r = ledger.get("runtime") or {}
    t = ledger.get("treasury") or {}
    spend = float(r.get("spend_window_usd") or 0.0)
    total = float(r.get("spend_total_usd") or 0.0)
    line = f"runtime cost (owner's compute bill): ${spend:.2f} last 24h · ${total:.2f} total"
    if r.get("provider_balance_usd") is not None:
        line += f" · provider balance ${float(r['provider_balance_usd']):.2f}"
    sec.lines.append(line)
    net = float(t.get("net_usd") or 0.0)
    line = (f"treasury cash flow (income − spend; open positions NOT included): "
            f"net ${net:+.2f}")
    if t.get("balance_usd") is not None:
        line += f" · USDC balance ${float(t['balance_usd']):.2f}"
    if int(t.get("pending_count") or 0):
        line += f" · {int(t['pending_count'])} pending invoice(s) ${float(t.get('pending_usd') or 0):.2f}"
    sec.lines.append(line)
    # Positions are read separately and must never take the money section down:
    # a missing ledger is a fact about the ledger, not about the treasury.
    try:
        sec.lines.append(_positions_line(data_dir))
    except Exception as e:
        sec.lines.append(f"open positions: UNKNOWN ({type(e).__name__}: {e})")
    from core.activity_evidence import ledger_note
    note = ledger_note(ledger)
    if note:
        sec.state = STATE_DEGRADED
        sec.reason = note
        sec.lines.append(f"⚠ {note}")
    return sec


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
    from core.instance import resolve_instance_id
    from core.status_liquidity import liquidity_section
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
        "delivery": _guarded("delivery", _delivery_section, uid, data_dir, tele, container, now),
        "posture": _guarded("posture", _posture_section),
        # ⚠️ Insertion order IS the render order — `test_every_section_is_always_present`
        # asserts `tuple(snap.sections) == SECTION_ORDER`. Keep the two in step.
        "security": _guarded("security", _security_section, uid, data_dir, now, window_sec),
        "identity": _guarded("identity", _identity_section,
                             resolve_instance_id(), data_dir),
        "apps": _guarded("apps", _apps_section, uid, data_dir),
        "groups": _guarded("groups", _groups_section, uid, data_dir),
        "room_actions": _guarded("room_actions", _room_actions_section, uid,
                                 data_dir),
        "creations": _guarded("creations", _creations_section, uid, data_dir),
        "collectibles": _guarded("collectibles", _collectibles_section, uid, data_dir),
        "liquidity": _guarded("liquidity", liquidity_section, uid, data_dir),
        "wallet": _guarded("wallet", _wallet_section, data_dir),
    }
    if not uid:
        for name in ("work", "approvals", "loops", "delivery", "apps",
                     "creations", "collectibles", "liquidity", "security"):
            sections[name] = Section(name=name, state=STATE_UNAVAILABLE,
                                     reason="no tenant (empty user_id)")
    return uid, now, window_sec, sections



#: The verbs that CREATE something the owner will later be asked about. Derived
#: from the spend ledger rather than a second store: every one of them already
#: calls `gate.record(counterparty=<the address it made>, result_ref=<the tx>)`,
#: so the record exists and nothing new has to be written to read it back.
CREATION_VERBS = ("deploy_token", "deploy_contract", "solana_deploy_token",
                  "launchpad_launch")


def _creations_section(user_id: str, data_dir: str) -> Section:
    """What this agent has CREATED on-chain (042b).

    The gap this closes is the one the 2026-08-25 ledger incident is the famous
    instance of: the agent did something durable and no surface could show it
    back. A token it deployed last week existed only in a transaction hash in a
    chat message.

    Read-only over `telemetry_events`, tenant-scoped, newest first. An absent or
    unreadable store renders its reason — never an empty list, because "I have
    created nothing" and "I cannot see what I created" are different facts and
    only one of them is reassuring.
    """
    from core.wallet.chains import explorer_url

    sec = Section(name="creations")
    db = _telemetry_db_path(data_dir)
    rows = _rows(
        db,
        "SELECT ts, attrs FROM telemetry_events WHERE kind='wallet_spend' "
        "AND user_id=? ORDER BY ts DESC LIMIT 400",
        (user_id,))

    made = []
    unreadable = 0
    for row in rows:
        try:
            attrs = json.loads(row.get("attrs") or "{}")
        except Exception:
            # NOT silent: a row we cannot parse might be a creation, so it is
            # counted and surfaced. A status view that quietly drops rows is how
            # "I have created nothing" comes to mean "I could not tell".
            unreadable += 1
            continue
        action = str(attrs.get("action") or "")
        if action not in CREATION_VERBS:
            continue
        chain = attrs.get("chain")
        address = attrs.get("counterparty")
        url = None
        if chain and address:
            kind = "token" if action in (
                "deploy_token", "solana_deploy_token", "launchpad_launch") else "address"
            url = explorer_url(chain, kind, str(address))
        made.append({
            "action": action,
            "address": address,
            "chain": chain,
            "tx": attrs.get("result_ref"),
            "usd": attrs.get("amount_usd"),
            "ts": row.get("ts"),
            "url": url,
        })

    sec.data["creations"] = made
    sec.data["unreadable_rows"] = unreadable
    if unreadable:
        sec.lines.append(f"⚠ {unreadable} spend row(s) could not be read — this "
                         f"list may be incomplete")
        sec.health.append(HealthItem(
            key="creations_unreadable", severity=SEVERITY_WARN,
            text=(f"{unreadable} wallet_spend row(s) did not parse, so what I "
                  f"have created cannot be listed in full"),
            remedy="check telemetry_events for malformed attrs"))
    if not made:
        sec.lines.append("nothing deployed or launched"
                         + (" (that could be read)" if unreadable else ""))
        return sec

    by = {}
    for item in made:
        by[item["action"]] = by.get(item["action"], 0) + 1
    sec.lines.append(", ".join(f"{n} {a}" for a, n in sorted(by.items())))
    for item in made[:5]:
        action = item["action"]
        addr = item.get("address") or "address unknown"
        chain = item.get("chain")
        ts_part = f" ({_hhmm(item['ts'])})" if item.get("ts") else ""
        url = item.get("url")
        url_part = f" — {url}" if url else ""
        if chain:
            sec.lines.append(f"{action}: {addr} on {chain}{ts_part}{url_part}")
        else:
            sec.lines.append(f"{action}: {addr}{ts_part}{url_part}")
    if len(made) > 5:
        sec.lines.append(f"…and {len(made) - 5} more")
    return sec


#: NFT move verbs, by their `gate.record(action=...)` name. Derived from the
#: spend ledger for the same reason CREATION_VERBS is: the record already
#: exists, so nothing new has to be written to read it back.
NFT_MOVE_VERBS = ("nft_transfer",)


def _collectibles_section(user_id: str, data_dir: str,
                          enumerate_fn=None) -> Section:
    """Non-fungibles: what the chain says is HELD, and what the guard MOVED.

    ⚠️ These are two different questions and the section never merges them. A
    `wallet_spend` row exists only for a SPEND, so an AIRDROPPED token has no
    row at all — telemetry can say what left, never what arrived unasked. Only
    a chain read answers "held".

    ⚠️ `held is None` means NOT READ (no provider, a failed read, or simply not
    asked for), and renders as such. `held == []` means a working read returned
    nothing. Collapsing the two would turn "I could not look" into "you own
    nothing", which is the exact confident-zero class the status SSOT exists to
    prevent.

    ⚠️ No network read unless `enumerate_fn` is supplied — status is cheap by
    default, the same contract `include_balances` carries for the ledger.
    """
    sec = Section(name="collectibles")

    # --- held (a chain read, opt-in) ---------------------------------------
    sec.data["held"] = None
    if enumerate_fn is not None:
        try:
            sec.data["held"] = list(enumerate_fn(user_id=user_id))
        except Exception as e:
            sec.lines.append(f"held: could not be read ({type(e).__name__}: "
                             f"{str(e)[:120]})")
    else:
        sec.lines.append("held: not read (a chain read is opt-in here; ask for "
                         "it explicitly, or run the nft_holdings verb)")

    held = sec.data["held"]
    if held is not None:
        if not held:
            sec.lines.append("held: none — this IS an answer from a working "
                             "read, not a failed one")
        else:
            sec.lines.append(f"held: {len(held)} NFT(s)")
            for item in held[:5]:
                name = item.get("name") or "(unnamed)"
                sec.lines.append(f"  {name} — {item.get('contract')} "
                                 f"#{item.get('token_id')}")
            if len(held) > 5:
                sec.lines.append(f"  …and {len(held) - 5} more")

    # --- moved (derived from the spend ledger) -----------------------------
    rows = _rows(
        _telemetry_db_path(data_dir),
        "SELECT ts, attrs FROM telemetry_events WHERE kind='wallet_spend' "
        "AND user_id=? ORDER BY ts DESC LIMIT 400",
        (user_id,))
    moved = []
    unreadable = 0
    for row in rows:
        try:
            attrs = json.loads(row.get("attrs") or "{}")
        except Exception:
            # NOT silent: an unparseable row might BE a move, so it is counted
            # and surfaced rather than quietly dropped.
            unreadable += 1
            continue
        if str(attrs.get("action") or "") not in NFT_MOVE_VERBS:
            continue
        moved.append({"asset": attrs.get("asset"), "to": attrs.get("counterparty"),
                      "chain": attrs.get("chain"), "tx": attrs.get("result_ref"),
                      "ts": row.get("ts")})
    sec.data["moved"] = moved
    sec.data["unreadable_rows"] = unreadable
    if unreadable:
        sec.lines.append(f"⚠ {unreadable} spend row(s) could not be read — the "
                         f"move list may be incomplete")
    if moved:
        sec.lines.append(f"moved out: {len(moved)}")
        for item in moved[:5]:
            sec.lines.append(f"  {item.get('asset') or 'asset unrecorded'} -> "
                             f"{item.get('to')} ({_hhmm(item.get('ts'))})")
    else:
        sec.lines.append("moved out: none recorded")
    return sec


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
    raised) pass it in."""
    uid, now, window_sec, sections = _build_core(
        user_id, data_dir=data_dir, task_agent=task_agent, session_id=session_id,
        container=container, window_hours=window_hours)
    if liquidity_enumerate_fn is not None and uid:
        from core.status_liquidity import liquidity_section
        from core.runtime_paths import data_dir_or_home
        sections["liquidity"] = _guarded("liquidity", liquidity_section, uid,
                                        data_dir_or_home(data_dir), liquidity_enumerate_fn)
    if not include_money:
        sections["money"] = Section(name="money", state=STATE_UNAVAILABLE,
                                    reason="not requested on this path")
    else:
        if ledger is None and uid:
            try:
                from core.activity_evidence import ledger_rollup_strict
                ledger = ledger_rollup_strict(uid, max(1, window_sec // 86400),
                                              include_balances=include_balances)
            except Exception as e:
                ledger = e
        elif ledger is None:
            ledger = ValueError("no tenant (empty user_id)")
        from core.runtime_paths import data_dir_or_home
        sections["money"] = _guarded("money", _money_section, uid, ledger,
                                     data_dir_or_home(data_dir))
    return _assemble(uid, now, window_sec, sections)
