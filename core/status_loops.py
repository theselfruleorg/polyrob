"""Status section: ``loops`` — the pause record, the live actors, cron and its
job ledger, the **Next** list (080 WS-4), and loop liveness. Split per concern (W9): ``_loops_section`` is
a short driver over one helper per part.

Moved out of ``core/status_snapshot.py`` (W9, 2026-10-10 structural review)
unchanged. ``core.status_snapshot`` stays the ONE assembler and re-exports
every name here; the store reads go through ``core.status_snapshot._rows``
late-bound, so a test that patches it still reaches this builder.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from core.cron_expect import MISSED as RUN_MISSED, RUN_EFFECTS, run_effect
from core.event_kinds import RESOURCE_HOLD
from core.status_types import (  # noqa: F401
    HealthItem, SEVERITY_CRIT, SEVERITY_WARN, Section, _K_AUTONOMY_STARTED,
    _K_AUTONOMY_TICK, _K_CRON_RUN, _K_GOAL_RUN, _K_SELF_WAKE, _K_WALLET_SPEND,
    _PAUSE_HONOURED_OUTCOMES, _age, _hhmm, _tele_rows, _telemetry_db_path, snapshot_rows
    as _rows,
)
from core.surfaces.tappable import read_token
from core.status_rails import (  # noqa: F401
    _distinct_runs, _effect_violations, _external_rail_health, _rail_cost_bits,
    _rail_gate_health, _rail_verdict_line, _rail_verdicts,
)


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
    local-mode box is routinely a short-lived `polyrob` REPL, not the service. Take
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


def _loops_section(user_id: str, cron_db: str, tele: Section, now: float,
                   goals_db: Optional[str] = None, data_dir: Optional[str] = None) -> Section:
    """The ``loops`` section: a driver over one helper per part (W9 split — this
    was one 351-line function). Order is the render order: the pause record,
    the live actors, the standing rail verdicts, cron, then (with the telemetry
    window) the rail ledger, the external-rail health and loop liveness."""
    sec = Section(name="loops")
    _pause_lines(sec, user_id, tele, data_dir)
    sec.data["started_loops"] = _read_started_loops(sec, data_dir) if tele.available else None
    _live_actor_lines(sec, user_id, cron_db, goals_db)
    sec.data["live_actors"]["started_loops"] = sec.data["started_loops"]
    _rail_verdict_lines(sec)
    jobs = _cron_job_lines(sec, user_id, cron_db)
    _next_lines(sec, user_id, jobs, goals_db, data_dir)
    _held_job_lines(sec, user_id, jobs, goals_db, now)
    if tele.available:
        _cron_rail_lines(sec, tele, jobs)
        # 056 WS4/WS9: an external rail the deploy cannot use is a HEALTH fact, not a
        # blocked goal discovered hours later. The email tool records a 535 as an
        # event (core cannot import tools); the autonomous toolset already stops
        # requesting `email` while the rejection is fresh.
        _external_rail_health(sec, tele)
        _rail_gate_health(sec, tele)
        _loop_liveness_lines(sec, tele, now, data_dir)
    else:
        sec.lines.append(f"24h run/heartbeat counts unavailable ({tele.reason})")
    _resource_pressure_lines(sec, tele, now, data_dir)
    return sec


def _resource_pressure_lines(sec: Section, tele: Section, now: float,
                             data_dir: Optional[str]) -> None:
    """102 T-1: does this process see memory or disk pressure, and did it hold work? An
    unreadable source allows the run, and it reads as such, never as no pressure."""
    from core import memory_pressure
    from core.runtime_paths import data_dir_or_home
    pressure = memory_pressure.read_pressure(data_dir_or_home(data_dir))
    sec.data["resource_pressure"] = {
        "keys": list(pressure.keys), "unread": list(pressure.unread),
        "mem_available_mb": pressure.mem_available_mb, "disk_free_mb": pressure.disk_free_mb}
    # /proc exists only on Linux, and a home with no data dir has no disk to read yet.
    # Neither is a failed read, so neither is a thing to report.
    unread = [u for u in pressure.unread
              if not (u == "MemAvailable" and sys.platform != "linux")
              and not (u == "free disk" and not os.path.isdir(data_dir_or_home(data_dir)))]
    if unread:
        sec.lines.append("resource pressure: could not read " + ", ".join(unread))
    if pressure.hold:
        why = "; ".join(pressure.reasons)
        sec.lines.append(f"resource pressure: new goals and cron jobs wait: {why}")
        sec.health.append(HealthItem(
            key="resource_hold", severity=SEVERITY_WARN,
            text=f"new goals and cron jobs wait: {why}",
            remedy="free memory or disk; the work stays due and starts by itself"))
    elif not pressure.unread:
        sec.lines.append("resource pressure: none")
    holds = _tele_rows(tele, RESOURCE_HOLD) if tele.available else []
    if holds:
        attrs = holds[0]["attrs"] or {}
        sec.lines.append(
            f"resource pressure: {len(holds)} hold events in the window, the last "
            f"{_age(holds[0]['ts'], now)} ago ({attrs.get('runner') or '?'}: "
            f"{', '.join(attrs.get('keys') or []) or '?'})")


def _pause_lines(sec: Section, user_id: str, tele: Section,
                 data_dir: Optional[str]) -> None:
    """The ONE pause record, and a CRIT when autonomous work ran after it."""
    from core.autonomy_control import read_state
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
            # Social posting is judged by the effect scan below: the retired
            # `social_write` kind carried no autonomy flag, so it flagged the
            # OWNER's own post after a pause as a violation.
            kinds = {_K_GOAL_RUN: "goal_run", _K_CRON_RUN: "cron_run", _K_SELF_WAKE: "self_wake",
                     _K_WALLET_SPEND: "wallet_spend"}
            from core.autonomy_control import KIND_SCOPES
            seen = []
            for k, label in kinds.items():
                # 2026-09-23: a `trading` pause flagged a read-only monitor and an
                # X post as CRITICAL, because this loop did no scope test while
                # `_effect_violations` below it does. Narrow ONLY the kinds the
                # KIND_SCOPES table already describes. `goal_run` and
                # `wallet_spend` have no entry, and inventing one would be
                # guessing in the PERMISSIVE direction on the alarm that guards
                # the owner's pause — so they stay unconditional.
                scopes = KIND_SCOPES.get(k)
                if scopes is not None and not any(sc in scopes for sc in st.scopes):
                    continue
                # An owner-direct spend is the owner acting through the agent,
                # which the pause does not bind (tx_guard step 0) — not a breach.
                rows = [r for r in _tele_rows(tele, k)
                        if float(r.get("ts") or 0) > float(st.since)
                        and (r.get("attrs") or {}).get("outcome") not in _PAUSE_HONOURED_OUTCOMES
                        and (r.get("attrs") or {}).get("lane") != "owner_direct"]
                if rows:
                    seen.append(f"{label} ×{_distinct_runs(rows)} (last {_hhmm(rows[0].get('ts'))})")
            seen.extend(_effect_violations(user_id, data_dir, st))
            if seen:
                sec.health.append(HealthItem(
                    key="pause_violation", severity=SEVERITY_CRIT,
                    text="PAUSE VIOLATED — autonomous activity after the pause: " + "; ".join(seen),
                    remedy="check `journalctl -u polyrob.service` for the session; file an incident"))


def _live_actor_lines(sec: Section, user_id: str, cron_db: str,
                      goals_db: Optional[str]) -> None:
    """``sec.data["live_actors"]``: running goals and cron jobs, the live turn."""
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
    # 056 WS3: a live human turn holds the shared workspace (turn.active marker,
    # written by owner_turn/interactive_turn; a dead pid reads as none). Shown
    # so "why did the rail start late" has an answer on every seat.
    try:
        from core.interactive_gate import read_turn_marker
        m = read_turn_marker()
    except Exception:
        m = None
    live["turn"] = m
    if m:
        sec.lines.append(f"turn: {m.get('kind') or 'turn'} active since "
                         f"{_hhmm(m.get('started'))} (session {str(m.get('session_id') or '')[:8]})")
    sec.data["live_actors"] = live


def _rail_verdict_lines(sec: Section) -> None:
    """One line (and maybe a health item) per standing external-rail verdict."""
    # 057 WS-F: standing external-rail verdicts come from the durable store
    # (verdicts.db, first_seen/last_seen/count), not from the 24 h telemetry
    # window — the email tool no longer re-probes SMTP every poll, so the
    # window would go blind on a CONTINUING outage. A row exists only while
    # the rail is unresolved; a success clears it.
    for v in _rail_verdicts():
        line, sev, key = _rail_verdict_line(v)
        sec.lines.append(line)
        if v.kind == "smtp":
            sec.data["email_auth_rejected_at"] = v.last_seen
        if sev is not None:
            sec.health.append(HealthItem(key=key, severity=sev, text=line,
                                         remedy=v.remedy or ""))


def _cron_job_lines(sec: Section, user_id: str, cron_db: str) -> List[Dict[str, Any]]:
    """The cron store's jobs (``[]`` when it cannot be read, with the reason)."""
    # The cron store may not exist yet (fresh install, or a posture without
    # cron). That is a fact about cron, not about the pause state above: report
    # it inside the section rather than making the whole section unreadable.
    try:
        jobs = _rows(cron_db, "SELECT id, task, enabled, status, next_run_at, last_run_at, "
                              "schedule_spec, payload FROM cron_jobs WHERE user_id=?",
                     (user_id,))
    except Exception as e:
        sec.data["cron_reason"] = f"{type(e).__name__}: {str(e)[:120]}"
        sec.lines.append(f"cron: unavailable ({sec.data['cron_reason']})")
        jobs = []
        if os.path.exists(cron_db or ""):
            # The store is there and the read failed: that is a fault, not a
            # fresh install (116 W5.8).
            sec.health.append(HealthItem(
                key="cron_store_unreadable", severity=SEVERITY_WARN,
                text=f"the job store cannot be read ({sec.data['cron_reason']}) — "
                     f"the job list and the next runs are unknown",
                remedy="`polyrob doctor` (file permissions, a locked or damaged cron.db)"))
    if "cron_reason" not in sec.data:
        enabled = [j for j in jobs if int(j.get("enabled") or 0)]
        upcoming = sorted(j["next_run_at"] for j in enabled if j.get("next_run_at"))
        sec.data.update({"cron_total": len(jobs), "cron_enabled": len(enabled),
                         "next_cron": upcoming[0] if upcoming else None})
        # The live job list for the agent's <operations> block (render_operations_block).
        # Routines review 2026-10-29: with how its last run ended and where it
        # reports — the agent saw "next 10-31" and not "failed 10-28, nothing posted".
        sec.data["jobs"] = [
            {"id": str(j.get("id") or ""), "name": _job_name(j),
             "schedule": str(j.get("schedule_spec") or ""),
             "next_run_at": j.get("next_run_at"), "status": str(j.get("status") or ""),
             "last_outcome": _job_payload(j).get("last_outcome"),
             "last_run_at": j.get("last_run_at"),
             "deliver": _job_payload(j).get("deliver")}
            for j in sorted(enabled, key=lambda j: str(j.get("next_run_at") or "~"))]
        line = f"cron: {len(enabled)} enabled / {len(jobs) - len(enabled)} disabled"
        if upcoming:
            line += f"; next {_when_text(upcoming[0])}"
        sec.lines.append(line)
    return jobs


#: How many upcoming runs the Next list names (080 WS-4).
NEXT_DEFAULT = 5


def autonomy_gates(started_loops: Optional[List[str]] = None) -> tuple:
    """``(cron_on, goals_on)``. The loops a LIVE process recorded starting win;
    this process's own gates are read only for a loop no live process named —
    they are a fact of the RENDERING process (a shell, a doctor run), not of
    the service. Raises when a gate cannot be read."""
    from core.config_policy import AutonomyConfig
    started = set(started_loops or ())
    return ("cron" in started or bool(AutonomyConfig.cron_enabled()),
            "goals" in started or bool(AutonomyConfig.goals_enabled()))


def autonomy_idle_reason(started_loops: Optional[List[str]] = None) -> Optional[str]:
    """``"autonomy is off"`` when neither the scheduler nor the goal loop is on,
    else ``None``. The ONE reader the headline (``status_render``) and the Next
    list share, so a seat never says RUNNING over loops that are off. A gate
    that cannot be read is named, never read as on."""
    try:
        cron_on, goals_on = autonomy_gates(started_loops)
    except Exception as e:
        return f"autonomy state unreadable ({type(e).__name__})"
    if not cron_on and not goals_on:
        return "autonomy is off"
    return None


def _job_payload(job: Dict[str, Any]) -> Dict[str, Any]:
    """The job row's payload; an unreadable one says so in its last outcome."""
    try:
        p = json.loads(job.get("payload") or "{}") or {}
    except Exception as e:
        return {"last_outcome": {"outcome": "unknown",
                                 "reason": f"payload unreadable: {type(e).__name__}"}}
    return p if isinstance(p, dict) else {}


def _job_name(job: Dict[str, Any]) -> str:
    try:
        p = json.loads(job.get("payload") or "{}") or {}
    except Exception as e:
        p = {"name": f"(payload unreadable: {type(e).__name__})"}
    name = (p.get("name") if isinstance(p, dict) else None) or ""
    if not name:
        lines = str(job.get("task") or "").strip().splitlines()
        name = lines[0] if lines else str(job.get("id") or "?")
    name = str(name).strip()
    return name if len(name) <= 48 else name[:47] + "…"


def next_runs(user_id: str, jobs: List[Dict[str, Any]], cron_reason: Optional[str],
              goals_db: Optional[str], *, n: int = NEXT_DEFAULT,
              data_dir: Optional[str] = None,
              started_loops: Optional[List[str]] = None) -> Dict[str, Any]:
    """The next *n* runs BY NAME: enabled cron jobs by ``next_run_at`` and the
    ready-goal queue in dispatch order (priority, then age — the board's own
    ``idx_goals_ready`` order). ``none`` is ONE sentence when nothing will run
    (paused / autonomy off / scheduler off / nothing queued); ``unreadable``
    names a store that did not answer — never read as "nothing queued"."""
    from core.autonomy_control import read_state
    from core.config_policy import AutonomyConfig
    out: Dict[str, Any] = {"items": [], "none": None, "notes": [], "unreadable": [],
                           "more": 0}
    st = read_state(data_dir)
    if st.paused and "all" in st.scopes:
        out["none"] = "nothing will run: paused (everything)"
        return out
    idle = autonomy_idle_reason(started_loops)
    if idle:
        out["none"] = f"nothing will run: {idle}"
        return out
    cron_on, goals_on = autonomy_gates(started_loops)
    cands: List[tuple] = []
    enabled = [j for j in (jobs or []) if int(j.get("enabled") or 0)]
    if cron_reason:
        out["unreadable"].append(f"jobs: {cron_reason}")
    elif cron_on:
        for j in enabled:
            when = j.get("next_run_at")
            if when:
                cands.append((str(when)[:16], "job", _job_name(j), str(when)))
    elif enabled:
        out["notes"].append(f"scheduler is off: {len(enabled)} enabled job(s) will not run")
    if goals_on:
        try:
            rows = _rows(goals_db or "", "SELECT id, title FROM goals WHERE kind='goal' "
                                         "AND status='ready' AND user_id=? "
                                         "ORDER BY priority DESC, created_at", (user_id,))
        except FileNotFoundError:
            rows = []  # a fresh install has no board: nothing is queued
        except Exception as e:
            rows = []
            out["unreadable"].append(f"goals: {type(e).__name__}: {str(e)[:80]}")
        for i, r in enumerate(rows):
            # A ready goal runs when the goal loop is next free: it sorts before
            # any timed job ("" < any ISO stamp), in the board's own order.
            cands.append(("", "goal", str(r.get("title") or r.get("id") or "?")[:48], i))
    if st.paused:
        out["notes"].append(f"paused: {', '.join(st.scopes)}")
    cands.sort(key=lambda c: (c[0], c[3] if c[1] == "goal" else 0))
    out["items"] = [{"kind": k, "name": name, "when": (when if k == "job" else None)}
                    for when, k, name, _ in cands[:max(1, n)]]
    out["more"] = max(0, len(cands) - max(1, n))
    if not out["items"] and not out["unreadable"]:
        out["none"] = ("nothing will run: scheduler is off" if not cron_on and enabled
                       else "nothing will run: no enabled job and no ready goal")
    return out


def _when_text(when: Any) -> str:
    """A job's ``next_run_at`` with the clock it is written in. A stamp with no
    zone is the SERVER clock, which is not always UTC (116 W5.14)."""
    text = str(when or "")
    body = text[:16].replace("T", " ")
    tail = text[16:]
    if tail.endswith("Z") or "+00:00" in tail:
        return body + " UTC"
    if "+" in tail or "-" in tail:
        return body + " " + tail[tail.find("+") if "+" in tail else tail.rfind("-"):]
    from core.cron_slots import server_clock
    return f"{body} {server_clock()}"


def _run_length(seconds: Any) -> str:
    """``42s`` under a minute, else whole minutes (a 40 s run is not "0m")."""
    sec = float(seconds)
    return f"{int(sec)}s" if sec < 60 else f"{int(sec // 60)}m"


def next_line(nxt: Dict[str, Any]) -> str:
    """The ONE render of the Next list (status, console, digest, agent_status)."""
    if nxt.get("none") and not nxt.get("unreadable"):
        line = f"next: {nxt['none']}"
    else:
        bits = []
        for it in nxt.get("items") or []:
            if it["kind"] == "job":
                bits.append(f"{it['name']} (job, {_when_text(it['when'])})")
            else:
                bits.append(f"{it['name']} (goal, when the goal loop is free)")
        line = "next: " + ("; ".join(bits) if bits else "unknown")
        if nxt.get("more"):
            line += f"; +{nxt['more']} more"
    for u in nxt.get("unreadable") or []:
        line += f" · could not read {u}"
    for note in nxt.get("notes") or []:
        line += f" · {note}"
    return line


def _next_lines(sec: Section, user_id: str, jobs: List[Dict[str, Any]],
                goals_db: Optional[str], data_dir: Optional[str]) -> None:
    """``sec.data["next"]`` and its one line (080 WS-4)."""
    try:
        nxt = next_runs(user_id, jobs, sec.data.get("cron_reason"), goals_db,
                        data_dir=data_dir, started_loops=sec.data.get("started_loops"))
    except Exception as e:
        nxt = {"items": [], "none": None, "notes": [], "more": 0,
               "unreadable": [f"next runs: {type(e).__name__}: {str(e)[:80]}"]}
    sec.data["next"] = nxt
    sec.lines.append(next_line(nxt))


def _held_job_lines(sec: Section, user_id: str, jobs: List[Dict[str, Any]],
                    goals_db: Optional[str], now: float) -> None:
    """One line per job an open question of the owner holds (116 W2.3): the job
    does not run until the question is answered or expires, and "next" alone
    would not say why."""
    enabled = [j for j in jobs if int(j.get("enabled") or 0)]
    if not enabled or not goals_db or not os.path.exists(goals_db):
        return
    from core.cron_expect import holding_ask
    from core.goal_vocab import ASK_OPEN, KIND_ASK
    try:
        asks = _rows(goals_db, "SELECT id, title, payload FROM goals WHERE kind=? AND status=? "
                               "AND user_id=? ORDER BY created_at", (KIND_ASK, ASK_OPEN, user_id))
    except Exception as e:
        sec.lines.append(f"held jobs: unavailable ({type(e).__name__}: {str(e)[:80]})")
        sec.data["held_jobs_reason"] = f"{type(e).__name__}: {str(e)[:80]}"
        return
    held = []
    for j in enabled:
        h = holding_ask(asks, str(j.get("id") or ""), now=now)
        if not h:
            continue
        held.append({"job_id": str(j.get("id") or ""), "name": _job_name(j), **h})
        via = " (set after idle runs)" if h.get("via") == "auto" else ""
        sec.lines.append(
            f"job {_job_name(j)[:28]}: held by your question{via} — "
            f"{str(h.get('question') or '')[:70]} · {read_token('/ask', h['ask_id']) or '/asks'}")
    sec.data["held_jobs"] = held


def _cron_rail_lines(sec: Section, tele: Section, jobs: List[Dict[str, Any]]) -> None:
    """The window's cron runs: counts, the per-job ledger, its cost, and the
    cut / cut-repeat health items. A cron job is a "job" on every line the owner
    reads (080 WS-4); the ``rails*`` data keys stay for the existing readers."""
    cron_runs = _tele_rows(tele, _K_CRON_RUN)
    by: Dict[str, int] = {}
    effects: Dict[str, int] = {}
    missed_jobs: Dict[str, int] = {}
    for r in cron_runs:
        a = r["attrs"] or {}
        o = a.get("outcome") or "?"
        by[o] = by.get(o, 0) + 1
        if o == "done":
            eff = run_effect(a)
            effects[eff] = effects.get(eff, 0) + 1
            if eff == RUN_MISSED:
                jid = str(a.get("job_id") or "?")
                missed_jobs[jid] = missed_jobs.get(jid, 0) + 1
    # A start is not an ending, and a `provider_rerouted` marker is not a run
    # (116 W2.6): they are counted apart, and a start that no later row of its
    # job closed is the one number that shows a lost row.
    starts = by.pop("started", 0)
    rerouted = by.pop("provider_rerouted", 0)
    no_end = _starts_without_end(cron_runs, now=time.time())
    wakes: Dict[str, int] = {}
    for r in _tele_rows(tele, _K_SELF_WAKE):
        a = r["attrs"] or {}
        o = str(a.get("outcome") or "?")
        if o == "skipped" and a.get("reason") == "budget_exhausted":
            o = "budget"
        wakes[o] = wakes.get(o, 0) + 1
    sec.data.update({"cron_runs_window": by, "cron_starts_window": starts,
                     "cron_no_end_window": no_end, "cron_effects_window": effects,
                     "self_wakes_window": wakes.get("fired", 0),
                     "self_wakes_by_outcome": wakes})
    ends = []
    for k, v in sorted(by.items()):
        if k == "done" and effects:
            ends.append(f"done={v} (" + ", ".join(
                f"{name} {effects[name]}" for name in RUN_EFFECTS if effects.get(name)) + ")")
        else:
            ends.append(f"{k}={v}")
    line = "24h job runs ended: " + (", ".join(ends) or "none") + f"; started={starts}"
    if no_end:
        line += f", {no_end} started with no end recorded"
    if rerouted:
        line += f"; provider rerouted ×{rerouted} (not a run)"
    other = ", ".join(f"{k} {v}" for k, v in sorted(wakes.items()) if k != "fired")
    line += f"; self-wakes fired={wakes.get('fired', 0)}" + (f" ({other})" if other else "")
    sec.lines.append(line)
    if missed_jobs:
        names = {str(j.get("id") or ""): _job_name(j) for j in jobs}
        sec.health.append(HealthItem(
            key="runs_missed", severity=SEVERITY_WARN,
            text="job said done and its check did not hold: " + "; ".join(
                f"{names.get(jid, jid)[:28]} ×{n}" for jid, n in sorted(missed_jobs.items())[:4]),
            remedy="`polyrob job show <id>` (what it expects), then fix the job or its `expect`"))
    # 056 WS1: the rail ledger, per enabled job — last TERMINAL outcome (done /
    # failed / cut_by_cap / cut_by_restart / held / deferred / skipped) and a
    # health item when a rail was cut. A `started` with no terminal event is
    # rendered as such ("started HH:MM, no end recorded"), never as done.
    # "partial" / "blocked" (2026-10-13 review C-03/C-11): a run that stopped
    # before done() after real work, or that declared BLOCKED, is a terminal
    # outcome of its own — never folded into done or failed.
    _terminal = {"done", "failed", "partial", "blocked", "cut_by_cap", "cut_by_restart",
                 "held", "deferred", "skipped"}
    rails: Dict[str, Dict[str, Any]] = {}
    for r in cron_runs:  # newest-first
        a = r.get("attrs") or {}
        jid = str(a.get("job_id") or "")
        if not jid:
            continue
        slot = rails.setdefault(jid, {})
        o = a.get("outcome")
        if o in _terminal and "last" not in slot:
            slot["last"] = {"outcome": o, "ts": r.get("ts"), "steps": a.get("steps"),
                            "duration_s": a.get("duration_s"), "reason": a.get("reason"),
                            "effect": run_effect(a) if o == "done" else None,
                            "hold": a.get("hold"), "held_by": a.get("held_by")}
        if o == "started" and "last_started" not in slot:
            slot["last_started"] = r.get("ts")
    sec.data["rails"] = rails
    # What each rail COST in the window. The events have always carried
    # `spend_usd`; nothing rendered it, so "which rail should I thin?" had
    # no answer but a hand-written sqlite query (2026-09-22, the OpenRouter
    # sub-$3 note). ⚠️ A run with NO figure recorded is counted as UNPRICED
    # rather than as $0 — folding it into the total would understate the
    # bill, and a total that is quietly too small is worse than one that
    # admits what it could not price. A `skipped` run really did cost
    # nothing, so it prices as a true zero.
    spend_by_job: Dict[str, Dict[str, Any]] = {}
    for r in cron_runs:
        a = r["attrs"] or {}
        jid = str(a.get("job_id") or "")
        o = a.get("outcome")
        if not jid or o in ("started", "provider_rerouted"):
            continue
        slot = spend_by_job.setdefault(jid, {"usd": 0.0, "runs": 0, "unpriced": 0,
                                             "skipped": 0})
        if o == "skipped" or a.get("hold"):
            slot["skipped"] += 1    # no model turn: not a run, and a true $0
            continue
        slot["runs"] += 1
        # 116 W2.6: `cost_usd` is the ledger figure and is ABSENT when it was
        # not read. `spend_usd` reads 0.0 for a failed read too, so only a
        # figure above zero is taken from it (a row from an older build).
        raw = a.get("cost_usd")
        if raw is None:
            raw = a.get("spend_usd") or None
        try:
            slot["usd"] += float(raw)
        except (TypeError, ValueError):
            slot["unpriced"] += 1
    sec.data["rails_spend_24h"] = spend_by_job
    sec.data["rails_spend_24h_total_usd"] = round(
        sum(v["usd"] for v in spend_by_job.values()), 4)
    cut = []
    # 057 WS-C: a job cut TWICE in the window is not a fluke — name the cap
    # it needs (its own longest completed run) rather than the last cut.
    cuts_per_job: Dict[str, int] = {}
    longest_done: Dict[str, float] = {}
    for r in _tele_rows(tele, _K_CRON_RUN):
        a = r["attrs"] or {}
        jid = str(a.get("job_id") or "")
        if a.get("outcome") == "cut_by_cap":
            cuts_per_job[jid] = cuts_per_job.get(jid, 0) + 1
        elif a.get("outcome") == "done" and a.get("duration_s") is not None:
            longest_done[jid] = max(longest_done.get(jid, 0.0), float(a["duration_s"]))
    for j in (jobs if "cron_reason" not in sec.data else []):
        if not int(j.get("enabled") or 0):
            continue
        slot = rails.get(str(j.get("id") or ""))
        if not slot:
            continue
        name = (j.get("task") or "")[:28].strip()
        last, ls = slot.get("last"), slot.get("last_started")
        if last and (ls is None or float(last["ts"] or 0) >= float(ls or 0)):
            word = last["outcome"]
            if last.get("hold"):
                word = "held by your question" + (
                    " (set after idle runs)" if last["hold"] == "auto" else "")
            elif last.get("effect"):
                word += f" ({last['effect']})"
            bits = [f"{name}: {word} {_hhmm(last['ts'])}"]
            if last.get("steps") is not None:
                bits.append(f"{last['steps']} steps")
            if last.get("duration_s") is not None:
                bits.append(_run_length(last['duration_s']))
            cost = spend_by_job.get(str(j.get("id") or "")) or {}
            bits.extend(_rail_cost_bits(cost))
            if cost.get("skipped"):
                bits.append(f"{cost['skipped']} slot(s) skipped in 24h")
            sec.lines.append("job " + " · ".join(bits))
            if last["outcome"] in ("cut_by_cap", "cut_by_restart"):
                cut.append(f"{name} {last['outcome']} at {_hhmm(last['ts'])}")
        elif ls is not None:
            sec.lines.append(f"job {name}: started {_hhmm(ls)}, no end recorded")
    if cut:
        sec.health.append(HealthItem(
            key="rail_cut", severity=SEVERITY_WARN,
            text="job run cut: " + "; ".join(cut[:4]),
            remedy="cut_by_cap → widen `polyrob job edit <id> --max-duration`; "
                   "cut_by_restart → deploy only on an idle box (no scheduled job, goal or turn)"))
    repeats = []
    for j in (jobs if "cron_reason" not in sec.data else []):
        jid = str(j.get("id") or "")
        if int(j.get("enabled") or 0) and cuts_per_job.get(jid, 0) >= 2:
            name = (j.get("task") or "")[:28].strip()
            need = longest_done.get(jid)
            repeats.append(f"{name} cut {cuts_per_job[jid]}× in 24h"
                           + (f" (longest completed run {_run_length(need)})" if need else ""))
    if repeats:
        sec.data["rail_cut_repeat"] = repeats
        sec.health.append(HealthItem(
            key="rail_cut_repeat", severity=SEVERITY_WARN,
            text="job time limit too small: " + "; ".join(repeats[:4]),
            remedy="raise `polyrob job edit <id> --max-duration` above the longest completed "
                   "run, or narrow the job's rig (`--rig money_rail`) so its steps are shorter"))


#: A start this old with no ending is a lost row; a newer one may still run.
NO_END_AFTER_SEC = 6 * 3600


def _starts_without_end(cron_runs: List[Dict[str, Any]], *, now: float) -> int:
    """Starts that no later row of the same job closed (rows NEWEST FIRST). The
    newest open start of a job counts only when it is older than
    ``NO_END_AFTER_SEC``: before that the run may still be live."""
    open_by_job: Dict[str, Optional[float]] = {}
    lost = 0
    for r in reversed(cron_runs):
        a = r.get("attrs") or {}
        jid, o = str(a.get("job_id") or ""), a.get("outcome")
        if not jid or o == "provider_rerouted":
            continue
        if o == "started":
            if open_by_job.get(jid) is not None:
                lost += 1
            open_by_job[jid] = float(r.get("ts") or 0)
        elif open_by_job.get(jid) is not None:
            open_by_job[jid] = None
    return lost + sum(1 for ts in open_by_job.values()
                      if ts is not None and now - ts > NO_END_AFTER_SEC)


#: A tick still running after this long is reported (a goal run inside a tick
#: is long by design; a tick that never returns is a hung loop).
TICK_STUCK_SEC = 6 * 3600
#: This many failed ticks in a row is a loop that does no work.
TICK_ERROR_STREAK = 3


def latest_heartbeats(tick_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """``{loop: heartbeat}`` from ``autonomy_tick`` rows (NEWEST FIRST): the
    newest row per (process, loop), then per loop the newest row of a LIVE
    process. A loop whose only rows come from a process that is gone keeps its
    newest row with ``proc_gone=True`` — a second process (a REPL beside the
    service) can no longer mask it, and a dead process is not "alive"."""
    per: Dict[tuple, Dict[str, Any]] = {}
    for r in tick_rows:
        a = r.get("attrs") or {}
        key = (a.get("pid"), a.get("loop") or "?")
        if key not in per:
            per[key] = {"ts": r["ts"], "alive": a.get("alive"), "pid": a.get("pid"),
                        "lag": a.get("loop_lag_ms_max"), "tick_ok_at": a.get("tick_ok_at"),
                        "tick_running_since": a.get("tick_running_since"),
                        "tick_errors": a.get("tick_errors"), "tick_error": a.get("tick_error"),
                        "interval_s": a.get("interval_s"), "tracked": "tick_ok_at" in a}
    gone: Dict[Any, bool] = {}
    out: Dict[str, Dict[str, Any]] = {}
    for (pid, loop), hb in per.items():   # insertion order = newest first
        if pid not in gone:
            gone[pid] = isinstance(pid, int) and not _pid_alive(pid)
        hb["proc_gone"] = gone[pid]
        cur = out.get(loop)
        if cur is None or (cur["proc_gone"] and not hb["proc_gone"]):
            out[loop] = hb
    return out


def loop_verdict(hb: Optional[Dict[str, Any]], now: float, stale_after: float) -> tuple:
    """``(state, detail)`` for one loop — the ONE liveness rule (the headline
    count, the health item and the host health check all call it).

    ``alive`` · ``silent`` (no heartbeat) · ``dead`` (the task exited, the
    process is gone, or the heartbeat is stale) · ``failing`` (the last
    ``TICK_ERROR_STREAK`` ticks raised) · ``stuck`` (no completed tick within
    two intervals and none running, or one tick running over
    ``TICK_STUCK_SEC``). A heartbeat from a process that does not record ticks
    (an older build during a deploy) is judged by task and age alone."""
    if hb is None:
        return ("silent", "no heartbeat in 24h")
    age = _age(hb["ts"], now)
    if hb.get("proc_gone"):
        return ("dead", f"last heartbeat {age} ago (process {hb.get('pid')} is gone)")
    if hb.get("alive") is False:
        return ("dead", f"last heartbeat {age} ago (task exited)")
    if now - float(hb["ts"]) > stale_after:
        return ("dead", f"last heartbeat {age} ago")
    if not hb.get("tracked"):
        return ("alive", f"alive ({age} ago)")
    errors = int(hb.get("tick_errors") or 0)
    if errors >= TICK_ERROR_STREAK:
        return ("failing", f"the last {errors} ticks failed: {hb.get('tick_error') or '?'}")
    running = hb.get("tick_running_since")
    hb_ts = float(hb["ts"])
    if running:
        if hb_ts - float(running) > TICK_STUCK_SEC:
            return ("stuck", f"one tick has run for {_age(running, hb_ts)}")
        return ("alive", f"alive (tick running since {_hhmm(running)})")
    ok_at = hb.get("tick_ok_at")
    allowed = max(stale_after, 2 * float(hb.get("interval_s") or 0))
    if ok_at is None:
        return (("alive", f"alive ({age} ago)") if errors == 0
                else ("failing", f"no tick has completed: {hb.get('tick_error') or '?'}"))
    if hb_ts - float(ok_at) > allowed:
        return ("stuck", f"no completed tick for {_age(ok_at, hb_ts)}")
    return ("alive", f"alive (last tick {_age(ok_at, now)} ago)")


def loops_health(data_dir: Optional[str] = None, *, now: Optional[float] = None,
                 window_sec: int = 86400) -> List[tuple]:
    """``[(loop, state, detail)]`` for every loop a live process started — the
    snapshot's liveness verdict for a caller that builds no snapshot (the host
    health check). Raises when the telemetry store cannot be read: a caller
    must not turn that into "healthy"."""
    import time as _time
    from core.env import int_env
    now = _time.time() if now is None else now
    rows = _rows(_telemetry_db_path(data_dir),
                 "SELECT ts, attrs FROM telemetry_events WHERE kind=? AND ts >= ? "
                 "ORDER BY ts DESC LIMIT 20000", (_K_AUTONOMY_TICK, now - window_sec))
    for r in rows:
        try:
            r["attrs"] = json.loads(r.get("attrs") or "{}") or {}
        except Exception:
            r["attrs"] = {}
    latest = latest_heartbeats(rows)
    probe = Section(name="loops")
    expected = _read_started_loops(probe, data_dir)
    if expected is None:
        expected = sorted(latest)
    stale_after = max(900, 2 * int_env("AUTONOMY_HEARTBEAT_INTERVAL_SEC", 300))
    return [(loop,) + loop_verdict(latest.get(loop), now, stale_after) for loop in expected]


def loop_lag_window(tick_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The event-loop lag over the rows' window: the maximum, the stalls over
    1 s, the newest window's value and the blocking site of the worst stall.
    ``measured`` is False when no row carried a value (never "no lag")."""
    seen: set = set()
    out: Dict[str, Any] = {"measured": False, "max_ms": 0, "stalls": 0,
                           "latest_ms": None, "site": None}
    for r in tick_rows:                       # newest first
        a = r.get("attrs") or {}
        lag = a.get("loop_lag_ms_max")
        if not isinstance(lag, (int, float)) or isinstance(lag, bool):
            continue
        # One emission writes the same window on every loop's row: count it once.
        key = (a.get("pid"), a.get("hb") if a.get("hb") is not None
               else int(float(r.get("ts") or 0) // 5))
        if key in seen:
            continue
        seen.add(key)
        if not out["measured"]:
            out["measured"], out["latest_ms"] = True, int(lag)
        stalls = a.get("loop_stalls")
        out["stalls"] += int(stalls) if isinstance(stalls, int) else (1 if lag > 1000 else 0)
        if lag > out["max_ms"]:
            out["max_ms"] = int(lag)
            out["site"] = a.get("loop_stall_site") or out["site"]
        elif out["site"] is None and a.get("loop_stall_site"):
            out["site"] = a.get("loop_stall_site")
    return out


def _ms_text(ms: int) -> str:
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms} ms"


def _loop_lag_lines(sec: Section, tick_rows: List[Dict[str, Any]]) -> None:
    """102 C-13 / 116 W4.3: the WINDOW's worst event-loop stall (the newest
    5-minute value alone said `max 2035 ms` on a day with 25 s stalls). A
    window with no value was NOT measured, and that never reads as no lag."""
    win = loop_lag_window(tick_rows)
    if not win["measured"]:
        if tick_rows:
            sec.lines.append("event-loop lag: not measured")
        return
    worst = win["max_ms"]
    sec.data["loop_lag_ms_max"] = worst
    sec.data["loop_lag"] = win
    sec.lines.append(
        f"event-loop lag: max {_ms_text(worst)} in 24h, {win['stalls']} stall(s) over 1 s "
        f"(latest window {_ms_text(int(win['latest_ms'] or 0))})")
    if worst > 1000:
        site = win.get("site")
        sec.health.append(HealthItem(
            key="loop_lag", severity=SEVERITY_WARN,
            text=(f"the event loop stalled for {_ms_text(worst)} ({win['stalls']} stall(s) over "
                  f"1 s in 24h): every session stops for that time"
                  + (f"; blocked at {site}" if site else "")),
            remedy=("move that call off the event loop" if site else
                    "find `event loop stuck` in the service log; it names the blocking call")))


def _read_started_loops(sec: Section, data_dir: Optional[str]) -> Optional[List[str]]:
    """The loops every LIVE process started (``autonomy_started``), or None when
    no row exists or the record cannot be read (a health item says so). Read
    ONCE per snapshot: loop liveness, the Next list and the headline use it, so
    a seat whose own env has autonomy off (a shell beside the service) does
    not print IDLE over a running service (116 W4.4)."""
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
    return started_loops


def _loop_liveness_lines(sec: Section, tele: Section, now: float,
                         data_dir: Optional[str]) -> None:
    """Loop liveness from the supervisor heartbeat, against the loops this
    process (and every live process) actually started."""
    from core.config_policy import AutonomyConfig
    # Loop liveness from the supervisor heartbeat (autonomy_tick). A loop
    # that should run but has no fresh heartbeat is reported as such — a
    # dead ticker must never render identically to a healthy one.
    tick_rows = _tele_rows(tele, _K_AUTONOMY_TICK)   # newest-first
    latest = latest_heartbeats(tick_rows)
    sec.data["heartbeats"] = latest
    gate_expected = []
    try:
        if AutonomyConfig.cron_enabled():  # the runtime's own gate
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
    # where the owner opens a `polyrob` REPL beside the service — what local
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
    #     STARTS, so a couple of dozen short `polyrob` REPL runs push the
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
    started_loops = sec.data.get("started_loops")
    if started_loops is not None:
        expected = sorted(set(started_loops) | set(gate_expected))
    else:
        expected = gate_expected
    from core.env import int_env
    stale_after = max(900, 2 * int_env("AUTONOMY_HEARTBEAT_INTERVAL_SEC", 300))
    hb_bits = []
    verdicts: Dict[str, bool] = {}
    for loop in expected:
        hb = latest.get(loop)
        state, detail = loop_verdict(hb, now, stale_after)
        verdicts[loop] = state == "alive"
        hb_bits.append(f"{loop}: {detail}")
        if state == "silent":
            sec.health.append(HealthItem(
                key=f"loop_silent:{loop}", severity=SEVERITY_WARN,
                text=f"{loop} loop: no liveness heartbeat recorded in 24h (dead, or "
                     f"this process does not emit heartbeats)",
                remedy="check the service log (`polyrob service logs`); /restart if the "
                       "loop task exited"))
        elif state == "dead":
            sec.health.append(HealthItem(
                key=f"loop_silent:{loop}", severity=SEVERITY_WARN,
                text=f"{loop} loop looks DEAD — {detail}",
                remedy="/restart (or `polyrob service restart`)"))
        elif state in ("failing", "stuck"):
            sec.health.append(HealthItem(
                key=f"loop_{state}:{loop}", severity=SEVERITY_WARN,
                text=f"{loop} loop does no work — {detail}",
                remedy="check the service log (`polyrob service logs`) for `ticker tick "
                       "failed`; /restart if it does not recover"))
    # The headline count uses the SAME verdict as the health items above, over
    # the loops that should run (it counted every loop that had a row).
    sec.data["live_actors"]["loops_alive"] = verdicts
    sec.data["live_actors"]["loops_checked"] = bool(tele.available)
    if hb_bits:
        sec.lines.append("loops: " + "; ".join(hb_bits))
    _loop_lag_lines(sec, tick_rows)
