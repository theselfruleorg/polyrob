"""TaskAgent - Minimal wrapper for task automation package.

This is a thin wrapper that delegates all real work to the task package components:
- SessionManager: Handles all session tracking and lifecycle
- SessionOrchestrator: Handles agent coordination and execution
- Controller: Handles tool management and action execution

Key principle: NO DUPLICATION - all logic exists in the task package.
"""

import logging
import asyncio
import time
import re
import json
from typing import Optional, Dict, Any, Union, List
from datetime import datetime
import uuid
from dataclasses import dataclass

from agents.base_agent import BaseAgent
from agents.task.conversation_resume import ConversationResumeMixin
from agents.task.session_registry import SessionRegistry
from agents.task.tool_defaults import default_session_tools
from core.exceptions import AgentError, SessionOwnershipError
from core.exceptions import InsufficientCreditsError
from core.optional_extras import missing_extra_hint


from core.exceptions import MessageQueueFullError

from agents.task.task_agent_support import (  # noqa: F401  (re-exported: existing importers + test seams)
    MAX_SESSIONS_PER_USER,
    SessionRequest,
    _DETACHED_TASKS,
    _SELF_WAKE_TASKS,
    _resolve_chat_runtime,
    _resolve_session_runtime,
    build_session_metadata,
    room_session_source,
    _spawn_detached,
    task_unavailable_message,
)
from agents.task.task_agent_chat import TaskAgentChatMixin
from agents.task.task_agent_delivery import TaskAgentDeliveryMixin
from agents.task.task_agent_lifecycle import TaskAgentLifecycleMixin
from agents.task.task_agent_control import TaskAgentControlMixin

logger = logging.getLogger(__name__)


class TaskAgent(TaskAgentChatMixin, TaskAgentDeliveryMixin, TaskAgentLifecycleMixin,
                TaskAgentControlMixin, ConversationResumeMixin, BaseAgent):
    """Minimal task automation wrapper.

    Core responsibilities:
    1. Verify task package availability
    2. Create/run sessions via task package
    3. Report status from SessionManager
    4. Route messages to active sessions
    """

    def __init__(self, name: str = "task_agent", config=None, container=None,
                 owns_workspace_gc: bool = True):
        """Initialize task agent wrapper.

        owns_workspace_gc: whether THIS process runs the daily workspace GC.
            The GC is destructive (rmtree), so exactly one process per box may
            own it. Read-only consumers that happen to build a TaskAgent — the
            webview console above all — must pass False. Default True keeps the
            agent/API processes behaving exactly as before.
        """
        # Get container if not provided - CRITICAL for tools
        if not container:
            from core.container import DependencyContainer
            container = DependencyContainer.get_instance()

        # Get config if not provided
        if not config:
            from core.config import BotConfig
            config = BotConfig()

        super().__init__(
            name=name,
            config=config,
            container=container
        )

        # Whether this process owns the destructive daily workspace GC (see __init__).
        self._owns_workspace_gc = bool(owns_workspace_gc)

        # Store capabilities
        self.capabilities = ["automation", "browser", "planning", "multi-agent"]
        self.description = "Task automation agent"

        # Core components from task package
        self.session_manager = None
        self.task_available = False
        self._initialized = False

        # Handles for the fire-and-forget periodic background loops so they can be
        # cancelled deterministically via aclose() — otherwise they are GC'd on
        # event-loop teardown ("Task was destroyed but it is pending" / "I/O
        # operation on closed file"), which is noise in prod and pollution in tests.
        self._bg_tasks: list = []

        # Active orchestrators, keyed by session_id, behind a registry interface.
        # Default: in-process dict (the reason UVICORN_WORKERS=1). Opt in to the
        # SQLite-backed registry (cross-process visibility for workers>1) with
        # SESSION_REGISTRY_BACKEND=sqlite — P6. Drop-in compatible interface.
        self._registry = self._build_registry(config)

        # Memory management with TTL and LRU eviction (from config)
        self.session_ttl_seconds = config.session_ttl_seconds
        self.max_sessions_in_memory = config.max_sessions_in_memory
        self.cleanup_interval = config.session_cleanup_interval
        # Shorter TTL for never-run 'created' sessions (keyed off created_at, not the
        # run-path activity clock) so they stop leaking the per-user session limit.
        self.created_session_ttl_seconds = getattr(
            config, 'created_session_ttl_seconds', 3600
        )
        self._session_last_activity = {}  # session_id → timestamp

        # Session execution locks for concurrency control
        self._session_execution_locks = {}  # session_id → asyncio.Lock
        self._recreate_locks = {}  # session_id → asyncio.Lock (serializes orchestrator recreation)
        # dead_session_id → asyncio.Lock (T1.4: serializes _try_conversation_resume so two
        # concurrent correspondent replies for the same dead session can't both mint a
        # replacement session / double-rebind the correspondent registry).
        self._resume_locks = {}

        # Compatibility attributes for API
        self.active_sessions = {}  # Will be populated from SessionManager
        self.user_sessions = {}    # Maps user_id to active session_id
        # S2 (chat consolidation): durable map (user_id, chat_id) -> session_id so
        # chat_once continues the same conversation across stateless HTTP calls
        # (keyed finer-grained than user_sessions, which keys by user only).
        self._chat_sessions = {}   # "chat:{user_id}:{chat_id}" -> session_id
        # Per-chat-key locks so concurrent chat_once() calls for the SAME
        # (user_id, chat_id) serialize: without this, two in-flight HTTP turns
        # could both create a session (one leaks) or read each other's reply off
        # the shared history. Lazily populated, keyed by _chat_key().
        self._chat_locks: Dict[str, "asyncio.Lock"] = {}

        # Initialize telemetry for continuous chat tracking
        try:
            from agents.task.telemetry.manager import TelemetryManager
            self.telemetry = TelemetryManager(
                session_id="task_agent",
                agent_id="continuous_chat"
            )
        except Exception as e:
            logger.debug(f"Failed to initialize telemetry: {e}")
            self.telemetry = None

    # --- Orchestrator registry accessors (public API) ---
    #
    # External callers (HTTP layer, tests) must use these instead of reaching
    # into the registry dict directly, so the storage can be swapped without
    # touching them. See agents/task/session_registry.py.

    @staticmethod
    def _build_registry(config):
        """Pick the registry backend (P6). Defaults to the in-process dict so
        production (UVICORN_WORKERS=1) is unchanged; SESSION_REGISTRY_BACKEND=sqlite
        opts into the cross-process SQLite registry."""
        import os
        backend = os.getenv("SESSION_REGISTRY_BACKEND", "memory").strip().lower()
        if backend == "sqlite":
            from agents.task.sqlite_session_registry import SqliteSessionRegistry
            from core.runtime_paths import data_dir_or_home
            data_dir = data_dir_or_home(getattr(config, "data_dir", None) if config else None)
            return SqliteSessionRegistry(os.path.join(data_dir, "session_registry.db"))
        return SessionRegistry()

    def get_orchestrator(self, session_id: str):
        """Return the active orchestrator for ``session_id`` or ``None``."""
        return self._registry.get(session_id)

    def route_session(self, session_id: str):
        """Cross-worker routing decision (P6) — a SessionRoute (LOCAL/REMOTE/MISSING).
        Lets the API distinguish 'owned by another worker' from a true 404 when
        SESSION_REGISTRY_BACKEND=sqlite and UVICORN_WORKERS>1. With the in-process
        registry it is always LOCAL or MISSING."""
        if hasattr(self._registry, "route"):
            return self._registry.route(session_id)
        # ultra-defensive fallback for a registry without route()
        from agents.task.session_route import SessionRoute, LOCAL, MISSING
        orch = self._registry.get(session_id)
        return SessionRoute(status=LOCAL, orchestrator=orch) if orch else SessionRoute(status=MISSING)

    def register_orchestrator(self, session_id: str, orchestrator) -> None:
        """Register (or replace) the orchestrator for ``session_id``."""
        self._registry.register(session_id, orchestrator)
        # SA-01: give the orchestrator a bound "re-run my loop" kick so a background
        # delegation that completes into an already-idle session can drain its result
        # instead of parking it forever. run_session refuses concurrent execution, so
        # the kick is a safe no-op when a loop is still active. Set here (the single
        # registration seam) so every registration path — create + recreate-from-disk —
        # gets it. Fail-open: a kick-wiring error must never block registration.
        try:
            uid = getattr(orchestrator, "user_id", None)

            async def _wake_kick():
                # 031: a delegation result re-entering an AUTONOMOUS session is
                # autonomous work — held while paused (the owner's own chat
                # session keeps its background results).
                try:
                    from agents.task.goals.autonomy_marker import is_autonomous
                    from core.autonomy_control import allows
                    if is_autonomous(session_id):
                        _dec = allows("self_wake")
                        if not _dec.allowed:
                            logger.warning("delegation wake kick HELD for %s (%s)",
                                           session_id, _dec.reason)
                            return
                except Exception:
                    logger.warning("wake kick pause probe failed — holding (fail closed)",
                                   exc_info=True)
                    return
                try:
                    await self.run_session(uid, session_id)
                except Exception:
                    pass

            orchestrator._wake_kick = _wake_kick
        except Exception:
            pass

    def remove_orchestrator(self, session_id: str):
        """Remove and return the orchestrator for ``session_id`` (``None`` if absent)."""
        return self._registry.remove(session_id)

    def heartbeat_session(self, session_id: str) -> None:
        """Refresh the registry's liveness clock for ``session_id`` (P6).

        No-op for the in-process registry (which has no heartbeat). For the SQLite
        backend this bumps ``last_seen_at`` so the periodic reaper spares live
        sessions. Fail-open — a heartbeat must never disrupt the run loop."""
        try:
            registry = self._registry
            if hasattr(registry, "heartbeat"):
                registry.heartbeat(session_id)
        except Exception:
            # Fail-open: heartbeat failure is non-critical, continue without it
            pass

    def active_orchestrators(self) -> List[Any]:
        """Snapshot list of the currently-registered orchestrators."""
        return self._registry.values()

    def active_session_count(self) -> int:
        """Number of orchestrators currently held in memory."""
        return self._registry.count()

    @property
    def _active_orchestrators(self) -> Dict[str, Any]:
        """Backward-compat view of the registry's underlying dict.

        Retained for existing tests/callers that mutate the dict directly.
        New code should use get_orchestrator/register_orchestrator/remove_orchestrator.
        """
        return self._registry._orchestrators

    @_active_orchestrators.setter
    def _active_orchestrators(self, value: Dict[str, Any]) -> None:
        self._registry._orchestrators = value

    async def _initialize(self) -> None:
        """Initialize and verify task package availability."""
        if self._initialized:
            return

        await super()._initialize()

        # Check task package and get SessionManager
        try:
            from agents.task.agent.session import get_session_manager

            self.session_manager = get_session_manager()
            self.task_available = True

            # Register in container for other services
            if self.container and not self.container.has_service('session_manager'):
                self.container.register_service('session_manager', self.session_manager)

            # P1b-0: install the Singular Chat outbound bus on the shared container
            # so create_session's binding + the P1a mirrors + cron/delivery's sink
            # have a router to reach. Flag-gated (default OFF -> no-op, byte-identical)
            # and fail-open (a bus error never breaks TaskAgent startup).
            if self.container:
                try:
                    from core.surfaces.bootstrap import install_surface_bus
                    # SB-04: do NOT pass a hardcoded "data/surfaces.db". This install
                    # runs first (during build_cli_container) and is idempotent, so a
                    # hardcoded path pinned the live outbound-allowlist/router DB to
                    # ./data/surfaces.db while `polyrob owner allow` writes
                    # <data_home>/surfaces.db (cwd/.polyrob or POLYROB_DATA_DIR) — a
                    # split-brain that left the P1 `message` allowlist un-configurable
                    # from the CLI admin surface. Let the config-aware default
                    # (<container.config.data_dir>/surfaces.db) resolve so both sides
                    # share one DB under the same data-home isolation.
                    install_surface_bus(self.container)
                except Exception as e:
                    logger.debug(f"surface bus install skipped: {e}")

            logger.info(f"✓ TaskAgent initialized with SessionManager")

            # Start background cleanup task for memory eviction
            self._bg_tasks.append(asyncio.create_task(self._periodic_cleanup()))
            logger.info(f"✓ Started periodic cleanup task (interval: {self.cleanup_interval}s, TTL: {self.session_ttl_seconds}s)")

            # Start workspace cleanup task — ONLY in the process that owns it.
            if getattr(self, "_owns_workspace_gc", True):
                self._bg_tasks.append(asyncio.create_task(self._periodic_workspace_cleanup()))
                logger.info("✓ Started periodic workspace cleanup task")
            else:
                logger.info("• Workspace cleanup task NOT started (this process does "
                            "not own the workspace GC)")

        except ImportError as e:
            self._task_unavailable_reason = str(e)
            logger.error(task_unavailable_message(self._task_unavailable_reason))
            self.task_available = False

        self._initialized = True

    def _assert_session_owner(self, session_id: Optional[str], user_id: str) -> None:
        """C4: refuse to reuse a session_id that already belongs to another user.

        Allows: no session_id (generated), a brand-new id, an id with no recorded
        owner (legacy), or the caller's own id. Raises SessionOwnershipError only on
        a real cross-user collision.
        """
        if not session_id:
            return
        existing = self.session_manager.get_session_info(session_id)
        if not existing:
            return
        owner = existing.get('user_id')
        if owner and user_id and owner != user_id:
            raise SessionOwnershipError(
                f"Session {session_id} belongs to another user"
            )

    async def create_session(
        self,
        user_id: str,
        request: Union[str, Dict[str, Any], SessionRequest],
        session_id: Optional[str] = None,
        skip_credit_check: bool = False,
        on_stream_chunk=None,
        **kwargs
    ) -> Dict[str, Any]:
        """Create a new task session with credit pre-validation.

        Args:
            user_id: User creating the session
            request: Task string, request dict, or SessionRequest object
            session_id: Optional session ID
            skip_credit_check: If True, skip credit validation (for admin/already-verified)
            on_stream_chunk: Optional streaming callback override. When provided,
                replaces the default webview stream callback. Used by CLI to print
                chunks to stdout. Signature: (session_id, agent_id, chunk, step) -> Awaitable[None]
            **kwargs: Additional parameters

        Returns:
            Session info dictionary
        """
        if not self._initialized:
            await self._initialize()

        if not self.task_available:
            raise AgentError(
                task_unavailable_message(getattr(self, "_task_unavailable_reason", None))
            )

        # Pre-validate credits before creating session (unless bypassed)
        if not skip_credit_check:
            await self._validate_user_credits(user_id)

        # B2: reap never-run 'created' sessions on-demand BEFORE the cap check.
        # The periodic cleanup task only ticks in a long-lived server process; a
        # one-shot `polyrob run` exits before it fires, so stale 'created' slots from
        # prior runs (loaded from disk on startup) would false-positive the cap.
        try:
            await self._cleanup_stale_created_sessions()
        except Exception:
            logger.debug("on-demand stale-session reap failed (non-fatal)", exc_info=True)

        # Check per-user session limit
        if not self._check_user_session_limit(user_id):
            raise AgentError(
                f"Session limit reached for user {user_id}. "
                f"Please complete existing sessions before creating new ones."
            )

        # Parse request into standard format
        if isinstance(request, str):
            session_request = SessionRequest(task=request)
        elif isinstance(request, SessionRequest):
            session_request = request
        else:
            # Dict format from API
            session_request = SessionRequest(
                task=request.get('task', ''),
                model=request.get('model'),
                provider=request.get('provider'),
                tools=request.get('tools') or default_session_tools(),
                max_steps=request.get('max_steps', 50),
                temperature=request.get('temperature', 0.0),
                use_vision=request.get('use_vision', True),
                session_config=request.get('session_config')
            )

        # SECURITY (C4): a client may supply a custom session_id (CLI/API/A2A). If
        # that id already belongs to a DIFFERENT user, refuse — otherwise we would
        # build a fresh orchestrator over theirs (register_orchestrator overwrite =
        # DoS) and stomp their task/model/tools metadata.
        self._assert_session_owner(session_id, user_id)

        # Generate session ID if needed
        if not session_id:
            session_id = str(uuid.uuid4())

        # Create session via SessionManager (043 A17: resolve the creator label)
        from agents.task.agent.session import resolve_creator
        actual_id = self.session_manager.create_session(
            session_id, user_id,
            creator=resolve_creator(kwargs.get("creator"), kwargs.get("session_source")))

        # Create orchestrator for this session
        from agents.task.agent.orchestrator import SessionOrchestrator

        # Streaming callback resolution: caller-provided > webview > None (headless).
        stream_callback = on_stream_chunk
        if stream_callback is None:
            try:
                from agents.task.utils_webview import make_webview_stream_callback
                stream_callback = make_webview_stream_callback()
            except Exception as e:
                logger.debug(f"Webview streaming unavailable, running headless: {e}")

        orchestrator = SessionOrchestrator(
            session_id=actual_id,
            user_id=user_id,
            container=self.container,  # Pass container for tools
            on_stream_chunk=stream_callback,
        )

        # 044 C2: stamp PUBLIC from the session SOURCE, before and independently
        # of the chat bind — `bind_chat_surface` returns early when
        # SINGULAR_CHAT_ENABLED is off (the default), so stamping only there built
        # a room session private-shaped. A room's safety flag must never depend on
        # a transport flag.
        orchestrator._public_session = room_session_source(kwargs.get("session_source"))

        # P1b-2: bind this session's orchestrator to the Singular Chat outbound bus
        # BEFORE initialize() — _register_stream_callback captures the router+key by
        # value, so binding after init would be a permanent no-op. Flag-gated +
        # fail-open: with the flag OFF or no chat_session_key (legacy callers pass
        # neither), this touches nothing and the legacy callback path is unchanged.
        try:
            from core.surfaces.binding import bind_chat_surface
            bind_chat_surface(
                orchestrator, self.container,
                session_source=kwargs.get("session_source"),
                chat_session_key=kwargs.get("chat_session_key"),
                session_id=actual_id, user_id=user_id,
                # 044 T20: a room SERVICE run binds but must not TAKE OVER the
                # room's durable chat<->session row (see bind_chat_surface).
                write_row=kwargs.get("bind_write_row", True),
            )
        except Exception as e:
            logger.debug(f"chat-surface bind skipped: {e}")

        # Initialize orchestrator with tools. A surface may pass an explicit
        # `tool_ids` override (e.g. the telegram owner-interactive toolset) WITHOUT
        # going through the request-dict path, so provider/model resolution stays
        # untouched. 044 T5 fix round 1: an explicit override is AUTHORITATIVE
        # including an EMPTY list (`or` treated `[]` as falsy and widened a
        # locked-down `GROUP_TURN_TOOLS=""` room back to `filesystem` read/write).
        # Only None — no kwarg at all — falls through to the request-dict toolset.
        tool_ids_override = kwargs.get("tool_ids")
        effective_tool_ids = (tool_ids_override if tool_ids_override is not None
                              else session_request.tools)
        await orchestrator.initialize(
            tool_ids=effective_tool_ids,
            tools_config=session_request.session_config.get('tools_config', {}) if session_request.session_config else {}
        )

        # Store orchestrator reference
        self.register_orchestrator(actual_id, orchestrator)

        # Store full session metadata with config for API compatibility. Shape +
        # the 044 C1 `effective_tools`/`public_session` keys: task_agent_support.
        self.session_manager.update_session_metadata(actual_id, build_session_metadata(
            session_request, effective_tool_ids=effective_tool_ids,
            public_session=orchestrator._public_session))

        # Save task to dedicated file for webview (uses SessionManager helper)
        try:
            self.session_manager._save_summary_file(actual_id, 'task.json', {
                "task": session_request.task,
                "model": session_request.model,
                "provider": session_request.provider,
                "created_at": datetime.now().isoformat()
            })
        except Exception as e:
            logger.warning(f"Failed to create task.json: {e}")

        # Update user_sessions mapping for API compatibility
        self.user_sessions[user_id] = actual_id

        logger.info(f"Created session {actual_id} with orchestrator for user {user_id}")

        return {
            'id': actual_id,
            'user_id': user_id,
            'task': session_request.task,
            'status': 'created',
            'model': session_request.model,
            'tools': session_request.tools,
            'config': {
                'model': session_request.model,
                'provider': session_request.provider,
                'tools': session_request.tools,
                'max_steps': session_request.max_steps,
                'temperature': session_request.temperature,
                'use_vision': session_request.use_vision
            }
        }


    async def run_session(
        self,
        user_id: str,
        session_id: Optional[str] = None
    ) -> str:
        """Run a task session with concurrency protection.

        This method encapsulates the entire execution flow:
        1. Acquire execution lock for the session (prevents race conditions)
        2. Get orchestrator (already initialized during create_session)
        3. Get LLM client
        4. Create agent
        5. Execute session

        All complexity is handled internally by the orchestrator.

        Args:
            user_id: User ID
            session_id: Session to run (or latest if None)

        Returns:
            Result message
        """
        if not self.task_available:
            return task_unavailable_message(getattr(self, "_task_unavailable_reason", None))

        # Get session
        if not session_id:
            sessions = self.session_manager.get_active_sessions(user_id)
            if not sessions:
                return "No active session found"
            session_id = sessions[-1]

        # CONCURRENCY PROTECTION (DUAL-LOCK PATTERN):
        # ==========================================
        # PRIMARY DEFENSE: Execution lock (prevents concurrent execution)
        # SECONDARY DEFENSE: Status transition check (validates state machine integrity)
        #
        # Get or create lock for this session
        if session_id not in self._session_execution_locks:
            self._session_execution_locks[session_id] = asyncio.Lock()

        # Acquire lock - only one execution at a time per session
        # This is the PRIMARY protection against race conditions
        async with self._session_execution_locks[session_id]:
            return await self._run_session_impl(user_id, session_id)

    async def _run_session_impl(
        self,
        user_id: str,
        session_id: str
    ) -> str:
        """Internal session execution implementation.

        This is the actual implementation separated for lock management.
        Should only be called from run_session() which holds the execution lock.

        Args:
            user_id: User ID
            session_id: Session ID to run

        Returns:
            Result message
        """
        session_info = self.session_manager.get_session_info(session_id)
        if not session_info or session_info.get('user_id') != user_id:
            return "Session not found or unauthorized"

        # Get current status
        current_status = session_info.get('status', 'unknown')

        # P2/P3 (2026-07-02): kill the "resume-to-check" model. A COMPLETED session
        # only re-runs when genuine queued input exists — every legitimate resume
        # path (STEER, continuation, self-wake, delegation-result) queues its
        # message BEFORE calling run_session. Without this gate, a no-input resume
        # burned an LLM call, concluded "No new user input", and appended another
        # wall of no-op done-turns to the persisted history (prod fa1212de).
        # No status churn, and an evicted orchestrator is NOT recreated just to
        # discover there is nothing to do.
        if current_status == 'completed' and not self._session_has_pending_input(session_id):
            logger.info(
                f"Session {session_id} is completed with no pending input — "
                f"skipping no-op resume"
            )
            return "No new input; session remains completed"

        # SECONDARY DEFENSE: Status transition validation
        # ================================================
        # This provides:
        # 1. Additional safety (defense-in-depth)
        # 2. Status state machine integrity
        # 3. Detection of logic errors that bypass the lock
        #
        # NOTE: The execution lock (line 312) is PRIMARY - this is SECONDARY verification
        # Include 'error' for backward compatibility with sessions that used old status
        if current_status in ['completed', 'suspended', 'failed', 'error']:
            # CONTINUOUS CHAT: completed/suspended/failed/error → resumed → running
            # Valid transitions require two steps for these statuses
            if not self.session_manager.try_transition_status(session_id, current_status, 'resumed'):
                logger.warning(f"Session {session_id} is already being resumed (concurrent call)")
                return "Session is already executing"
            # Update current status for next transition
            current_status = 'resumed'

        if current_status == 'resumed':
            # Try: resumed → running
            if not self.session_manager.try_transition_status(session_id, 'resumed', 'running'):
                logger.warning(f"Session {session_id} is already running (concurrent call)")
                return "Session is already executing"

            # Track session resume telemetry
            if self.telemetry:
                try:
                    # Get message queue size if available
                    queue_size = 0
                    orchestrator = self._registry.get(session_id)
                    if orchestrator and orchestrator.agents:
                        agent = next(iter(orchestrator.agents.values()))
                        if hasattr(agent, 'hitl_manager') and agent.hitl_manager:
                            queue_size = agent.hitl_manager.get_queue_size()

                    self.telemetry.capture_event(
                        event_type="session_resume",
                        data={
                            "session_id": session_id,
                            "previous_status": current_status,
                            "message_queue_size": queue_size,
                            "concurrent_sessions": self._registry.count()
                        }
                    )
                except Exception as e:
                    logger.debug(f"Failed to emit session resume telemetry: {e}")
        elif current_status in ('created', 'initializing'):
            # 'created' = fresh session; 'initializing' = the orchestrator just (re)created it
            # (orchestrator.__init__ sets status='initializing'). A warm STEER resume — e.g. the
            # owner sending a voice/text message to continue a suspended session — lands in
            # 'initializing', and WITHOUT this case it dead-ends at the else below ("Cannot run
            # session in status: initializing") so Rob never replies. Both are pre-run states →
            # transition to running. try_transition_status is a compare-and-swap, so it's safe.
            if not self.session_manager.try_transition_status(session_id, current_status, 'running'):
                logger.warning(f"Session {session_id} is already being processed")
                return "Session is already executing"
        elif current_status == 'running':
            # Already running!
            logger.warning(f"Session {session_id} is already running")
            return "Session is already executing"
        elif current_status == 'active':
            # Legacy status, try to transition
            if not self.session_manager.try_transition_status(session_id, 'active', 'running'):
                logger.warning(f"Session {session_id} is already being processed")
                return "Session is already executing"
        else:
            logger.warning(f"Cannot run session {session_id} in status: {current_status}")
            return f"Cannot run session in status: {current_status}"

        # EXCLUSIVE EXECUTION ACQUIRED
        # =============================
        # Protected by:
        # ✅ Execution lock (PRIMARY - held throughout this method)
        # ✅ Status transition (SECONDARY - validated state machine)
        #
        # Update activity time
        self._session_last_activity[session_id] = time.time()

        orchestrator = None
        final_status = None  # Track final status to pass to cleanup

        try:
            # Get orchestrator (should already exist from create_session); recreate
            # from disk under the shared per-session lock if it was evicted (a2).
            orchestrator = self._registry.get(session_id)
            if not orchestrator:
                logger.info(f"No orchestrator in memory for {session_id}, attempting recreation")
                orchestrator = await self._resolve_or_recreate(session_id, session_info)
                if not orchestrator:
                    raise RuntimeError(f"Failed to recreate orchestrator for session {session_id}")

            # Get request details
            request = session_info.get('request', {})
            task = request.get('task', '')

            # Check if agent already exists (for continuous task execution)
            # IMPORTANT: Use just the agent name, orchestrator will format the full ID
            agent_name = "executor"

            # Try to get existing agent using orchestrator's registry
            # The orchestrator stores agents as "executor_{session_id}"
            agent_id = f"{agent_name}_{session_id}"
            existing_agent = orchestrator.agents.get(agent_id)

            if existing_agent:
                # ✅ REUSE: Continuous task - reuse existing agent with all history
                logger.info(f"♻️  Reusing existing agent for continuous task execution: {agent_id}")
                agent = existing_agent

                # CRITICAL FIX: Reset agent state for continuation
                # Without this, _last_result still has is_done=True from previous run,
                # causing the agent to exit immediately without processing new messages
                if hasattr(agent, 'reset_for_continuation'):
                    agent.reset_for_continuation()
                else:
                    # Fallback for older agent versions
                    logger.warning(f"Agent {agent_id} missing reset_for_continuation, manual reset")
                    agent._last_result = None
                    agent._cancelled = False
                    if hasattr(agent, 'state'):
                        agent.state.stopped = False
                        agent.state.done = False

                # Log context for debugging
                if hasattr(agent, 'message_manager') and hasattr(agent.message_manager, 'history'):
                    messages_count = len(agent.message_manager.history.messages)
                    logger.info(f"Agent has {messages_count} messages in history")
            else:
                # ✅ CREATE: First task in session
                logger.info(f"🆕 Creating new agent for session {session_id}")

                # Get LLM - simplified approach
                llm = await self._get_llm_for_request(request)

                agent = await orchestrator.create_agent(
                    task=task,
                    llm=llm,
                    agent_name=agent_name,  # ← Use simple name, orchestrator adds session_id
                    use_vision=request.get('use_vision', True),
                    max_actions_per_step=10,
                    session_config=request.get('session_config')
                )

            # Execute - orchestrator handles everything
            results = await orchestrator.execute_session(
                agent_sequence=[agent.agent_id],
                max_steps_per_agent={
                    agent.agent_id: request.get('max_steps', 100)
                }
            )

            # Check results and set status ONCE
            agent_result = results.get(agent.agent_id, {})
            result_status = agent_result.get('status', 'error')
            
            if result_status == 'completed':
                final_status = 'completed'
                self.session_manager.update_session_status(session_id, final_status)
                return "Session completed successfully"
            elif result_status == 'stopped':
                # Agent was explicitly stopped/cancelled by user
                final_status = 'cancelled'
                self.session_manager.update_session_status(session_id, final_status)
                return "Session cancelled by user"
            else:
                # Error or unknown status
                final_status = 'failed'
                error_msg = agent_result.get('error', 'Unknown error')
                self.session_manager.update_session_status(session_id, final_status)
                return f"Session failed: {error_msg}"

        except asyncio.CancelledError:
            # P1 finalization: a hard wall-clock timeout (cron/goal budget) or an
            # explicit cancel raises CancelledError, which is a BaseException — the
            # `except Exception` below NEVER catches it, so final_status stayed None
            # and the finally defaulted it to 'completed', mislabeling a forcibly-
            # timed-out run as a clean success. Mark it cancelled, then re-raise to
            # honor the cancellation.
            final_status = 'cancelled'
            self._session_last_activity[session_id] = time.time()
            try:
                self.session_manager.update_session_status(session_id, final_status)
                self.session_manager.update_session_metadata(session_id, {
                    'error': 'session cancelled (timeout or explicit cancel)',
                    'error_time': datetime.now().isoformat(),
                    'error_type': 'CancelledError',
                })
            except Exception:
                pass
            raise

        except InsufficientCreditsError as e:
            # Special handling for billing errors - suspend rather than fail
            logger.warning(f"Session {session_id} suspended due to insufficient credits: {e}")

            final_status = 'suspended'
            self._session_last_activity[session_id] = time.time()

            # Update status and metadata with user-friendly message
            self.session_manager.update_session_status(session_id, final_status)
            self.session_manager.update_session_metadata(session_id, {
                'error': str(e),
                'error_time': datetime.now().isoformat(),
                'error_type': 'InsufficientCreditsError',
                'suspension_reason': 'insufficient_credits',
                'credits_required': e.required,
                'credits_available': e.available,
                'resume_instructions': 'Add credits at /api/payments/deposit to resume'
            })

            # Don't re-raise - return gracefully with suspension message
            return f"Session suspended: {str(e)}. Add credits to resume."

        except Exception as e:
            logger.error(f"Session {session_id} failed: {e}", exc_info=True)

            # Set final status to 'failed' (matches SessionStatus enum)
            final_status = 'failed'

            # Update activity time even on error
            self._session_last_activity[session_id] = time.time()

            # Update status and metadata
            self.session_manager.update_session_status(session_id, final_status)
            self.session_manager.update_session_metadata(session_id, {
                'error': str(e),
                'error_time': datetime.now().isoformat(),
                'error_type': type(e).__name__
            })

            # Re-raise for proper error handling
            raise
        finally:
            # NOTE: For continuous chat, we keep the orchestrator and agent alive
            # but release browser contexts to free resources (can be reacquired on next run)
            if orchestrator:
                try:
                    # STEP 1: Save message history BEFORE cleanup (BUG FIX #2)
                    # This ensures conversation context is preserved for continuous chat
                    try:
                        for agent in orchestrator.agents.values():
                            if hasattr(agent, 'message_manager') and agent.message_manager:
                                try:
                                    agent.message_manager.save_to_disk(
                                        session_id=session_id,
                                        user_id=orchestrator.user_id
                                    )
                                    logger.info(f"💾 Saved message history for agent {agent.agent_id}")
                                except Exception as e:
                                    logger.error(f"Failed to save message history for agent {agent.agent_id}: {e}")
                    except Exception as e:
                        logger.error(f"Failed to save message histories during completion: {e}")

                    # STEP 2: Release browser contexts but PRESERVE agents for continuous chat
                    # Pass the final_status to cleanup to prevent overwriting
                    await orchestrator.cleanup(
                        preserve_workspace=True,
                        preserve_agents=True,  # Keep agents in registry for continuous chat
                        status=final_status or 'completed',  # Use determined status or default
                        full_cleanup=False  # Keep for continuous chat
                    )

                    # Verify agent persistence
                    agent_ids = list(orchestrator.agents.keys())
                    if agent_ids:
                        logger.info(
                            f"Session {session_id} finished with status '{final_status}'. "
                            f"Orchestrator kept alive with {len(agent_ids)} agent(s) for continuous chat"
                        )
                    else:
                        logger.warning(
                            f"Session {session_id} finished but no agents in registry. "
                            f"Next message may trigger agent recreation."
                        )

                    # Update activity time after completion
                    self._session_last_activity[session_id] = time.time()

                except Exception as e:
                    logger.error(f"Error in cleanup for {session_id}: {e}")
    
    async def _get_llm_for_request(self, request: Dict[str, Any]):
        """Get LLM using canonical LLMManager.get_chat_model() method.

        Uses the existing LLMManager utility which handles:
        - Client lookup and initialization
        - Token limits from model_registry
        - Native chat model creation
        
        If the requested provider is unavailable, automatically falls back
        to the next available provider in the fallback hierarchy.

        Args:
            request: Request dictionary with provider, model, temperature

        Returns:
            Native BaseChatModel instance

        Raises:
            RuntimeError: If LLMManager not available or ALL providers failed
        """
        # Get LLM manager from container (single source of truth)
        llm_manager = self.container.get_service('llm')
        if not llm_manager:
            raise RuntimeError("LLMManager not available in container")

        provider = request.get('provider')
        model = request.get('model')
        if not provider or not model:
            provider, model = _resolve_session_runtime(provider, model)
        temperature = request.get('temperature', 0.0)

        # Try the requested provider first
        try:
            return await llm_manager.get_chat_model(
                provider=provider,
                model=model,
                temperature=temperature
            )
        except ValueError as e:
            # Provider not available - try fallback
            logger.warning(
                f"⚠️ Requested provider '{provider}' unavailable: {e}. "
                f"Attempting automatic fallback..."
            )
            
            # Use the fallback method to get next available provider
            fallback_llm = await llm_manager.get_fallback_chat_model(
                exclude_providers=[provider],
                original_model=model,
                temperature=temperature
            )
            
            if fallback_llm:
                fallback_model = getattr(fallback_llm, 'model_name', 'unknown')
                logger.info(f"✅ Using fallback LLM: {fallback_model}")
                return fallback_llm
            else:
                logger.error(f"❌ No fallback LLM available after {provider} failed")
                raise RuntimeError(
                    f"LLM provider '{provider}' is not available and no fallback providers "
                    f"could be initialized. Please check your API keys and configuration."
                )
        except Exception as e:
            logger.error(f"Failed to get LLM for request: {e}", exc_info=True)
            raise
