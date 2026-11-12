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
from typing import Any, Dict, Optional, Tuple


def counts_from_sections(sections: Dict[str, Any], data_dir: str
                         ) -> Tuple[Optional[int], Optional[int], Dict[str, str]]:
    """``(open goals, enabled cron jobs, {store: reason})`` from the snapshot's
    ``work`` and ``loops`` sections (116 W7.4) — the numbers `/status` shows.

    Open goals = ``work.data["open_goals"]`` (the live statuses of
    ``core.goal_vocab.LIVE_STATUS_ORDER``); cron jobs = ``loops.data
    ["cron_enabled"]`` (a cancelled or disabled row is not a job). A store that
    does not exist yet holds nothing (0, a fresh install); a store that exists
    and could not be read is ``None`` with its reason, never 0."""
    from core.runtime_paths import cron_db_path, goals_db_path
    unread: Dict[str, str] = {}
    goals: Optional[int] = 0
    cron: Optional[int] = 0
    if os.path.exists(goals_db_path(data_dir)):
        work = sections.get("work")
        open_goals = work.data.get("open_goals") if work is not None and work.available else None
        if open_goals is None:
            goals = None
            unread["goals"] = (getattr(work, "reason", "") or "no open-goal list") \
                if work is not None else "no work section"
        else:
            goals = len(open_goals)
    if os.path.exists(cron_db_path(data_dir)):
        loops = sections.get("loops")
        enabled = loops.data.get("cron_enabled") if loops is not None and loops.available else None
        if enabled is None:
            cron = None
            unread["cron"] = ((loops.data.get("cron_reason") or loops.reason or "no cron count")
                              if loops is not None else "no loops section")
        else:
            cron = int(enabled)
    return goals, cron, unread


def read_autonomy_snapshot(user_id: str, data_dir: str = "data",
                           sections: Optional[Dict[str, Any]] = None) -> Optional[dict]:
    """Open goals + enabled cron jobs for the tenant, read from the snapshot's
    ``work``/``loops`` sections (``core.status_snapshot.build_operations_sections``:
    the cheap builder the agent's per-turn ``<operations>`` block uses, no 24h
    telemetry read). *sections*: a caller that already holds a snapshot passes
    its sections. None = hide the line. ``goals``/``cron`` None = unreadable;
    ``unread`` names why (present only when something could not be read)."""
    review = False
    try:
        try:
            from core.config_policy import AutonomyConfig

            review = bool(AutonomyConfig.background_review_enabled())
        except Exception:
            review = None

        if sections is None:
            from core.status_snapshot import build_operations_sections
            sections = build_operations_sections(user_id, data_dir=data_dir)
        goals, cron, unread = counts_from_sections(sections, data_dir)
    except Exception:
        return None

    if goals == 0 and cron == 0 and review is False:
        return None
    out = {"goals": goals, "cron": cron, "review": review}
    if unread:
        out["unread"] = unread
    return out


def poll_status_bar(user_id: Optional[str]) -> Optional[dict]:
    """The status bar's autonomy line — over the deployed home and owner (CLI13).

    It read ``config.data_dir`` (the shell's home) and the session tenant, so on
    the box the bar showed the shell's empty board while ``/autonomy`` beside it
    read the daemon's. Both now resolve through ``cli._admin_home`` — read-only,
    so root is never refused. A seam that refuses (an unreadable deployed home)
    degrades to the old local resolution rather than tear down the bar.
    """
    try:
        from cli._admin_home import admin_data_dir, admin_owner_tenant
        tenant = admin_owner_tenant(user_id)
        data_dir = admin_data_dir(write=False)
    except Exception:
        from core.runtime_paths import data_dir_or_home
        tenant, data_dir = (user_id or "local"), data_dir_or_home(None)
    return read_autonomy_snapshot(tenant, data_dir)
