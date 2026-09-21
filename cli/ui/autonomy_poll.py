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
            from core.config_policy import AutonomyConfig

            review = bool(AutonomyConfig.background_review_enabled())
        except Exception:
            review = None

        from core.runtime_paths import cron_db_path, goals_db_path

        cron_db = cron_db_path(data_dir)
        if os.path.exists(cron_db):
            try:
                from cron.jobs import CronJobStore
                from cron.service import CronService

                cron = len(CronService(CronJobStore(cron_db)).list_jobs(user_id=user_id))
            except Exception:
                cron = None

        goals_db = goals_db_path(data_dir)
        if os.path.exists(goals_db):
            try:
                from agents.task.goals.board import GoalBoard

                # C16: ``status_counts`` counts EVERY row of this tenant.
                # ``board.list`` is the dispatcher's ``priority DESC … LIMIT``
                # order, so the old count here was "open goals among the
                # highest-priority 100" — a number that silently stopped
                # growing on a busy board and read as a plateau, not a window.
                counts = GoalBoard(goals_db).status_counts(user_id=user_id)
                goals = sum(n for status, n in counts.items()
                            if status not in ("done", "cancelled"))
            except Exception:
                goals = None
    except Exception:
        return None

    if goals == 0 and cron == 0 and review is False:
        return None
    return {"goals": goals, "cron": cron, "review": review}
