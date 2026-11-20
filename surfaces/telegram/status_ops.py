"""Owner read/control replies for the Telegram admin verbs: /status, /mode,
/recap, /goals, /missed, /cwd, /pause|/halt|/resume.

Moved out of ``surfaces/telegram/harness.py`` (W9, 2026-10-10 structural
review) unchanged; the harness re-exports every name, so the REPL seats that
import ``surfaces.telegram.harness._mode_reply`` (and friends) keep reading the
SAME sentences. Imports nothing from the harness (no import cycle).
"""
from core.runtime_paths import goals_db_path
import asyncio
import logging
import os
from typing import Any, Optional

logger = logging.getLogger("surfaces.telegram.harness")


async def _status_reply(task_agent: Any, user_id: str, session_id: Optional[str],
                        data_dir: str, board: Optional[Any] = None, *,
                        full: bool = False) -> str:
    """`/status` — rendered from the ONE status snapshot (core/status_snapshot.py).

    2026-08-28: this used to assemble its own view (ready/running counts + the
    posture card + two money lines) and drop any section whose read failed, so
    it rendered "Goals: 0 open, 0 running · kill switch: clear" over a
    credit-dead provider, 91 suppressed owner notices, two open asks and a
    blocked goal. Now: health first (never empty), every section typed, an
    unreadable source renders ``unavailable (<reason>)`` instead of vanishing.

    ``board`` is accepted for call-site compatibility; the snapshot reads the
    same ``goals.db`` read-only. Runs in a worker thread so the network balance
    probe (a display surface opts in) never blocks the polling loop.
    """
    from core.status_snapshot import build_status_snapshot
    from core.status_render import render_status_text
    snap = await asyncio.to_thread(
        build_status_snapshot, user_id, data_dir=data_dir, task_agent=task_agent,
        session_id=session_id, include_balances=True)
    # `/status` is the short form (core.status_render.BRIEF_SECTIONS); `/status all`
    # is every section. Health leads both and counts every section's issues.
    return render_status_text(snap, title="Status:", prefix="• ",
                              health_limit=_STATUS_HEALTH_LIMIT, brief=not full)


#: Health lines one chat `/status` renders before rolling the rest into a count
#: (a phone screen; the console/CLI keep the full list).
from core.status_render import SEAT_HEALTH_LIMIT as _STATUS_HEALTH_LIMIT  # noqa: E402


def _pause_line(data_dir: str) -> str:
    """The cheap (no snapshot) pause line for verbs that build no header (/goals)."""
    try:
        from core.status_render import pause_headline_from
        from core.surfaces.owner_admin import pause_state
        return pause_headline_from(pause_state(data_dir).to_dict())
    except Exception as e:
        logger.warning("telegram pause line failed: %s", e, exc_info=True)
        return f"autonomy: unavailable ({type(e).__name__}: {str(e)[:120]})"


def _cwd_reply() -> str:
    """`/cwd` (C67) — the directory I am actually working in, read-only.

    ⚠️ Three different answers hide behind "where are you working": the process
    CWD (what a shell command would touch), the data home (where my stores
    live) and the project root (what a coding turn edits). They are routinely
    different on a deployed box, so all three are named rather than one being
    picked and passed off as the answer. Never raises — a path I cannot resolve
    is rendered as unavailable with its reason, never as blank.
    """
    import os as _os
    lines = ["Where I am working:"]
    try:
        lines.append(f"• process directory: `{_os.getcwd()}`")
    except Exception as exc:
        lines.append(f"• process directory: unavailable ({type(exc).__name__})")
    try:
        from core.runtime_paths import effective_data_home
        lines.append(f"• data home (my stores): `{effective_data_home()}`")
    except Exception as exc:
        lines.append(f"• data home: unavailable ({type(exc).__name__}: "
                     f"{str(exc)[:80]})")
    try:
        from core.instance import resolve_instance_id
        lines.append(f"• instance: {resolve_instance_id()}")
    except Exception as exc:
        lines.append(f"• instance: unavailable ({type(exc).__name__})")
    lines.append("Files I produced: /files · my stores' health: /status")
    return "\n".join(lines)


def _goals_reply(user_id: str, data_dir: str, board: Optional[Any] = None) -> str:
    """owner-UX P4 T2: goal board summary — counts by status + up to 5 most
    recent open/running goals.

    ``board`` lets a caller share an already-open ``GoalBoard`` (see
    ``_status_reply``'s docstring); a bare call without one opens its own.
    """
    from modules.goals.board import (
        STATUS_READY, STATUS_RUNNING, STATUS_TRIAGE, GoalBoard)
    gb = board if board is not None else GoalBoard(goals_db_path(data_dir))
    # Counts over EVERY row and a newest-first open list, both in SQL. The old
    # ``gb.list(limit=1000)`` scan is a ``priority DESC`` window that evicts the
    # newest low-priority rows (the manifest stream legs) first once the board
    # outgrows the limit — the exact eviction the agent's goal_list hit at 100
    # rows on 2026-08-29.
    # The DAG is the answer to "why is nothing moving": a `waiting` row is
    # parked behind a prerequisite, and goal_edges has held that fact all
    # along while the surface rendered the bare word "waiting" (2026-09-08 —
    # the owner asked "so the trading goal is running?" and could not tell).
    # core.goal_board_render also leads with running/blocked, because those are
    # the only rows that need a human.
    from core.goal_board_render import render_board

    return render_board(gb, user_id=user_id)


def _seat_via(result: Any) -> str:
    """The seat a line ACTUALLY arrived on, for an audit `via`. This harness is
    the shared inbound actor (console, discord, slack … reach it too), so a
    literal "telegram" is a lie on every other seat (CLI14)."""
    from core.surfaces.seat_names import seat_via
    src = getattr(getattr(getattr(result, "inbound", None), "identity", None), "source", None)
    return seat_via(getattr(src, "surface_id", ""))


def _pause_reply(data_dir: str, cmd: str, args: list, *, via: str = "telegram") -> str:
    """`/pause`, `/halt` (alias) and `/resume` from the phone — thin over the
    ONE owner-admin pause API (031); the reply is the VERIFIED (read-back)
    state, never a checkmark on a write. ``via`` is the seat (``_seat_via``)."""
    from core.surfaces.owner_admin import (pause_autonomy, render_pause_result,
                                           render_resume_result, resume_autonomy_scopes)
    from core.surfaces.owner_intent import parse_pause_args
    try:
        scopes, minutes = parse_pause_args([] if cmd == "/halt" else list(args))
    except ValueError as e:
        return f"⚠️ {e}"
    if cmd == "/resume":
        if minutes is not None:
            return "⚠️ /resume takes scopes only (no duration) — e.g. /resume trading"
        res = resume_autonomy_scopes(data_dir, scopes=None if scopes == ("all",) else scopes,
                                     via=via)
        return render_resume_result(res, halt_hint="/pause")
    res = pause_autonomy(data_dir, scopes=scopes, duration_minutes=minutes,
                         reason=f"{cmd} {' '.join(args)}".strip(), via=via)
    return render_pause_result(res, resume_hint="/resume")


# 125 S14: /missed, /journey, /mode and the health header moved below the seats
# (core/surfaces/owner_reads.py); the old names stay importable (the harness
# re-exports them).
from core.surfaces.owner_reads import (  # noqa: E402,F401
    MISSED_WIDTH as _MISSED_WIDTH,
    health_header as _health_header,
    journey_reply,
    missed_reply as _missed_reply,
    mode_reply as _mode_reply,
)
