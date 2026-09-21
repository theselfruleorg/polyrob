"""h_goals_view.py — the REPL's ``/goals`` view (C15/C16/E17, 2026-09-21).

Its own module because ``handlers.py`` sits at its size ratchet
(``tests/test_file_size_ratchet.py``: extract new behaviour, never grow the
god-file).

⚠️ ``GoalBoard.list`` is the DISPATCHER's order (``priority DESC, created_at
ASC LIMIT``). Used as a "what is on my board" VIEW it is wrong: on the 409-row
prod board it showed the OLDEST rows and zero live legs, so the agent told the
owner it had no trading goals while ten clean cycles had run. Every view here
reads :meth:`GoalBoard.list_recent` (newest first, tenant-scoped) and
:meth:`GoalBoard.status_counts` (every row, never a window).

Honest states: an unreadable board renders ``unavailable(<reason>)``, never an
empty list — and a READ never creates the store.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from cli.ui import candy

#: How many rows the compact REPL view prints.
_VIEW_LIMIT = 10


def _board_path() -> str:
    """The goals DB under the ONE owner/admin home seam (C2)."""
    from cli._admin_home import admin_data_dir
    from core.runtime_paths import goals_db_path
    return goals_db_path(admin_data_dir(write=False))


def read_board(user_id: str, *, limit: int = _VIEW_LIMIT
               ) -> Tuple[Optional[List[Any]], Dict[str, int], Optional[str]]:
    """``(rows, counts, unavailable_reason)`` for *user_id*.

    ``rows is None`` means the board could not be read — the caller must say so
    rather than print an empty list. A missing file is NOT an error and is NOT
    created: it reads as "no goals yet".
    """
    path = _board_path()
    if not os.path.exists(path):
        return [], {}, None
    try:
        from agents.task.goals.board import GoalBoard
        board = GoalBoard(path)
        rows = list(board.list_recent(user_id=user_id, limit=limit))
        counts = dict(board.status_counts(user_id=user_id))
        return rows, counts, None
    except Exception as exc:  # unreadable/locked store — never a confident zero
        return None, {}, f"{type(exc).__name__}: {exc}"


def _counts_line(counts: Dict[str, int]) -> str:
    total = sum(counts.values())
    parts = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
    return f"{candy.GUTTER}{total} goal(s) on the board — {parts}"


def goals_view(user_id: str, *, limit: int = _VIEW_LIMIT) -> str:
    """The rendered ``/goals`` body."""
    rows, counts, reason = read_board(user_id, limit=limit)
    if rows is None:
        return (f"{candy.GUTTER}unavailable ({reason}) — that is UNKNOWN, not "
                f"an empty board.")
    if not rows:
        # E17: one grammar, no flag names — name a verb the owner can RUN.
        # ⚠️ `/goal` steers an EXISTING goal (show|ready|pause|resume|retry|
        # cancel); there is no `/goal create`, so naming it sent the owner to a
        # usage line instead of a board. `/trade` is the chat seat that seeds a
        # run; `polyrob goals create` is the general one.
        return candy.empty("goals",
                           "seed one with `/trade <what to do>`, or "
                           "`polyrob goals create` for anything else")
    lines = [candy.status_line(g.status, f"{g.id[:8]}: {g.title[:40]}") for g in rows]
    if counts:
        lines.append("")
        lines.append(_counts_line(counts))
    if sum(counts.values()) > len(rows):
        # NOT a promise of every row: `goals list` is itself windowed
        # (``-n``, max 500), so it cannot show more than its own ceiling.
        lines.append(f"{candy.GUTTER}(newest {len(rows)} shown — "
                     f"`polyrob goals list -n 100` widens the window)")
    return "\n".join(lines)
