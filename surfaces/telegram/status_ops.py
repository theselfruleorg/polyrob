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
                        data_dir: str, board: Optional[Any] = None) -> str:
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
    return render_status_text(snap, title="Status:", prefix="• ", health_limit=_STATUS_HEALTH_LIMIT)


#: Health lines one chat `/status` renders before rolling the rest into a count
#: (a phone screen; the console/CLI keep the full list).
_STATUS_HEALTH_LIMIT = 8


async def _health_header(user_id: str, data_dir: str, task_agent: Any = None,
                         limit: int = 4) -> list:
    """The mandatory health block other read verbs (/mode, /recap, /goals)
    prepend — a degraded instance renders first on EVERY seat. Cheap path (no
    money, no balances). Never raises: a builder failure renders as its own
    'unavailable' line rather than silently producing no header."""
    try:
        from core.status_snapshot import build_status_snapshot
        from core.status_render import render_health_lines
        from core.status_render import pause_headline
        snap = await asyncio.to_thread(
            build_status_snapshot, user_id, data_dir=data_dir, task_agent=task_agent,
            include_money=False)
        # 031: the pause state leads every seat, before health.
        return [pause_headline(snap)] + render_health_lines(snap, prefix="• ", limit=limit)
    except Exception as e:
        logger.warning("telegram health header failed: %s", e, exc_info=True)
        return [f"Health: unavailable ({type(e).__name__}: {str(e)[:120]})"]


def _pause_line(data_dir: str) -> str:
    """The cheap (no snapshot) pause line for verbs that build no header (/goals)."""
    try:
        from core.status_render import pause_headline_from
        from core.surfaces.owner_admin import pause_state
        return pause_headline_from(pause_state(data_dir).to_dict())
    except Exception as e:
        logger.warning("telegram pause line failed: %s", e, exc_info=True)
        return f"autonomy: unavailable ({type(e).__name__}: {str(e)[:120]})"


def _missed_reply(user_id: str, data_dir: str, args: list) -> str:
    """`/missed [n]` — the owner notices the delivery rail could not send
    live (durable ``owner_notice`` rows the rail writes instead of dropping
    the text): suppressed by the daily cap, held by an owner pause, or
    undelivered (no live sink / send failed) — newest first. 2026-08-28: 91
    of 92 notices in 24h were capped and nothing let the owner read them; A7
    (2026-09-14) widened this to every marker (it used to match the cap
    marker only, so a paused/undelivered notice was unreadable). A thin
    renderer over ``core.surfaces.missed.missed_notices`` — CLI/REPL reuse
    the same query."""
    try:
        n = max(1, min(20, int(args[0]))) if args else 5
    except (TypeError, ValueError):
        return "Usage: /missed [n] (1-20, default 5)"
    try:
        from core.surfaces.missed import missed_notices
        rows = missed_notices(user_id, data_dir, n)
    except Exception as e:
        return f"Missed messages: unavailable ({type(e).__name__}: {str(e)[:120]})"
    if not rows:
        return "No undelivered messages on record."
    # D36: the SHARED renderer (`core.surfaces.missed.format_notice_lines`),
    # which WRAPS. This seat used to hard-clip every notice at 240 characters —
    # in the one verb that exists so the owner can recover text he never
    # received, which makes the clip a second suppression of the same message.
    from core.surfaces.missed import format_notice_lines
    lines = [f"Last {len(rows)} undelivered message(s), newest first:"]
    for r in rows:
        lines.extend(format_notice_lines(
            float(r.get("ts") or 0), str(r.get("kind") or "capped"),
            str(r.get("text") or "").strip(), gutter="• ", width=_MISSED_WIDTH))
    lines.append("Raise the cap: /config set delivery.daily_cap N")
    return "\n".join(lines)


#: A chat bubble re-wraps proportional text, so the terminal's 80 columns are
#: the wrong measure here. Narrow enough that one notice line stays one line.
_MISSED_WIDTH = 60


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


#: Entry lines one chat `/recap` may render before rolling the rest into a count.
_RECAP_CHAT_LIMIT = 15


def _recap_reply(user_id: str, data_dir: str, args: list) -> str:
    """owner-UX P4 T2: `/recap [window]` (default 24h) over core.recap (T1).

    Bounded for chat (G14): an unbounded `/recap 7d` renders every event,
    episode and authored skill one line each — several phone messages of
    scrollback. The terminal keeps the full listing.
    """
    from core.recap import build_recap, format_recap_markdown
    window = args[0] if args else "24h"
    try:
        entries = build_recap(user_id, data_dir, window=window)
    except ValueError:
        return (f"Invalid recap window {window!r} — expected e.g. '30m' / '24h' / '7d' "
                "(a bare number of seconds also works).")
    return format_recap_markdown(entries, window, limit=_RECAP_CHAT_LIMIT)


def _goals_reply(user_id: str, data_dir: str, board: Optional[Any] = None) -> str:
    """owner-UX P4 T2: goal board summary — counts by status + up to 5 most
    recent open/running goals.

    ``board`` lets a caller share an already-open ``GoalBoard`` (see
    ``_status_reply``'s docstring); a bare call without one opens its own.
    """
    from agents.task.goals.board import (
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
    src = getattr(getattr(getattr(result, "inbound", None), "identity", None), "source", None)
    via = str(getattr(src, "surface_id", "") or "telegram")
    return "console" if via == "webview" else via


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


def _mode_reply() -> str:
    """`/mode` — JUST the effective-posture card + how to change it (030 WS-E4,
    read-only v1).

    `/status` already embeds the card inside a long snapshot; `/mode` answers
    the single question "what may this instance do right now, and why" with
    nothing else around it. `/config set` writes preferences AND the flag keys
    the config oracle allows from chat (the master switch); mode and posture
    are refused remotely, so the copy says they need the server.
    """
    lines = ["Effective posture (all axes):"]
    try:
        from core.config_policy.posture_card import render_posture_card
        lines.extend(render_posture_card(prefix="• "))
    except Exception:
        logger.debug("telegram /mode posture card failed", exc_info=True)
        return ("I could not read my own posture card. Ask me again in a "
                "moment, or run `polyrob autonomy status` on my box.")
    lines.append("")
    # A11: `/config set` is NOT prefs-only — it writes the flag keys the
    # oracle (core.config_service.set_value, surface="telegram") allows. The
    # master switch is one of them; the capability mode and the postures are
    # refused on every remote surface, so they need the server.
    lines.append("To change: `/config set AUTONOMY_ENABLED true|false` turns the "
                 "master on or off from here (applies on the next restart). The "
                 "capability mode and the postures need the server. Live pause: "
                 "/pause and /resume.")
    return "\n".join(lines)
