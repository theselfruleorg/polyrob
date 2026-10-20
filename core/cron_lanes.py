"""A cron LANE and the owner's stop of it (work order 0011: an owner stop is state).

The owner used to stop a cron purpose ("no more buybacks") by cancelling one job
and writing a prose rule into ``owner.md``. Nothing in code read that rule, so a
new job in the same purpose was one model turn away. This module makes the stop
a row the scheduler reads.

- :func:`job_lane` is the ONE lane rule (owner decision 2026-10-15): the job's
  first pinned skill (``payload.skills[0]``), else its rig (``payload.rig``),
  else ``""`` — no lane, so no stop applies. No explicit field, no text match.
- The stop lives in ``core.goal_suppressions`` (scope ``cron_lane``, the same
  table as ``/goal stop --forever``), in the ``goals.db`` beside ``cron.db``.
- :func:`lane_gate` runs inside ``CronService.schedule`` over every pinned
  skill and the rig (:func:`gated_lanes`): a create that is not the owner's
  touching a stopped lane is REFUSED (:class:`LaneStopped`) and names the stop;
  the owner's own create (a typed seat — never the agent's tool, even on an
  owner turn) succeeds and lifts the stop.
- :func:`stop_job` (``/job stop <id> --forever``) writes it; :func:`allow_lane`
  (``/job allow <lane>``) removes it. ``prune_cancelled`` never reads this table.
- :func:`check_pinned_skills` (081 WS-5.4) is the ONE pin validator: a cron job
  write and a goal write that pin a skill id no scope serves are refused
  (:class:`UnknownPinnedSkill`). A pin that goes missing later is named at the
  top of the run's task (:func:`missing_skills_notice`), never dropped silently.
  A pinned id is the lane key, so a pinned skill is never renamed or retired.

The ``/pause cron`` half (owner decision 2026-10-15): a job created while its
run kind is paused is stored PAUSED (``payload.paused.by == "pause_scope"``) and
``cron.scheduler.release_scope_paused`` (the scheduler tick) resumes it once the pause is
lifted. ``/job resume <id>`` resumes it at once, as for any paused job.

Core, not ``cron/``: the agent's ``cronjob`` tool (tools tier) and the owner
seats import it (layering ratchet). ``release_scope_paused`` lives in
``cron/scheduler.py``, the one caller.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("core.cron_lanes")

#: ``payload.paused.by`` of a job created while ``/pause cron`` (or all) was on.
PAUSE_SCOPE_BY = "pause_scope"
#: The ``via`` an agent cancel records when the turn is a genuine owner turn.
OWNER_TURN_VIA = "owner_turn"


class LaneStopped(ValueError):
    """A create refused because the owner stopped the job's lane. (Core cannot
    import ``cron.schedule.ScheduleError``; a seat catches this one by name.)"""


class UnknownPinnedSkill(LaneStopped):
    """A job or goal write refused at the lane gate: a pinned skill id names no
    skill. A subclass so every seat that catches :class:`LaneStopped` shows it."""


def job_lane(payload: Optional[Dict[str, Any]]) -> str:
    """The job's lane: first pinned skill, else its rig, else ``""``."""
    p = payload if isinstance(payload, dict) else {}
    skills = p.get("skills")
    if isinstance(skills, (list, tuple)) and skills:
        first = str(skills[0] or "").strip().lower()
        if first:
            return first
    return str(p.get("rig") or "").strip().lower()


def _stops_db(store) -> str:
    from core.goal_suppressions import stops_db_beside
    return stops_db_beside(store.db_path)


def _refusal(lane: str, stop) -> str:
    from core.goal_suppressions import stopped_at_text
    why = f" (“{stop.reason}”)" if stop.reason else ""
    return (f"the owner stopped `{lane}` at {stopped_at_text(stop)}{why}; only the "
            f"owner can re-arm it: /job allow {lane}. Nothing was scheduled.")


def _pinned(payload: Optional[Dict[str, Any]]) -> List[str]:
    """The pinned skill ids, cleaned by the one rail cleaner (``rigs.pinned_skills``)."""
    from core.config_policy.rigs import pinned_skills
    return pinned_skills(payload if isinstance(payload, dict) else {})


def missing_pinned_skills(payload: Optional[Dict[str, Any]], *, user_id: Optional[str]) -> List[str]:
    """The pinned ids that no scope serves for *user_id* (``core.skills.scopes.resolve``,
    the ONE precedence order the run loads by). Raises when the lookup itself fails."""
    from core.skills.scopes import external_index, resolve
    pins = _pinned(payload)
    if not pins:
        return []
    ext = external_index()
    return [sid for sid in pins if resolve(sid, user_id or None, external=ext) is None]


def check_pinned_skills(payload: Optional[Dict[str, Any]], *, user_id: Optional[str]) -> None:
    """Refuse a job or goal write that pins an unknown skill id (and one whose
    skills cannot be checked: an unreadable store is not an empty one)."""
    try:
        missing = missing_pinned_skills(payload, user_id=user_id)
    except Exception as exc:
        raise UnknownPinnedSkill(f"could not check the pinned skills ({type(exc).__name__}); "
                                 f"nothing was saved") from exc
    if missing:
        raise UnknownPinnedSkill(
            f"no skill named {', '.join(f'`{m}`' for m in missing)} — pin an existing skill id "
            f"(see the skill index); nothing was saved")


def missing_skills_notice(payload: Optional[Dict[str, Any]], *, user_id: Optional[str]) -> str:
    """The line a run's task starts with when a pinned skill is gone ("" when
    every pin resolves). A failed lookup says so too. Never raises."""
    try:
        missing = missing_pinned_skills(payload, user_id=user_id)
    except Exception as exc:
        return (f"[IMPORTANT: the skills pinned to this run could not be checked "
                f"({type(exc).__name__}). Start your report with one line that says so.]")
    if not missing:
        return ""
    return (f"[IMPORTANT: this run pins skill(s) that were not found and are skipped: "
            f"{', '.join(missing)}. Start your report with one line that names them, so the "
            f"owner can fix the pin.]")


def gated_lanes(payload: Optional[Dict[str, Any]]) -> List[str]:
    """Every lane a stop can hold against this job: each pinned skill, then the
    rig. :func:`job_lane` names the job; the gate reads all of these, so putting
    a stopped skill second does not get it past the stop."""
    p = payload if isinstance(payload, dict) else {}
    skills = p.get("skills")
    out: List[str] = []
    for v in (list(skills) if isinstance(skills, (list, tuple)) else []) + [p.get("rig")]:
        lane = str(v or "").strip().lower()
        if lane and lane not in out:
            out.append(lane)
    return out


#: :func:`lane_gate`'s stop for a lane whose store an owner create could not read.
UNREADABLE = object()


def lane_gate(store, *, user_id: str, lanes: List[str], owner_seat: bool) -> List[tuple]:
    """``[(lane, stop), …]`` — the owner's stops in force among *lanes*.

    Raises :class:`LaneStopped` for a create that is not the owner's while a
    stop is in force — and when the stop store cannot be read (an unreadable
    store is not an empty one). The owner's create gets the stops back so the
    caller lifts them after the job is stored (:data:`UNREADABLE` when the store
    could not be read — the caller says so)."""
    from core.goal_suppressions import lane_stop
    found: List[tuple] = []
    for lane in [str(x) for x in lanes if x]:
        try:
            stop = lane_stop(_stops_db(store), user_id=user_id, lane=lane)
        except Exception as exc:
            if not owner_seat:
                raise LaneStopped(f"could not read the owner's stops for `{lane}` "
                                  f"({type(exc).__name__}); nothing was scheduled") from exc
            logger.warning("lane stop store unreadable (owner create goes on)", exc_info=True)
            found.append((lane, UNREADABLE))
            continue
        if stop is None:
            continue
        if not owner_seat:
            raise LaneStopped(_refusal(lane, stop))
        found.append((lane, stop))
    return found


def lift_after_owner_create(store, *, user_id: str, lane: str, stop) -> str:
    """Remove the stop the owner's new job re-arms; the sentence that says so."""
    from core.goal_suppressions import SCOPE_CRON_LANE, allow, stopped_at_text
    if stop is UNREADABLE:
        return (f"I could not read your stops for `{lane}`, so I cannot say whether "
                f"this job re-arms a stopped lane — check /jobs; /job allow {lane} lifts one.")
    try:
        allow(_stops_db(store), user_id=user_id, scope=SCOPE_CRON_LANE, value=lane)
    except Exception as exc:
        return (f"Your stop of `{lane}` could NOT be lifted ({type(exc).__name__}); "
                f"lift it with /job allow {lane}.")
    return (f"Your stop of `{lane}` (set {stopped_at_text(stop)}) is lifted: "
            f"this job re-arms the lane.")


def scope_pause_note(payload: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
    """The ``payload.paused`` note for a job created while its run kind is
    paused (``/pause cron`` or ``/pause``), else ``None``."""
    from core.autonomy_control import allows
    kind = "digest" if payload.get("digest") else "cron_run"
    decision = allows(kind)
    if decision.allowed:
        return None
    scopes = [s for s in (getattr(decision.state, "scopes", ()) or ()) if s in ("all", "cron")]
    return {"by": PAUSE_SCOPE_BY, "via": "pause", "at": now.isoformat(),
            "scopes": scopes or ["all"]}


def scope_pause_text(note: Dict[str, Any]) -> str:
    resume = "/resume" if "all" in (note.get("scopes") or []) else "/resume cron"
    return f"Created PAUSED: scheduled jobs are paused. It runs after {resume}."


def schedule_notes(job) -> List[str]:
    """What ``CronService.schedule`` did beyond storing *job* (a lifted stop,
    a created-paused job) — every seat appends these to its reply."""
    return list(getattr(job, "schedule_notes", None) or [])


# --- the owner's stop verbs ---------------------------------------------------

def stop_job(svc, job, *, user_id: str, via: str, forever: bool = False,
             reason: str = "") -> str:
    """``/job stop <id> [--forever]`` on every owner seat. Once = cancel the job.
    Forever = also stop its lane, so no job in it is created again except by the
    owner. Never raises; a failed write is named in the sentence."""
    short = f"`{job.id[:8]}`"
    stopped = False
    if job.status != "cancelled":
        stopped = bool(svc.cancel(job.id, user_id=user_id, via=via))
        if not stopped:
            return f"Could not stop {short} — see /jobs."
    head = (f"✅ Stopped job {short}. It will not run again." if stopped
            else f"Job {short} is already stopped.")
    if not forever:
        lane = job_lane(job.payload)
        tail = (f" Stop the `{lane}` lane for good: /job stop {job.id[:8]} --forever"
                if lane else "")
        return head + tail
    lane = job_lane(job.payload)
    if not lane:
        return (head + " It has no lane (no pinned skill and no rig), so there is no "
                "lane to stop for good.")
    from core.goal_suppressions import SCOPE_CRON_LANE, suppress
    note = (reason or "").strip() or f"stopped with job {job.id[:8]}"
    try:
        suppress(_stops_db(svc.store), user_id=user_id, scope=SCOPE_CRON_LANE,
                 value=lane, label=lane, reason=note)
    except Exception as exc:
        return (f"{head} But I could NOT stop the `{lane}` lane for good "
                f"({type(exc).__name__}: {str(exc)[:80]}) — a new job in it may be created.")
    return (f"{head} The `{lane}` lane is stopped: no new job in it is created — "
            f"not by a job or by me — until you re-arm it: /job allow {lane}")


def allow_lane(store, *, user_id: str, lane: str) -> str:
    """``/job allow <lane>`` — remove the owner's stop of *lane*."""
    from core.goal_suppressions import SCOPE_CRON_LANE, allow
    want = str(lane or "").strip().lower()
    if not want:
        return "Usage: /job allow <lane> — /jobs lists the lanes you stopped."
    try:
        removed = allow(_stops_db(store), user_id=user_id, scope=SCOPE_CRON_LANE, value=want)
    except Exception as exc:
        return f"Could not read your stops ({type(exc).__name__}) — nothing changed."
    if not removed:
        return f"No stop on `{want}` — /jobs lists the lanes you stopped."
    return f"✅ Re-armed `{want}`: a job in this lane may be created again."


def stopped_lines(store, *, user_id: str) -> List[str]:
    """The "Stopped by you" section of the owner job list (empty when none)."""
    from core.goal_suppressions import SCOPE_CRON_LANE, active, stopped_at_text
    try:
        stops = active(_stops_db(store), user_id=user_id, scope=SCOPE_CRON_LANE)
    except Exception as exc:
        return [f"Stopped by you — unavailable ({type(exc).__name__}); I cannot tell "
                f"which lanes you stopped."]
    if not stops:
        return []
    out = [f"Stopped by you ({len(stops)}) — no job in these lanes is created except by you:"]
    for s in stops:
        why = f" — “{s.reason}”" if s.reason else ""
        out.append(f"• `{s.key}` since {stopped_at_text(s)}{why} · /job allow {s.key}")
    return out


def lane_line(store, *, user_id: str, payload: Optional[Dict[str, Any]]) -> str:
    """``/job show``: the job's lane and, when the owner stopped it, since when
    ("" for a job with no lane). Read from the stop table — the row keeps no copy."""
    lane = job_lane(payload)
    if not lane:
        return ""
    from core.goal_suppressions import lane_stop, stopped_at_text
    try:
        stop = lane_stop(_stops_db(store), user_id=user_id, lane=lane)
    except Exception as exc:
        return (f"lane `{lane}` — could not read your stops ({type(exc).__name__}); "
                f"I cannot tell whether it is stopped.")
    if stop is None:
        return f"lane `{lane}`"
    return (f"lane `{lane}` — stopped by you since {stopped_at_text(stop)}; "
            f"re-arm: /job allow {lane}")


__all__ = [
    "LaneStopped", "OWNER_TURN_VIA", "PAUSE_SCOPE_BY", "UNREADABLE", "UnknownPinnedSkill",
    "allow_lane", "check_pinned_skills", "gated_lanes", "job_lane", "lane_gate", "lane_line",
    "lift_after_owner_create", "missing_pinned_skills", "missing_skills_notice",
    "schedule_notes", "scope_pause_note", "scope_pause_text", "stop_job", "stopped_lines",
]
