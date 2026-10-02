"""A cron rail that reports the same owner-only remedy run after run asks ONCE.

A rail that is blocked on something only the owner can do (log in to an
account, pin a token) says so in its result and then, by design, stays quiet
— its task text forbids pinging the owner every run. Nothing turned that
repeated line into a question. Live: X OUTREACH logged "Phase 1 BLOCKED (owner
remedy: `polyrob x-account capture-session`)" on 11 consecutive daily runs
(2026-09-18 → 09-28) and made 0 touches; the owner was never asked.

This module watches each finished cron run's result text. A result that says
``BLOCKED`` (or "owner remedy") AND names an owner command (``polyrob …``) is a
block on that remedy. When the SAME remedy repeats on ``STREAK_THRESHOLD``
consecutive runs of one job, it raises ONE owner ask on that rail (A: I will
run it · B: retire this job). The answer rides the rail's next run
(``agents/task/goals/rail_answers.py``). A clean run or a different remedy
resets the count, and a streak asks at most once.

Deterministic on purpose: the prompt already said "ask the owner if blocked"
and the model followed the task text's "no ping" instead.

Fail-open: any error answers "no ask" and never touches the run's outcome.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Optional

from core.sqlite_util import execute_retry

logger = logging.getLogger(__name__)

#: consecutive identical blocks before the owner is asked
STREAK_THRESHOLD = 3

_BLOCK_RE = re.compile(r"\bBLOCKED\b|\bowner[- ]remedy\b", re.IGNORECASE)
_UPPER_BLOCK_RE = re.compile(r"\bBLOCKED\b")
_REMEDY_RE = re.compile(r"\bowner[- ]remedy\b", re.IGNORECASE)


def remedy_in(text: Optional[str]) -> Optional[str]:
    """The owner command a blocked run names, or ``None``.

    Needs BOTH a block marker (an upper-case ``BLOCKED`` or the words "owner
    remedy") and a named ``polyrob …`` call: a CLI call alone is a report, and
    a block with no owner command has nothing to ask about.
    """
    if not text:
        return None
    if not (_UPPER_BLOCK_RE.search(text) or _REMEDY_RE.search(text)):
        return None
    from core.owner_remedy import cli_calls_named
    calls = cli_calls_named(text)
    return calls[0] if calls else None


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
    return (f"The scheduled job \"{name}\" ({job.id}) has been blocked on its last "
            f"{count} runs until you run `{remedy}`. A) I will run `{remedy}` "
            f"or B) retire this job?")


def observe(job: Any, final_text: Optional[str], *, data_dir: str,
            board: Any = None) -> Optional[Any]:
    """Record this run's block (or its absence). Returns the NEW ask when this
    run completes a streak, else ``None``. Never raises."""
    try:
        from core.runtime_paths import cron_db_path, goals_db_path
        store = _Store(cron_db_path(data_dir))
        remedy = remedy_in(final_text)
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
            why=(f"Each run's result names `{remedy}` as the owner-only fix and the job "
                 f"stays blocked until it is done. Choosing B stops the runs."),
            extra_payload={"origin": "cron_remedy_streak", "rail_id": f"cron:{job.id}",
                           "rail_kind": "cron", "remedy": remedy,
                           "options": ask_options(question)},
            force=True)
        store.put(job.id, job.user_id, remedy, count, str(ask.id))
        logger.info("cron job %s: same owner remedy on %d runs (%s) -> ask %s",
                    job.id, count, remedy, ask.id)
        return ask
    except Exception:
        logger.debug("remedy streak skipped for %s (fail-open)",
                     getattr(job, "id", "?"), exc_info=True)
        return None


async def observe_and_notify(job: Any, final_text: Optional[str], *, data_dir: str,
                             container: Any = None) -> None:
    """``observe`` plus ONE owner notice for a newly raised ask. Fail-open."""
    ask = observe(job, final_text, data_dir=data_dir)
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


__all__ = ["STREAK_THRESHOLD", "remedy_in", "observe", "observe_and_notify"]
