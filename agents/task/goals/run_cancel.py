"""Stop a goal's in-process run the moment the owner pauses or cancels it (AG-17).

Without this the run lives on until its next heartbeat (``ttl/3``, 300 s by
default) — the owner read "paused" while it still spent. The dispatcher of each
process registers itself here for its board's ``goals.db``; the board calls
:func:`cancel_local_run` after a winning pause/cancel CAS. A run in ANOTHER
process stops at its next heartbeat (the cleared claim fails it).
"""
from __future__ import annotations

import logging
import os
import weakref
from typing import Any, Dict

logger = logging.getLogger(__name__)

#: abspath(goals.db) -> weak set of dispatchers with a ``cancel_goal_run(goal_id)``.
_REG: Dict[str, "weakref.WeakSet[Any]"] = {}


def register(db_path: str, dispatcher: Any) -> None:
    _REG.setdefault(os.path.abspath(db_path), weakref.WeakSet()).add(dispatcher)


def cancel_local_run(db_path: str, goal_id: str) -> bool:
    """Cancel *goal_id*'s run in this process, if one is live. Never raises."""
    hit = False
    for d in list(_REG.get(os.path.abspath(str(db_path)), ())):
        try:
            hit = bool(d.cancel_goal_run(goal_id)) or hit
        except Exception:
            logger.debug("local run cancel failed for %s", goal_id, exc_info=True)
    return hit
