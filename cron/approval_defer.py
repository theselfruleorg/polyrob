"""Was this cron run cut off waiting on the OWNER, rather than failing? (FIX 3)

A cron job may set a hard cap shorter than the durable owner-approval wait
(``payment_approval_timeout_sec()``, 300s; see
``tools/controller/approval.py::approval_wait_timeout_sec``). In that case an
approval-gated verb attempted inside the run can be cut off while waiting for
the owner, and the scheduler must not record that as the JOB's failure. The
durable ask survives (the owner can still ``/approve``, leaving a one-shot grant
the next attempt redeems), so only the accounting would be wrong.

This module answers exactly one question, and answers it narrowly:

    did THIS run open a NEW owner approval ask that is still OPEN?

Two deliberate boundaries:

- **created during this run.** An ask that predates the run is not this run's
  excuse. The provider dedups by request hash, so a second attempt re-polls the
  SAME (now pre-existing) ask — which makes the deferral self-bounding at ONE per
  ask instead of an unbounded "the owner never answered so keep retrying" loop.
- **still OPEN.** A decided ask means the run HAD its answer; a cut-off after that
  is a genuine timeout.

Fail-open in the direction of today's behaviour: any error, missing store, or
missing tenant answers "no" — i.e. the timeout is recorded as a failure exactly
as before. Nothing here ever CREATES ``goals.db``.
"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def goals_db_beside(cron_db_path: str) -> str:
    """``goals.db`` in the same data home as *cron_db_path* (they are siblings —
    both are opened from ``data_dir`` by the autonomy runtime)."""
    return os.path.join(os.path.dirname(os.path.abspath(cron_db_path)), "goals.db")


def open_owner_ask_since(user_id: str, since: float, *,
                         cron_db_path: Optional[str] = None,
                         board=None) -> Optional[str]:
    """The id of an OPEN ``tool_approval`` ask for *user_id* created at/after
    *since*, or ``None``.

    Read-only: a missing ``goals.db`` returns ``None`` rather than creating one.
    """
    if not user_id:
        return None
    try:
        if board is None:
            if not cron_db_path:
                return None
            path = goals_db_beside(cron_db_path)
            if not os.path.exists(path):
                return None
            from agents.task.goals.board import GoalBoard
            board = GoalBoard(path)
        from agents.task.goals.board import ASK_OPEN
        from tools.controller.approval_queue import TOOL_APPROVAL_ASK_KIND
        for ask in board.asks(user_id=user_id, status=ASK_OPEN):
            payload = ask.payload or {}
            if payload.get("ask_kind") != TOOL_APPROVAL_ASK_KIND:
                continue
            if float(ask.created_at or 0.0) >= float(since):
                return ask.id
    except Exception:
        logger.debug("owner-ask probe failed; recording the timeout as a failure",
                     exc_info=True)
    return None


__all__ = ["goals_db_beside", "open_owner_ask_since"]
