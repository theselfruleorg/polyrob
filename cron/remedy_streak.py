"""A cron rail that declares the same BLOCKED need run after run asks ONCE.

A rail that is blocked on something only the owner can do declares it with the
run-outcome protocol (``OUTCOME: BLOCKED — <need>``, parsed once into
``RunOutcome.blocked`` / ``blocked_need``) and then stays quiet. Live: X
OUTREACH reported the same block on 11 consecutive daily runs (2026-09-18 →
09-28) and the owner was never asked.

When the SAME declared need repeats on ``STREAK_THRESHOLD`` consecutive runs of
one job, this raises ONE owner ask on that rail (A: I will do it · B: retire
this job). The answer rides the rail's next run
(``agents/task/goals/rail_answers.py``). A run that is not blocked, or a
different need, resets the count, and a streak asks at most once. The model's
free text is never parsed here: only the run outcome's ``blocked_need`` field.

Fail-open: any error answers "no ask" and never touches the run's outcome.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from core.sqlite_util import execute_retry

logger = logging.getLogger(__name__)

#: consecutive identical blocks before the owner is asked
STREAK_THRESHOLD = 3


def _key(need: Optional[str]) -> Optional[str]:
    """The streak key of a declared need, or None (not blocked / no need)."""
    key = " ".join(str(need or "").split())[:300]
    return key or None


class _Store:
    """Per-job streaks, colocated in cron.db beside ``wake_gate``."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        execute_retry(
            db_path,
            """CREATE TABLE IF NOT EXISTS remedy_streak (
                   job_id TEXT PRIMARY KEY,
                   user_id TEXT,
                   remedy TEXT NOT NULL,
                   count INTEGER NOT NULL,
                   ask_id TEXT,
                   updated_at REAL NOT NULL
               )""",
        )

    def get(self, job_id: str) -> Optional[dict]:
        row = execute_retry(
            self.db_path,
            "SELECT remedy, count, ask_id FROM remedy_streak WHERE job_id=?",
            (job_id,), fetch="one")
        return dict(row) if row else None

    def put(self, job_id: str, user_id: str, remedy: str, count: int,
            ask_id: Optional[str]) -> None:
        execute_retry(
            self.db_path,
            """INSERT INTO remedy_streak (job_id, user_id, remedy, count, ask_id, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                   user_id=excluded.user_id, remedy=excluded.remedy,
                   count=excluded.count, ask_id=excluded.ask_id,
                   updated_at=excluded.updated_at""",
            (job_id, user_id, remedy, count, ask_id, time.time()))

    def clear(self, job_id: str) -> None:
        execute_retry(self.db_path, "DELETE FROM remedy_streak WHERE job_id=?", (job_id,))


def _question(job: Any, remedy: str, count: int) -> str:
    name = " ".join(str(getattr(job, "task", "") or "").split())[:60] or str(job.id)
    return (f"The scheduled job \"{name}\" ({job.id}) declared the same block on its "
            f"last {count} runs: {remedy}. A) I will do it or B) retire this job?")


def observe(job: Any, blocked_need: Optional[str], *, data_dir: str,
            board: Any = None) -> Optional[Any]:
    """Record this run's declared block (``RunOutcome.blocked_need``; None when
    the run was not blocked). Returns the NEW ask when this run completes a
    streak, else ``None``. Never raises."""
    try:
        from core.runtime_paths import cron_db_path, goals_db_path
        store = _Store(cron_db_path(data_dir))
        remedy = _key(blocked_need)
        prev = store.get(job.id)
        if remedy is None:
            if prev:
                store.clear(job.id)
            return None
        same = bool(prev) and prev["remedy"] == remedy
        count = (int(prev["count"]) + 1) if same else 1
        ask_id = prev.get("ask_id") if same else None
        if count < STREAK_THRESHOLD or ask_id:
            store.put(job.id, job.user_id, remedy, count, ask_id)
            return None
        if board is None:
            from agents.task.goals.board import GoalBoard
            board = GoalBoard(goals_db_path(data_dir))
        from tools.controller.owner_ask_action import ask_options
        question = _question(job, remedy, count)
        ask = board.create_ask(
            user_id=job.user_id, what=question,
            why=("Each run declared this block and the job stays blocked until it "
                 "is resolved. Choosing B stops the runs."),
            extra_payload={"origin": "cron_remedy_streak", "rail_id": f"cron:{job.id}",
                           "rail_kind": "cron", "remedy": remedy,
                           "options": ask_options(question)},
            force=True)
        store.put(job.id, job.user_id, remedy, count, str(ask.id))
        logger.info("cron job %s: same declared block on %d runs (%s) -> ask %s",
                    job.id, count, remedy, ask.id)
        return ask
    except Exception:
        logger.debug("remedy streak skipped for %s (fail-open)",
                     getattr(job, "id", "?"), exc_info=True)
        return None


async def observe_and_notify(job: Any, blocked_need: Optional[str], *, data_dir: str,
                             container: Any = None) -> None:
    """``observe`` plus ONE owner notice for a newly raised ask. Fail-open."""
    ask = observe(job, blocked_need, data_dir=data_dir)
    if ask is None:
        return
    try:
        from core.surfaces.tappable import ask_option_token
        from core.surfaces.user_delivery import deliver_user_message
        options = (ask.payload or {}).get("options") or {}
        taps = ""
        if options:
            taps = "\nTap an answer: " + " · ".join(
                f"{letter}) {ask_option_token(ask.id, letter)}" for letter in options)
        text = (f"❓ I need your decision (ask {str(ask.id)[:12]}): {ask.title}"
                + taps + f"\nAnswer here in chat (e.g. \"A\"), or /fulfill {ask.id} <answer>.")
        await deliver_user_message(container, job.user_id, text,
                                   source="cron_remedy_streak", ask_id=str(ask.id))
    except Exception:
        logger.debug("remedy streak notice skipped for %s", getattr(job, "id", "?"),
                     exc_info=True)


__all__ = ["STREAK_THRESHOLD", "observe", "observe_and_notify"]
