"""autonomy_poll.py — slow background refresh of ``state.autonomy_snapshot``.

The persistent app's second status line (``statusbar.autonomy_line``) reads a
CACHED snapshot on every repaint; this module is the missing writer. It is
driven by a slow asyncio task on the event loop (NEVER the repaint path),
fail-open on every leg, and — critically — never CREATES a store: a missing
``cron.db``/``goals.db`` is skipped (opening a store would mkdir/create as a
side effect; the path-concerns landmine).
"""

from __future__ import annotations

import os
from typing import Optional


def read_autonomy_snapshot(user_id: str, data_dir: str = "data") -> Optional[dict]:
    """Count open goals + scheduled cron jobs for the tenant. None = hide the line."""
    goals = 0
    cron = 0
    review = False
    try:
        try:
            from agents.task.constants import AutonomyConfig

            review = bool(AutonomyConfig.background_review_enabled())
        except Exception:
            review = False

        cron_db = os.path.join(data_dir, "cron.db")
        if os.path.exists(cron_db):
            try:
                from cron.jobs import CronJobStore
                from cron.service import CronService

                cron = len(CronService(CronJobStore(cron_db)).list_jobs(user_id=user_id))
            except Exception:
                cron = 0

        goals_db = os.path.join(data_dir, "goals.db")
        if os.path.exists(goals_db):
            try:
                from agents.task.goals.board import GoalBoard

                all_goals = GoalBoard(goals_db).list(user_id=user_id)
                goals = len(
                    [g for g in all_goals if getattr(g, "status", "") not in ("done", "cancelled")]
                )
            except Exception:
                goals = 0
    except Exception:
        return None

    if not (goals or cron or review):
        return None
    return {"goals": goals, "cron": cron, "review": review}
