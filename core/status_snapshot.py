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

# Goal-board literals (pinned to agents/task/goals/board.py by the contract test).
_KIND_GOAL, _KIND_OBJECTIVE, _KIND_ASK = "goal", "objective", "ask"
_ST_READY, _ST_RUNNING, _ST_BLOCKED, _ST_WAITING, _ST_TRIAGE = (
    "ready", "running", "blocked", "waiting", "triage")
_ST_DONE, _ST_CANCELLED = "done", "cancelled"
_ASK_OPEN = "open"
_OBJ_ACTIVE = "active"
_TOOL_APPROVAL_ASK_KIND = "tool_approval"
# Telemetry kinds (pinned to core/event_kinds.py by the contract test).
_K_USER_DELIVERY, _K_OWNER_NOTICE = "user_delivery", "owner_notice"
_K_CREDIT_SENTINEL, _K_CRON_RUN, _K_GOAL_RUN = "credit_sentinel", "cron_run", "goal_run"
_K_SELF_WAKE, _K_TOOL_TIMEOUT, _K_TOOL_DENIED = "self_wake", "tool_timeout", "tool_denied"
_K_RUN_DEGRADED, _K_AUTONOMY_TICK = "run_outcome_degraded", "autonomy_tick"
_K_SOCIAL_WRITE, _K_WALLET_SPEND = "social_write", "wallet_spend"
#: 031: outcomes that mean "the starter honoured the pause" (not a violation)
_PAUSE_HONOURED_OUTCOMES = frozenset({"paused", "skipped", "held", "dropped"})
_SUPPRESSED_PREFIX = "[suppressed by daily proactive-message cap"

SECTION_ORDER = ("session", "providers", "work", "approvals", "loops",
                 "delivery", "posture", "apps", "money")


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


def _telemetry_db_path(data_dir: str) -> str:
    override = (os.getenv("TELEMETRY_EVENT_LOG_PATH") or "").strip()
    if override:
        return override
    local = os.path.join(data_dir, "telemetry_events.db")
    if os.path.exists(local):
        return local
    from core.runtime_paths import sidecar_db_path
    return str(sidecar_db_path("telemetry_events.db"))


def _read_telemetry(user_id: str, data_dir: str, since_ts: float) -> Section:
    """One bounded read of the durable event log for the window; the other
    sections aggregate over ``data['rows']`` (memory_* kinds excluded — they
    are the bulk of the log and carry no health signal)."""
    path = _telemetry_db_path(data_dir)
    rows = _rows(
        path,
        "SELECT ts, kind, user_id, source, attrs FROM telemetry_events "
        "WHERE ts >= ? AND kind NOT IN ('memory_write','memory_recall') "
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
        release = entry.get("release_ts")
        until = (f"until ~{_hhmm(release)}" if release
                 else f"auto-release {_hhmm(float(entry.get('ts') or 0) + _release_window())}")
        reason = (entry.get("reason") or "").strip().replace("\n", " ")[:140]
        sec.health.append(HealthItem(
            key=f"credit_sentinel:{name}", severity=SEVERITY_CRIT,
            text=f"credit sentinel TRIPPED for {who} since {_hhmm(entry.get('ts'))} "
                 f"({until})" + (f": {reason}" if reason else ""),
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
            remedy="/pending · /approve <id> · /reject <id>"))
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
        expected = []
        try:
            from core.autonomy_runtime import _cron_enabled  # the runtime's own gate
            if _cron_enabled():
                expected.append("cron")
        except Exception as e:
            sec.lines.append(f"cron gate unreadable ({type(e).__name__}) — cron liveness not checked")
        if AutonomyConfig.goals_enabled():
            expected.append("goals")
        from core.env import int_env
        stale_after = max(900, 2 * int_env("AUTONOMY_HEARTBEAT_INTERVAL_SEC", 300))
        hb_bits = []
        for loop in expected:
            hb = latest.get(loop)
            if hb is None:
                hb_bits.append(f"{loop}: no heartbeat in 24h")
                sec.health.append(HealthItem(
                    key=f"loop_heartbeat:{loop}", severity=SEVERITY_WARN,
                    text=f"{loop} loop: no liveness heartbeat recorded in 24h (dead, or "
                         f"this process does not emit heartbeats)",
                    remedy="check the service log; restart if the loop task exited"))
            elif hb.get("alive") is False or now - float(hb["ts"]) > stale_after:
                hb_bits.append(f"{loop}: last heartbeat {_age(hb['ts'], now)} ago"
                               + ("" if hb.get("alive") else " (task exited)"))
                sec.health.append(HealthItem(
                    key=f"loop_heartbeat:{loop}", severity=SEVERITY_WARN,
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
        f"{outcomes.get('deduped', 0)} deduped, {outcomes.get('rate_limited', 0)} rate-limited, "
        f"{outcomes.get('quiet_held', 0)} held (quiet hours)")
    if capped:
        top = ", ".join(f"{k}={v}" for k, v in sorted(by_source.items(), key=lambda kv: -kv[1])[:3])
        sec.health.append(HealthItem(
            key="delivery_capped", severity=SEVERITY_WARN,
            text=f"{capped} owner message(s) suppressed by the daily cap in 24h "
                 f"({consumed}/{cap} used; by source: {top})",
            remedy="/missed to read them; raise the cap with `/config set delivery.daily_cap N`"))
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


def _posture_section() -> Section:
    from core.config_policy.posture_card import build_posture_card, render_posture_card
    rows = build_posture_card()
    return Section(name="posture", lines=render_posture_card(rows), data={"rows": rows})


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
        "apps": _guarded("apps", _apps_section, uid, data_dir),
    }
    if not uid:
        for name in ("work", "approvals", "loops", "delivery", "apps"):
            sections[name] = Section(name=name, state=STATE_UNAVAILABLE,
                                     reason="no tenant (empty user_id)")
    return uid, now, window_sec, sections


def build_status_snapshot(user_id: str, *, data_dir: Optional[str] = None,
                          task_agent: Any = None, session_id: Optional[str] = None,
                          container: Any = None, include_balances: bool = False,
                          include_money: bool = True, window_hours: int = 24,
                          ledger: Any = None) -> StatusSnapshot:
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
