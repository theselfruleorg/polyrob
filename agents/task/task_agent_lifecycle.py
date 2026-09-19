"""Session lifecycle housekeeping: periodic cleanup, stale/old-session eviction, per-user limits + credit checks, lookup and cancel-by-id.

Split out of ``agents/task_agent_lite.py`` (S8, 2026-08-29) as a mixin over ``TaskAgent``;
every method is verbatim and relies on the host for the session registry, container,
config and chat maps. Logs under the historical ``agents.task_agent_lite`` logger name.
"""
import logging
from core.exceptions import AgentError, SessionOwnershipError
from datetime import datetime
from typing import Optional, Dict, Any, Union, List
import asyncio
import json
import re
import time
from agents.task.task_agent_support import (
    MAX_SESSIONS_PER_USER,
    _spawn_detached,
)

logger = logging.getLogger("agents.task_agent_lite")


class TaskAgentLifecycleMixin:
    async def aclose(self) -> None:
        """Cancel the periodic background loops. Idempotent and fail-open.

        Safe to call when the loops were never started (the ImportError path sets
        task_available=False and skips creation, leaving _bg_tasks empty).
        """
        tasks = getattr(self, "_bg_tasks", None) or []
        for t in tasks:
            try:
                t.cancel()
            except Exception:
                pass
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._bg_tasks = []
    async def _periodic_cleanup(self):
        """Periodically clean up old sessions (TTL and LRU eviction)."""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                await self._cleanup_old_sessions()
            except Exception as e:
                logger.error(f"Error in periodic cleanup: {e}")
    async def _periodic_workspace_cleanup(self):
        """Periodically clean up old session workspaces (destructive: rmtree).

        Returns immediately in a process that does not own the GC. Belt to the
        spawn-site brace: a second owner is a data-loss bug, not a slow tick.
        """
        if not getattr(self, "_owns_workspace_gc", True):
            logger.debug("workspace GC not owned by this process — loop not running")
            return
        # 056 WS8: the first pass runs 10 min after start, then daily. With
        # ~28 restarts/day on prod the old "sleep 24 h first" never fired once,
        # and 1,642 stale session dirs (4.0 GB) accumulated.
        delay = 600
        while True:
            try:
                await asyncio.sleep(delay)
                delay = 86400

                if self.session_manager:
                    cleaned = self.session_manager.cleanup_old_workspaces(max_age_days=7)
                    logger.info(f"Workspace cleanup: removed {cleaned} old workspaces")
                self._session_dir_gc()

            except Exception as e:
                logger.error(f"Error in workspace cleanup: {e}")

    def _session_dir_gc(self) -> None:
        """056 WS8: collect stale per-session dirs (dry-run unless
        SESSION_DIR_GC_APPLY=true). Live/resident session ids are protected;
        the report is logged and recorded as a `session_gc` event so the owner
        sees the numbers before the switch is flipped."""
        try:
            from agents.task.path import pm
            from agents.task.session_registry import resident_session_ids
            from core.session_gc import collect_stale_sessions
            sessions_root = str(getattr(pm(), "data_root", "") or "")
            protect = set()
            try:
                protect.update(resident_session_ids(self))
            except Exception:
                pass
            try:
                if self.session_manager:
                    protect.update(self.session_manager.get_active_sessions())
            except Exception:
                pass
            rep = collect_stale_sessions(sessions_root, max_age_days=14, protect=protect)
            logger.info("session gc (%s): candidates=%s removed=%s bytes=%s kept_recent=%s "
                        "protected=%s errors=%s sample=%s",
                        "APPLY" if rep.get("apply") else "DRY-RUN", rep.get("candidates"),
                        rep.get("removed"), rep.get("bytes"), rep.get("kept_recent"),
                        rep.get("protected"), rep.get("errors"), rep.get("sample"))
            try:
                from core.event_log import get_event_log, event_log_enabled
                from core.event_kinds import SESSION_GC
                if event_log_enabled():
                    get_event_log().record(SESSION_GC, user_id="", source="lifecycle",
                                           **{k: v for k, v in rep.items() if k != "sample"})
            except Exception:
                pass
        except Exception as e:
            logger.warning("session gc skipped: %s", e)
    @staticmethod
    def _session_age_seconds(created_at, now: Optional[datetime] = None) -> Optional[float]:
        """Age in seconds from an ISO 'created_at' string, or None if unparseable."""
        if not created_at:
            return None
        try:
            ts = datetime.fromisoformat(created_at)
        except (ValueError, TypeError):
            return None
        return ((now or datetime.now()) - ts).total_seconds()
    async def _cleanup_stale_created_sessions(self) -> int:
        """Retire sessions stuck in 'created' that never ran.

        A 'created' session counts toward the per-user limit (get_active_sessions), but
        the TTL/LRU GC keys off _session_last_activity, which is only written on the run
        path. So a created-but-never-run session is invisible to that GC and would consume
        the per-user slot forever, eventually blocking all new runs. This sweep is keyed
        off 'created_at' (always set at create time) and deletes both the in-memory entry
        and the on-disk metadata.

        Returns the number of sessions retired.
        """
        if not self.session_manager:
            return 0

        now = datetime.now()
        ttl = self.created_session_ttl_seconds
        stale = []
        # get_active_sessions() (no user filter) reads in-memory _sessions only — exactly
        # the entries that count toward the per-user limit. Disk-only sessions are excluded.
        for session_id in self.session_manager.get_active_sessions():
            info = self.session_manager.get_session_info(session_id) or {}
            if info.get('status') != 'created':
                continue
            # A run-path activity timestamp means this is a normal in-flight session.
            if session_id in self._session_last_activity:
                continue
            age = self._session_age_seconds(info.get('created_at'), now)
            if age is None or age <= ttl:
                continue
            stale.append(session_id)

        for session_id in stale:
            logger.info(
                f"GC: retiring stale 'created' session {session_id} "
                f"(age > {ttl}s, never ran)"
            )
            self._registry.remove(session_id)  # no-op if never registered
            self._session_execution_locks.pop(session_id, None)
            self.session_manager.cleanup_session(session_id, delete_files=True)

        return len(stale)
    async def _cleanup_old_sessions(self):
        """Remove sessions that haven't been active recently."""
        # First retire never-run 'created' sessions that the activity-keyed GC below
        # can never see (they have no _session_last_activity entry).
        await self._cleanup_stale_created_sessions()

        now = time.time()
        ttl_threshold = now - self.session_ttl_seconds

        sessions_to_evict = []

        # Find sessions past TTL
        for session_id, last_activity in list(self._session_last_activity.items()):
            if last_activity < ttl_threshold:
                sessions_to_evict.append((session_id, 'ttl'))

        # Evict by TTL
        for session_id, reason in sessions_to_evict:
            logger.info(f"Evicting session {session_id} (TTL {self.session_ttl_seconds}s exceeded)")
            await self._evict_session(session_id, reason='ttl')

        # Also enforce max sessions limit (LRU eviction)
        if self._registry.count() > self.max_sessions_in_memory:
            # Sort by last activity (oldest first)
            sorted_sessions = sorted(
                self._session_last_activity.items(),
                key=lambda x: x[1]
            )

            # Calculate how many to remove
            excess_count = self._registry.count() - self.max_sessions_in_memory

            # Remove oldest
            for session_id, _ in sorted_sessions[:excess_count]:
                logger.info(f"Evicting session {session_id} (max sessions limit: {self.max_sessions_in_memory})")
                await self._evict_session(session_id, reason='lru')
    def _prune_chat_mappings(self, session_id: str) -> None:
        """Drop any _chat_sessions / _chat_locks entries pointing at an evicted session.

        P7/F2 finalization: these process-global dicts (chat-key → session_id, and its
        per-key lock) grew UNBOUNDED because eviction pruned the registry + activity/
        execution/recreate-lock maps but never these two — a long-lived multi-user
        process accumulated a stale entry per (user, chat) forever.
        """
        chat_sessions = getattr(self, "_chat_sessions", None)
        if not chat_sessions:
            return
        stale = [k for k, sid in list(chat_sessions.items()) if sid == session_id]
        locks = getattr(self, "_chat_locks", None)
        for k in stale:
            chat_sessions.pop(k, None)
            if locks is not None:
                locks.pop(k, None)
    async def _evict_session(self, session_id: str, reason: str = 'unknown'):
        """Fully evict a session from memory with state persistence.

        Args:
            session_id: Session to evict
            reason: Eviction reason (ttl, lru, manual)
        """
        try:
            # a3: never reap a session with a live run loop. A held execution lock means
            # a turn is in flight — tearing it down mid-run would corrupt state and drop
            # the user's reply. Leave it; the next cleanup pass collects it once idle.
            exec_lock = self._session_execution_locks.get(session_id)
            if exec_lock is not None and exec_lock.locked():
                logger.debug(
                    f"Skipping eviction of {session_id} (reason: {reason}) — run in flight"
                )
                return

            orchestrator = self._registry.get(session_id)
            if orchestrator:
                # Save HITL state BEFORE cleanup (queued messages not saved by cleanup)
                # NOTE: Message history is saved by cleanup(full_cleanup=True) - no need to duplicate
                try:
                    for agent in orchestrator.agents.values():
                        # Save HITL state (queued messages, callbacks info)
                        # This is NOT handled by cleanup() so we must do it here
                        if hasattr(agent, 'hitl_manager') and agent.hitl_manager:
                            try:
                                hitl_state = agent.hitl_manager.get_state()
                                if hitl_state.get('queued_messages'):
                                    from agents.task.path import pm
                                    import json
                                    hitl_path = pm().create_file_path(
                                        session_id=session_id,
                                        subdir_name="memory",
                                        filename="hitl_state.json",
                                        user_id=orchestrator.user_id
                                    )
                                    hitl_path.parent.mkdir(parents=True, exist_ok=True)
                                    with open(hitl_path, 'w') as f:
                                        json.dump(hitl_state, f, indent=2, default=str)
                                    logger.info(f"💾 Saved HITL state for agent {agent.agent_id} ({len(hitl_state.get('queued_messages', []))} queued messages)")
                            except Exception as e:
                                logger.error(f"Failed to save HITL state for agent {agent.agent_id}: {e}")
                except Exception as e:
                    logger.error(f"Failed to save HITL state during eviction: {e}")

                # Capture telemetry before eviction
                if self.telemetry:
                    try:
                        # Calculate session age
                        last_activity = self._session_last_activity.get(session_id, time.time())
                        session_age = time.time() - last_activity

                        self.telemetry.capture_event(
                            event_type="session_eviction",
                            data={
                                "session_id": session_id,
                                "reason": reason,
                                "session_age_seconds": session_age,
                                "total_sessions_before": self._registry.count(),
                                "max_sessions_limit": self.max_sessions_in_memory
                            }
                        )
                    except Exception as e:
                        logger.debug(f"Failed to emit eviction telemetry: {e}")

                # STEP 2: Full cleanup - release all resources
                await orchestrator.cleanup(
                    preserve_workspace=True,
                    status="suspended",
                    full_cleanup=True  # Release agents, LLMs, tools
                )

                # Remove from all tracking
                self._registry.remove(session_id)
                self._session_last_activity.pop(session_id, None)
                self._session_execution_locks.pop(session_id, None)  # Remove execution lock
                self._recreate_locks.pop(session_id, None)  # a3: don't leak recreate locks
                self._prune_chat_mappings(session_id)  # P7/F2: don't leak chat-key → session maps

                logger.info(f"Evicted session {session_id} from memory (reason: {reason}) with persistence")
        except Exception as e:
            logger.error(f"Error evicting session {session_id}: {e}")
    async def _cleanup(self) -> None:
        """Cleanup orchestrators and resources during TaskAgent shutdown."""
        # Cleanup active orchestrators - full cleanup since we're shutting down
        for session_id, orchestrator in self._registry.items():
            try:
                await orchestrator.cleanup(
                    preserve_workspace=True,
                    preserve_agents=False,  # Clear agents during shutdown
                    full_cleanup=True  # Release all resources
                )
            except Exception as e:
                logger.error(f"Error cleaning up orchestrator for {session_id}: {e}")

        self._registry.clear()
        self._session_last_activity.clear()
        self._session_execution_locks.clear()
        self._recreate_locks.clear()  # a3: don't leak recreate locks across shutdown
        self._resume_locks.clear()
        logger.info("TaskAgent cleanup complete")
    async def cleanup(self) -> None:
        """Clean up resources."""
        await self._cleanup()
    async def process_input(
        self,
        user_input: str,
        user_id: str,
        chat_id: Optional[str] = None,
        **kwargs
    ) -> str:
        """Process user input - delegates to process_user_message."""
        return await self.process_user_message(user_id, user_input, kwargs)
    async def start_conversation(
        self,
        user_id: str,
        chat_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Start a conversation - task agent doesn't need this."""
        return True
    def list_sessions(self, user_id: str) -> list:
        """List user's sessions."""
        if not self.task_available:
            return []
        return self.session_manager.get_active_sessions(user_id)
    def _count_user_sessions(self, user_id: str) -> int:
        """Count active sessions for a user.

        Args:
            user_id: User identifier

        Returns:
            Number of active sessions for this user
        """
        if not self.session_manager:
            return 0

        sessions = self.session_manager.get_active_sessions(user_id)
        return len(sessions)
    def _check_user_session_limit(self, user_id: str) -> bool:
        """Check if user has reached session limit.

        Args:
            user_id: User identifier

        Returns:
            True if under limit, False if at/over limit
        """
        current_count = self._count_user_sessions(user_id)
        max_limit = getattr(self.config, 'max_sessions_per_user', MAX_SESSIONS_PER_USER)

        if current_count >= max_limit:
            logger.warning(
                f"User {user_id} at session limit: {current_count}/{max_limit}"
            )
            return False

        return True
    async def _validate_user_credits(self, user_id: str, min_credits: int = 1):
        """Validate user has sufficient credits before session creation.

        This is a pre-validation check to fail fast before creating resources.
        The actual billing happens during LLM calls via usage_tracker.

        Args:
            user_id: User to validate
            min_credits: Minimum credits required (default: 1 for session creation)

        Raises:
            AgentError: If insufficient credits
        """
        # Skip for admin users and x402 users (they already paid via x402 middleware)
        try:
            tier_mgr = self.container.get_service('tier_manager')
            if tier_mgr:
                user_tier = await tier_mgr.get_user_tier(user_id)
                if user_tier == 'admin':
                    logger.debug(f"Admin user {user_id} bypassed credit check")
                    return
                if user_tier == 'x402':
                    # x402 users pay per-request via middleware, not via credits
                    logger.debug(f"x402 user {user_id} bypassed credit check (paid via x402)")
                    return
        except Exception as e:
            logger.debug(f"Could not check tier (proceeding with credit check): {e}")

        # Check balance
        try:
            balance_mgr = self.container.get_service('balance_manager')
            if not balance_mgr:
                logger.warning("Balance manager not available - skipping credit check")
                return

            has_credits = await balance_mgr.has_sufficient_balance(user_id, min_credits)
            if not has_credits:
                balance = await balance_mgr.get_balance(user_id)
                available = balance.get('balance', 0) if balance else 0
                raise AgentError(
                    f"Insufficient credits. Required: {min_credits}, Available: {available}. "
                    f"Please add credits at /api/payments/deposit to continue."
                )
        except AgentError:
            raise  # Re-raise our own error
        except Exception as e:
            logger.warning(f"Credit check failed (allowing session): {e}")
    def is_autonomous_session(self, session_id: str) -> bool:
        """031: is *session_id* an autonomous run (goal/cron/planner) rather than
        an owner-driven chat session? Read by ``core.autonomy_runtime`` on the
        pause edge (duck-typed — core may not import agents.*) so a pause cancels
        a goal run's background delegations and never the owner's own."""
        from agents.task.goals.autonomy_marker import is_autonomous
        return is_autonomous(session_id)

    def cancel_autonomous_delegations(self, reason: str) -> int:
        """031: cancel the running background delegations of every RESIDENT
        autonomous session (never the owner's own chat). Called by
        ``core.autonomy_runtime`` on the paused edge (duck-typed). Returns the
        number cancelled; fail-open per session."""
        from agents.task.goals.autonomy_marker import is_autonomous
        n = 0
        registry = getattr(self, "_registry", None)
        lister = getattr(registry, "session_ids", None)
        for sid in (list(lister()) if callable(lister) else []):
            if not is_autonomous(sid):
                continue
            try:
                orch = registry.get(sid)
                reg = getattr(orch, "async_delegation", None) if orch is not None else None
                if reg is not None and hasattr(reg, "cancel_all"):
                    n += int(reg.cancel_all(reason) or 0)
            except Exception:
                self.logger.warning(f"delegation cancel failed for {sid}", exc_info=True)
        return n

    def get_all_sessions(self) -> Dict[str, Any]:
        """Get all sessions (admin function)."""
        if not self.task_available or not self.session_manager:
            return {}
        # Access internal tracking for admin purposes
        return self.session_manager._sessions.copy()
    def _get_user_sessions(self, user_id: str) -> Dict[str, Any]:
        """Get all sessions for a user."""
        if not self.session_manager:
            return {}
        
        sessions = {}
        for session_id in self.session_manager.get_active_sessions(user_id):
            info = self.session_manager.get_session_info(session_id)
            if info:
                sessions[session_id] = info
        return sessions
    def _remove_session(self, user_id: str, session_id: str) -> None:
        """Remove a session and cleanup."""
        orchestrator = self._registry.remove(session_id)
        if orchestrator:
            _spawn_detached(orchestrator.cleanup())
    async def get_session_by_id(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session info by ID (API compatibility)."""
        if not self.session_manager:
            return None
        
        info = self.session_manager.get_session_info(session_id)
        if info:
            # Update active_sessions for compatibility
            user_id = info.get('user_id')
            if user_id:
                if user_id not in self.active_sessions:
                    self.active_sessions[user_id] = {}
                self.active_sessions[user_id][session_id] = info
        return info
    async def cancel_session_by_id(self, session_id: str, force: bool = False) -> bool:
        """Cancel a session by ID (API compatibility)."""
        try:
            info = self.session_manager.get_session_info(session_id)
            if info:
                user_id = info.get('user_id')
                return await self.cancel_session(user_id, force=force, session_id=session_id)
            return False
        except Exception as e:
            logger.error(f"Failed to cancel session {session_id}: {e}")
            return False