"""An open owner question holds its own cron job (116 W2.3).

A job that asked the owner something (``owner_ask`` from its run: the ask carries
``payload.rail_id = "cron:<job id>"``) used to keep running on schedule with
nothing to do until the answer came: prod 2026-11-11, one job ran 24 times in
24 h against its own open "expand the list or stop?" question.

Two ways an ask holds its job (both write ``payload.holds_rail`` on the ask; the
read is ``core.cron_expect.holding_ask`` / ``job_held_by``, one home for the
scheduler and status):

- the run asks with ``owner_ask(holds_this_job=true)`` — ``holds_rail_via="owner_ask"``;
- automatic (owner decision D2): :data:`AUTO_HOLD_AFTER` finished runs in a row
  labelled ``idle`` or ``unverified`` (``core.cron_expect.run_effect``) while the
  job's own ask stayed open — ``holds_rail_via="auto"``. The count rides on the ask
  (``payload.idle_runs_open``); any other finished run resets it.

The hold ends when the ask is decided (an approval re-arms the job at once,
``rail_answers.rearm_cron_rail``) or expires. A held due slot is a ``deferred``
``cron_run`` row with reason ``owner_ask:<id>`` and no session, $0, no failure
count (``cron/scheduler.py``). Fail-open everywhere: an error never holds a job.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from core.cron_expect import HOLD_VIA_AUTO, HOLDS_RAIL_KEY, IDLE, UNVERIFIED

logger = logging.getLogger(__name__)

#: D2 (owner 2026-11-12): finished idle/unverified runs in a row, with the job's own
#: ask open, before the job is held until the answer.
AUTO_HOLD_AFTER = 3
#: The count of such runs since the ask opened (on the ask's payload).
IDLE_RUNS_KEY = "idle_runs_open"
_COUNTED = frozenset({IDLE, UNVERIFIED})
_OPEN_ASK = "kind='ask' AND status='open'"


def _board(data_dir: str) -> Optional[Any]:
    from core.runtime_paths import goals_db_path
    path = goals_db_path(data_dir)
    if not os.path.exists(path):
        return None
    from agents.task.goals.board import GoalBoard
    return GoalBoard(path)


def observe_run(job: Any, effect: Optional[str], *, data_dir: str,
                board: Any = None) -> Optional[Any]:
    """Count this finished run against the job's open asks. *effect* is the run's
    ``run_effect`` label (``None`` for a run that did not end ``done``: it resets
    the count). Returns the ask that NOW holds the job (newly, automatically),
    else ``None``. Never raises."""
    try:
        board = board if board is not None else _board(data_dir)
        if board is None:
            return None
        from agents.task.goals.rail_answers import mark_holds_rail, open_rail_asks
        asks = open_rail_asks(board, job.user_id, f"cron:{job.id}")
        if not asks:
            return None
        from agents.task.goals.payload_keys import patch, update_key_cas
        newly = None
        for ask in asks:
            p = dict(ask.payload or {})
            if effect not in _COUNTED:
                if p.get(IDLE_RUNS_KEY):
                    patch(board.db_path, ask.id, remove=[IDLE_RUNS_KEY], where_extra=_OPEN_ASK)
                continue
            seen = {}

            def _bump(cur, _seen=seen):
                _seen["n"] = int(cur or 0) + 1
                return _seen["n"]
            if not update_key_cas(board.db_path, ask.id, IDLE_RUNS_KEY, _bump,
                                  where_extra=_OPEN_ASK):
                continue
            if (seen.get("n", 0) >= AUTO_HOLD_AFTER and p.get(HOLDS_RAIL_KEY) is not True
                    and newly is None and mark_holds_rail(board.db_path, ask.id,
                                                          via=HOLD_VIA_AUTO)):
                newly = ask
                logger.info("cron job %s: %d idle runs in a row with ask %s open -> held "
                            "until the answer", job.id, seen["n"], ask.id)
        return newly
    except Exception:
        logger.debug("rail hold: run not counted for %s (fail-open)",
                     getattr(job, "id", "?"), exc_info=True)
        return None


async def observe_and_notify(job: Any, effect: Optional[str], *, data_dir: str,
                             container: Any = None) -> None:
    """:func:`observe_run` plus ONE owner line when the job is newly held. Fail-open."""
    ask = observe_run(job, effect, data_dir=data_dir)
    if ask is None:
        return
    try:
        from core.surfaces.user_delivery import deliver_user_message
        name = " ".join(str(getattr(job, "task", "") or "").split())[:60] or str(job.id)
        text = (f"⏸ The scheduled job \"{name}\" ({job.id}) ran {AUTO_HOLD_AFTER} times "
                f"with nothing to do while your answer to ask {str(ask.id)[:12]} was open. "
                f"It is held until you answer: {ask.title[:200]}"
                f"\nAnswer here in chat, or /fulfill {ask.id} <answer>.")
        await deliver_user_message(container, job.user_id, text,
                                   source="cron_rail_hold", ask_id=str(ask.id))
    except Exception:
        logger.debug("rail hold notice skipped for %s", getattr(job, "id", "?"), exc_info=True)


def held_by(job: Any, *, cron_db_path: Optional[str]) -> Optional[dict]:
    """The open question that holds *job* now (``core.cron_expect.holding_ask``
    shape), or ``None``. Fail-open: an unreadable board answers ``None`` (the job runs)."""
    try:
        from core.cron_expect import job_held_by
        from core.runtime_paths import goals_db_path
        if not cron_db_path:
            return None
        home = os.path.dirname(os.path.abspath(cron_db_path))
        return job_held_by(goals_db_path(home), str(getattr(job, "user_id", "") or ""),
                           str(job.id))
    except Exception:
        logger.warning("rail hold: open asks of job %s not read — the job runs",
                       getattr(job, "id", "?"), exc_info=True)
        return None


__all__ = ["AUTO_HOLD_AFTER", "IDLE_RUNS_KEY", "held_by", "observe_and_notify",
           "observe_run"]
