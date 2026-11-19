"""The agent's pause and resume rule for one cron job, and the job's last runs (120 W2).

Beside ``tools/cronjob_tools.py`` (which holds the actions) so that file stays under
its size row. The pause and the resume themselves are ``CronService.pause`` /
``.resume`` — the one store path every owner seat uses; nothing here writes.

The resume rule reads the pause note the row already holds (``payload.paused =
{by, via, at}``; the scheduler writes ``by="failure_streak"``):

=====================================  ==========================================
Who paused                             Who may resume through ``cronjob_resume``
=====================================  ==========================================
the agent (``via="agent"``)            the agent, on a turn that may resume at all
the agent on the owner's word          a genuine owner turn
(``via="owner_turn"``)
the failure limit                      a genuine owner turn — never an autonomous
(``by="failure_streak"``)              run: the limit exists to stop a loop
an owner seat, a pause scope, or a     nobody here: the owner (``/job resume``).
note that is missing or unreadable     Fail closed.
=====================================  ==========================================
"""
import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

#: ``cronjob_show(runs=N)`` reads at most this many outcomes.
MAX_RUNS = 20
#: The ``cron_run`` rows read to find one job's outcomes (newest first).
_SCAN_ROWS = 2000


def pause_note(job) -> dict:
    """The row's pause note, or ``{}`` when it is missing or not a mapping."""
    note = (getattr(job, "payload", None) or {}).get("paused")
    return note if isinstance(note, dict) else {}


def resume_rule(job, *, owner_turn: bool) -> Tuple[bool, str]:
    """``(may_resume, words)`` for the agent. *words* names the path when it may not."""
    from core.cron_lanes import OWNER_TURN_VIA
    from core.cron_outcome import FAILURE_STREAK_BY
    note, short = pause_note(job), str(getattr(job, "id", ""))[:8]
    by, via = note.get("by"), note.get("via")
    owner_path = f"the owner resumes it (/job resume {short})"
    if by == FAILURE_STREAK_BY:
        if owner_turn:
            return True, ""
        return False, (f"the failure limit paused it, so only an owner turn lifts that: "
                       f"{owner_path}, or the owner asks you in chat and you call "
                       f"cronjob_resume then")
    if via == "agent":
        return True, ""
    if via == OWNER_TURN_VIA:
        if owner_turn:
            return True, ""
        return False, (f"you paused it on the owner's word, so it resumes on the owner's "
                       f"word: {owner_path}, or the owner asks you in chat")
    return False, f"the owner paused it (or the pause note cannot be read), so {owner_path}"


def cancel_refusal(job, *, owner_turn: bool) -> str:
    """The refusal for a cancel of a paused job the caller may not resume, else "".
    A cancel and a new schedule of the same task would lift an owner pause."""
    if owner_turn or job is None or getattr(job, "status", None) != "paused":
        return ""
    ok, words = resume_rule(job, owner_turn=False)
    if ok:
        return ""
    return (f"cronjob_cancel refused: job {str(job.id)[:8]} is paused and you may not "
            f"lift that pause, so you may not cancel it: {words}")


def paused_path(job, *, owner_turn: bool) -> str:
    """The sentence ``cronjob_run_now`` gives for a paused job: the right path to resume."""
    ok, words = resume_rule(job, owner_turn=owner_turn)
    if ok:
        return f"resume it first with cronjob_resume(job_id='{job.id}')"
    return words


def own_run_job(execution_context) -> Optional[str]:
    """The cron job this run (or its parent run) runs in, else None."""
    from core.autonomy_marker import cron_job_for_session
    for attr in ("session_id", "parent_session_id"):
        job_id = cron_job_for_session(getattr(execution_context, attr, None))
        if job_id:
            return job_id
    return None


def _stamp(ts) -> str:
    import datetime as _dt
    try:
        return _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc).strftime("%m-%d %H:%MZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _run_line(row: dict) -> str:
    a = row.get("attrs") or {}
    bits = [_stamp(row.get("ts")), str(a.get("outcome") or "?")]
    if a.get("reason"):
        bits.append(f"({' '.join(str(a['reason']).split())[:120]})")
    if a.get("verified") is not None:
        bits.append(f"verified={str(bool(a['verified'])).lower()}")
    if a.get("cost_usd") is not None:
        try:
            bits.append(f"${float(a['cost_usd']):.4f}")
        except (TypeError, ValueError):
            pass
    return "  - " + " ".join(bits)


def job_run_lines(job, n: int, *, data_dir: Optional[str] = None) -> List[str]:
    """The last *n* outcomes of *job* from its terminal ``cron_run`` rows, newest first.
    A log that cannot be read says so; it never reads as "no runs"."""
    n = max(0, min(int(n or 0), MAX_RUNS))
    if not n:
        return []
    head = f"last {n} runs (the run log):"
    try:
        from core.event_kinds import CRON_RUN
        from core.event_log import open_event_log
        log = open_event_log(data_dir)
        if log is None:
            return [f"{head} could not be read (no run log in this data home)"]
        rows = log.query(kind=CRON_RUN, user_id=str(job.user_id or ""), limit=_SCAN_ROWS,
                         strict=True)
    except Exception as exc:
        logger.warning("cronjob_show runs: the run log could not be read", exc_info=True)
        return [f"{head} could not be read ({type(exc).__name__})"]
    mine = [r for r in rows if (r.get("attrs") or {}).get("job_id") == job.id
            and (r.get("attrs") or {}).get("outcome") != "started"][:n]
    if not mine:
        return [f"{head} none recorded"]
    return [head] + [_run_line(r) for r in mine]


__all__ = ["MAX_RUNS", "cancel_refusal", "job_run_lines", "own_run_job", "pause_note", "paused_path",
           "resume_rule"]
