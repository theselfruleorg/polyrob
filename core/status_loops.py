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
from typing import Any, Dict, List, Optional

from core.status_types import (  # noqa: F401
    HealthItem, SEVERITY_CRIT, SEVERITY_WARN, Section, _K_AUTONOMY_STARTED,
    _K_AUTONOMY_TICK, _K_CRON_RUN, _K_GOAL_RUN, _K_SELF_WAKE, _K_WALLET_SPEND,
    _PAUSE_HONOURED_OUTCOMES, _age, _hhmm, _tele_rows, _telemetry_db_path, snapshot_rows
    as _rows,
)
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


def _loops_section(user_id: str, cron_db: str, tele: Section, now: float,
                   goals_db: Optional[str] = None, data_dir: Optional[str] = None) -> Section:
    """The ``loops`` section: a driver over one helper per part (W9 split — this
    was one 351-line function). Order is the render order: the pause record,
    the live actors, the standing rail verdicts, cron, then (with the telemetry
    window) the rail ledger, the external-rail health and loop liveness."""
    sec = Section(name="loops")
    _pause_lines(sec, user_id, tele, data_dir)
    _live_actor_lines(sec, user_id, cron_db, goals_db)
    _rail_verdict_lines(sec)
    jobs = _cron_job_lines(sec, user_id, cron_db)
    _next_lines(sec, user_id, jobs, goals_db, data_dir)
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
    return sec


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
    if "cron_reason" not in sec.data:
        enabled = [j for j in jobs if int(j.get("enabled") or 0)]
        upcoming = sorted(j["next_run_at"] for j in enabled if j.get("next_run_at"))
        sec.data.update({"cron_total": len(jobs), "cron_enabled": len(enabled),
                         "next_cron": upcoming[0] if upcoming else None})
        # The live job list for the agent's <operations> block (render_operations_block).
        sec.data["jobs"] = [
            {"id": str(j.get("id") or ""), "name": _job_name(j),
             "schedule": str(j.get("schedule_spec") or ""),
             "next_run_at": j.get("next_run_at"), "status": str(j.get("status") or "")}
            for j in sorted(enabled, key=lambda j: str(j.get("next_run_at") or "~"))]
        line = f"cron: {len(enabled)} enabled / {len(jobs) - len(enabled)} disabled"
        if upcoming:
            line += f"; next {upcoming[0][:16]}"
        sec.lines.append(line)
    return jobs


#: How many upcoming runs the Next list names (080 WS-4).
NEXT_DEFAULT = 5


def autonomy_idle_reason() -> Optional[str]:
    """``"autonomy is off"`` when neither the scheduler nor the goal loop is on,
    else ``None``. The ONE reader the headline (``status_render``) and the Next
    list share, so a seat never says RUNNING over loops that are off. A gate
    that cannot be read is named, never read as on."""
    from core.config_policy import AutonomyConfig
    try:
        cron_on = bool(AutonomyConfig.cron_enabled())
        goals_on = bool(AutonomyConfig.goals_enabled())
    except Exception as e:
        return f"autonomy state unreadable ({type(e).__name__})"
    if not cron_on and not goals_on:
        return "autonomy is off"
    return None


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
              data_dir: Optional[str] = None) -> Dict[str, Any]:
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
    idle = autonomy_idle_reason()
    if idle:
        out["none"] = f"nothing will run: {idle}"
        return out
    cron_on = bool(AutonomyConfig.cron_enabled())
    goals_on = bool(AutonomyConfig.goals_enabled())
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


def next_line(nxt: Dict[str, Any]) -> str:
    """The ONE render of the Next list (status, console, digest, agent_status)."""
    if nxt.get("none") and not nxt.get("unreadable"):
        line = f"next: {nxt['none']}"
    else:
        bits = []
        for it in nxt.get("items") or []:
            if it["kind"] == "job":
                bits.append(f"{it['name']} (job, {str(it['when'])[:16].replace('T', ' ')} UTC)")
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
                        data_dir=data_dir)
    except Exception as e:
        nxt = {"items": [], "none": None, "notes": [], "more": 0,
               "unreadable": [f"next runs: {type(e).__name__}: {str(e)[:80]}"]}
    sec.data["next"] = nxt
    sec.lines.append(next_line(nxt))


def _cron_rail_lines(sec: Section, tele: Section, jobs: List[Dict[str, Any]]) -> None:
    """The window's cron runs: counts, the per-job ledger, its cost, and the
    cut / cut-repeat health items. A cron job is a "job" on every line the owner
    reads (080 WS-4); the ``rails*`` data keys stay for the existing readers."""
    cron_runs = _tele_rows(tele, _K_CRON_RUN)
    by = {}
    for r in cron_runs:
        o = (r["attrs"] or {}).get("outcome") or "?"
        by[o] = by.get(o, 0) + 1
    wakes = len(_tele_rows(tele, _K_SELF_WAKE))
    sec.data.update({"cron_runs_window": by, "self_wakes_window": wakes})
    sec.lines.append("24h: cron " + (", ".join(f"{k}={v}" for k, v in sorted(by.items()))
                                     or "no runs") + f"; self-wakes={wakes}")
    # 056 WS1: the rail ledger, per enabled job — last TERMINAL outcome (done /
    # failed / cut_by_cap / cut_by_restart / held / deferred / skipped) and a
    # health item when a rail was cut. A `started` with no terminal event is
    # rendered as such ("started HH:MM, no end recorded"), never as done.
    # "partial" / "blocked" (2026-10-13 review C-03/C-11): a run that stopped
    # before done() after real work, or that declared BLOCKED, is a terminal
    # outcome of its own — never folded into done or failed.
    _terminal = {"done", "failed", "partial", "blocked", "cut_by_cap", "cut_by_restart",
                 "held", "deferred", "skipped", "provider_rerouted"}
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
                            "duration_s": a.get("duration_s"), "reason": a.get("reason")}
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
    for r in _tele_rows(tele, _K_CRON_RUN):
        a = r["attrs"] or {}
        jid = str(a.get("job_id") or "")
        if not jid or a.get("outcome") == "started":
            continue
        slot = spend_by_job.setdefault(jid, {"usd": 0.0, "runs": 0, "unpriced": 0})
        slot["runs"] += 1
        raw = a.get("spend_usd")
        if raw is None:
            if a.get("outcome") == "skipped":
                continue  # a $0 skip is a real zero, not an unknown
            slot["unpriced"] += 1
            continue
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
            bits = [f"{name}: {last['outcome']} {_hhmm(last['ts'])}"]
            if last.get("steps") is not None:
                bits.append(f"{last['steps']} steps")
            if last.get("duration_s") is not None:
                bits.append(f"{int(float(last['duration_s']) // 60)}m")
            bits.extend(_rail_cost_bits(spend_by_job.get(str(j.get("id") or ""))))
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
                           + (f" (longest completed run {int(need // 60)}m)" if need else ""))
    if repeats:
        sec.data["rail_cut_repeat"] = repeats
        sec.health.append(HealthItem(
            key="rail_cut_repeat", severity=SEVERITY_WARN,
            text="job time limit too small: " + "; ".join(repeats[:4]),
            remedy="raise `polyrob job edit <id> --max-duration` above the longest completed "
                   "run, or narrow the job's rig (`--rig money_rail`) so its steps are shorter"))


def _loop_liveness_lines(sec: Section, tele: Section, now: float,
                         data_dir: Optional[str]) -> None:
    """Loop liveness from the supervisor heartbeat, against the loops this
    process (and every live process) actually started."""
    from core.config_policy import AutonomyConfig
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
                remedy="check the service log (`polyrob service logs`); /restart if the "
                       "loop task exited"))
        elif hb.get("alive") is False or now - float(hb["ts"]) > stale_after:
            hb_bits.append(f"{loop}: last heartbeat {_age(hb['ts'], now)} ago"
                           + ("" if hb.get("alive") else " (task exited)"))
            sec.health.append(HealthItem(
                key=f"loop_silent:{loop}", severity=SEVERITY_WARN,
                text=f"{loop} loop looks DEAD — last heartbeat {_age(hb['ts'], now)} ago"
                     + ("" if hb.get("alive") else " (task exited)"),
                remedy="/restart (or `polyrob service restart`)"))
        else:
            hb_bits.append(f"{loop}: alive ({_age(hb['ts'], now)} ago)")
    if hb_bits:
        sec.lines.append("loops: " + "; ".join(hb_bits))
