"""098 CL-07: the REPL session's status when the REPL exits.

Exit is not a cancel. A run that is still live is cancelled
(``cancel_session``); otherwise only a session that never finished a turn
(``created``/``initializing``/``resumed``) moves to ``cancelled``, through the CAS of the session status table — a ``completed``,
``failed`` or ``suspended`` session keeps its status.
"""
from __future__ import annotations

from typing import Any

#: Statuses an idle REPL exit closes as ``cancelled`` (each an edge of
#: ``agents.task.agent.session.SESSION_MACHINE``).
EXIT_CANCEL_FROM = frozenset({"created", "initializing", "running", "resumed"})


def close_on_exit(session_manager: Any, session_id: str) -> bool:
    """CAS the session to ``cancelled`` when it is in :data:`EXIT_CANCEL_FROM`.
    True when this call wrote ``cancelled``. Never raises."""
    try:
        cur = (session_manager.get_session_info(session_id) or {}).get("status")
        if cur not in EXIT_CANCEL_FROM:
            return False
        return bool(session_manager.try_transition_status(session_id, cur, "cancelled"))
    except Exception:
        return False


def needs_cancel(ui_state: Any, session_manager: Any, session_id: str) -> bool:
    """True when a run of the session is live: a REPL turn, or a background
    run (status ``running``) — then the exit stops it with ``cancel_session``.
    An unreadable state cancels, as before."""
    try:
        if ui_state.lifecycle.is_active():
            return True
        cur = (session_manager.get_session_info(session_id) or {}).get("status")
        return cur == "running"
    except Exception:
        return True
