"""Public session-control verbs for :class:`~agents.task_agent_lite.TaskAgent`.

Extracted 2026-09-08 to bring `task_agent_lite.py` back under its shrink-only size
ratchet ceiling (AGENTS.md: new behaviour gets its own mixin rather than growing
the four large classes). This is the "operate on an EXISTING session from outside"
concern — status, cancel, pause, resume — as opposed to
:class:`TaskAgentLifecycleMixin`, which owns the agent's own internal lifecycle
(periodic cleanup, stale-session eviction, teardown).

Every verb here is tenant-scoped: it resolves the session, refuses when the row is
missing or owned by a different `user_id`, and returns a falsey/`found: False`
result rather than raising. `TaskAgent` composes this via MRO, so
`self.session_manager`, `self._registry` and `self.task_available` are the agent's
own attributes; the mixin owns no state.
"""
import logging
from typing import Any, Dict, Optional

from agents.task.task_agent_support import task_unavailable_message

logger = logging.getLogger(__name__)


class TaskAgentControlMixin:
    """`get_session_status` / `cancel_session` / `pause_session` / `resume_session`."""

    async def get_session_status(
        self,
        user_id: str,
        session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Status of one session, or of *user_id*'s most recent one.

        Args:
            user_id: owning user.
            session_id: a specific session; omitted means the latest active one.

        Returns:
            A status dict; `{'found': False, ...}` when there is nothing to report.
        """
        if not self.task_available:
            return {
                'found': False,
                'message': task_unavailable_message(
                    getattr(self, "_task_unavailable_reason", None)
                )
            }

        if session_id:
            session_info = self.session_manager.get_session_info(session_id)
            if session_info and session_info.get('user_id') == user_id:
                return {
                    'found': True,
                    'id': session_id,
                    'status': session_info.get('status'),
                    'task': session_info.get('task'),
                    'created_at': session_info.get('created_at')
                }
        else:
            sessions = self.session_manager.get_active_sessions(user_id)
            if sessions:
                return await self.get_session_status(user_id, sessions[-1])

        return {
            'found': False,
            'message': 'No session found'
        }

    async def cancel_session(
        self,
        user_id: str,
        force: bool = False,
        session_id: Optional[str] = None
    ) -> bool:
        """Stop a session and persist the terminal status.

        Args:
            user_id: owning user.
            force: also clean the session up (files are kept).
            session_id: the session to cancel; omitted means the latest active one.

        Returns:
            True when the session was cancelled.
        """
        if not self.task_available:
            return False

        if not session_id:
            sessions = self.session_manager.get_active_sessions(user_id)
            if not sessions:
                return False
            session_id = sessions[-1]

        session_info = self.session_manager.get_session_info(session_id)
        if not session_info or session_info.get('user_id') != user_id:
            return False

        # Actually stop the running loop: cancel() sets the flag every agent checks
        # at its next step.
        orchestrator = self._registry.get(session_id)
        if orchestrator:
            try:
                orchestrator.cancel()
                logger.info(f"Called cancel() on orchestrator for session {session_id}")
            except Exception as e:
                logger.error(f"Error calling orchestrator.cancel(): {e}")

        # F3: persist the terminal status unconditionally (idempotent;
        # CREATED→CANCELLED is a valid transition per session.py). orchestrator
        # .cancel() only flips an in-memory flag — it does NOT touch the session's
        # persisted status — so without this an interactively-cancelled session
        # would leak as 'created' on disk. Flipping it here removes the fragile
        # two-hop persistence (the previous else-branch only updated status when
        # the session was NOT running).
        self.session_manager.update_session_status(session_id, 'cancelled')
        logger.debug(f"Session {session_id} status set to cancelled")

        if force:
            self.session_manager.cleanup_session(session_id, delete_files=False)

        logger.info(f"Cancelled session {session_id}")
        return True

    async def pause_session(self, user_id: str, session_id: str) -> bool:
        """Pause (suspend) a session — an honest status op, not a hard stop.

        Only a RUNNING session can suspend (per session.py's transition table:
        RUNNING -> SUSPENDED). Returns True on a successful transition, False if the
        session is unknown, not owned by ``user_id``, or not in a suspendable state.
        Does NOT interrupt an in-flight step — use ``cancel_session`` for that.
        """
        if not self.task_available:
            return False
        info = self.session_manager.get_session_info(session_id)
        if not info or info.get('user_id') != user_id:
            return False
        return self.session_manager.try_transition_status(
            session_id, info.get('status'), 'suspended'
        )

    async def resume_session(self, user_id: str, session_id: str) -> bool:
        """Mark a suspended/failed/completed session resumable (-> RESUMED).

        Flips the persisted status so the normal resume path
        (try_transition_status RESUMED -> RUNNING inside the run loop) will pick it
        up; actually continuing execution still requires ``rob run``/attach. Returns
        False for an unknown/foreign session or one not in a resumable state.
        """
        if not self.task_available:
            return False
        info = self.session_manager.get_session_info(session_id)
        if not info or info.get('user_id') != user_id:
            return False
        cur = info.get('status')
        if cur not in ('suspended', 'failed', 'completed'):
            return False
        return self.session_manager.try_transition_status(session_id, cur, 'resumed')
