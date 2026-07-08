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
from agents.task.session_registry import SessionRegistry
from core.exceptions import AgentError, SessionOwnershipError
from core.exceptions import InsufficientCreditsError
from core.exceptions import MessageQueueFullError

logger = logging.getLogger(__name__)

# Session limits per user
# Defensive fallback ONLY for the getattr below; the runtime SSOT is the config
# field BotConfig.max_sessions_per_user (core/config.py, default 10). Keep this in
# sync with that default. (B3: was previously shadowed by a stray, unused
# constants.MAX_SESSIONS_PER_USER=100 — removed.)
MAX_SESSIONS_PER_USER = 10

#: Strong references to in-flight fire-and-forget self-wake ``run_session`` tasks
#: (AU-F3.1). asyncio only holds a WEAK reference to a task created via
#: ``asyncio.create_task`` -- without this, the task object can be garbage-collected
#: mid-run (a well-known asyncio footgun), silently dropping the self-wake dispatch.
#: Mirrors ``core/autonomy_runtime.py::_BACKGROUND_TASKS``. Self-cleans via
#: ``add_done_callback``.
_SELF_WAKE_TASKS: set = set()


@dataclass
class SessionRequest:
    """Model for session configuration."""
    task: str
    model: str = "gpt-5"
    provider: str = "openai"
    tools: List[str] = None
    max_steps: int = 50
    temperature: float = 0.0
    use_vision: bool = True
    session_config: Optional[Dict[str, Any]] = None
    
    def __post_init__(self):
        if self.tools is None:
            self.tools = ["browser", "filesystem", "task"]


def _resolve_chat_runtime(env=None):
    """Resolve (provider, model) for chat_once via the shared core resolver (Seam 2).

    Precedence: CHAT_PROVIDER/CHAT_MODEL or DEFAULT_PROVIDER/DEFAULT_MODEL pin >
    the historical openai/gpt-5 default (used only if an OpenAI key is present) >
    first keyed provider (canonical order) > openai/gpt-5 last resort. The model is
    always filled to match the resolved provider, so a first-keyed/anthropic-pinned
    provider never inherits SessionRequest's gpt-5 default.
    """
    import os as _os
    from core.runtime_config import resolve_runtime_config
    from modules.llm.llm_client_registry import get_default_model

    env = _os.environ if env is None else env
    pinned_provider = env.get("CHAT_PROVIDER") or env.get("DEFAULT_PROVIDER")
    pinned_model = env.get("CHAT_MODEL") or env.get("DEFAULT_MODEL")
    provider, model = resolve_runtime_config(
        None,
        None,
        env=env,
        pinned_provider=pinned_provider,
        pinned_model=pinned_model,
        cli_store_default=("openai", "gpt-5"),
        last_resort=("openai", "gpt-5"),
    )
    if not model:
        model = get_default_model(provider)
    return provider, model


class TaskAgent(BaseAgent):
    """Minimal task automation wrapper.

    Core responsibilities:
    1. Verify task package availability
    2. Create/run sessions via task package
    3. Report status from SessionManager
    4. Route messages to active sessions
    """

    def __init__(self, name: str = "task_agent", config=None, container=None):
        """Initialize task agent wrapper."""
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
            data_dir = getattr(config, "data_dir", "data") if config else "data"
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

            # Start workspace cleanup task
            self._bg_tasks.append(asyncio.create_task(self._periodic_workspace_cleanup()))
            logger.info("✓ Started periodic workspace cleanup task")

        except ImportError as e:
            logger.error(f"Task package not available: {e}")
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
            raise AgentError("Task package not available")

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
                model=request.get('model', 'gpt-5'),
                provider=request.get('provider', 'openai'),
                tools=request.get('tools', ['browser', 'filesystem', 'task']),
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

        # Create session via SessionManager
        actual_id = self.session_manager.create_session(session_id, user_id)

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
            )
        except Exception as e:
            logger.debug(f"chat-surface bind skipped: {e}")

        # Initialize orchestrator with tools. A surface may pass an explicit `tool_ids`
        # override (e.g. the telegram owner-interactive toolset) WITHOUT going through
        # the request-dict path, so provider/model resolution stays untouched.
        tool_ids_override = kwargs.get("tool_ids")
        await orchestrator.initialize(
            tool_ids=tool_ids_override or session_request.tools,
            tools_config=session_request.session_config.get('tools_config', {}) if session_request.session_config else {}
        )

        # Store orchestrator reference
        self.register_orchestrator(actual_id, orchestrator)

        # Store full session metadata with config for API compatibility
        self.session_manager.update_session_metadata(actual_id, {
            'task': session_request.task,
            'model': session_request.model,
            'tools': session_request.tools,
            'config': {
                'model': session_request.model,
                'provider': session_request.provider,
                'tools': session_request.tools,
                'max_steps': session_request.max_steps,
                'temperature': session_request.temperature,
                'use_vision': session_request.use_vision,
                'tools_config': session_request.session_config.get('tools_config', {}) if session_request.session_config else {}
            },
            'request': session_request.__dict__,
            'created_at': datetime.now().isoformat(),
            'status': 'created',
            'orchestrator_ready': True
        })

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

    def _rebind_recreated_chat(self, orchestrator, session_id: str, user_id: str) -> None:
        """Re-attach the outbound chat surface to a recreated orchestrator (#0).

        Recreation rebuilds the orchestrator without `_message_router`/`_chat_session_key`,
        so a resumed chat's replies would be silently dropped (the streaming + send_message
        seams read those by getattr→None). Reverse-look-up the chat binding and re-bind.
        MUST run BEFORE orchestrator.initialize()/create_agent — the stream callback
        captures the router by value. Fail-open; no-op for non-chat sessions / bus off."""
        try:
            if not self.container:
                return
            registry = self.container.get_service("session_chat_registry")
            if registry is None or not hasattr(registry, "resolve_by_session_id"):
                return
            row = registry.resolve_by_session_id(session_id)
            if not row:
                return  # not a chat-bound session
            from core.surfaces.binding import bind_chat_surface
            from core.surfaces.envelopes import SessionSource
            src = SessionSource(
                surface_id=row.get("surface_id"),
                chat_id=row.get("chat_id"),
                chat_type="dm",
            )
            bind_chat_surface(
                orchestrator, self.container,
                session_source=src,
                chat_session_key=row.get("session_key"),
                session_id=session_id,
                user_id=user_id,
            )
        except Exception as e:
            logger.debug(f"_rebind_recreated_chat failed for {session_id}: {e}")

    def touch_chat_binding(self, session_key: str) -> None:
        """Bump a chat binding's last-activity clock (idle boundary, a1). Fail-open;
        resolves the session_chat_registry from the container. No-op if the bus is off."""
        try:
            if not self.container:
                return
            registry = self.container.get_service("session_chat_registry")
            if registry is not None:
                registry.touch(session_key)
        except Exception as e:
            logger.debug(f"touch_chat_binding failed for {session_key}: {e}")

    async def _resolve_or_recreate(
        self, session_id: str, session_info: Dict[str, Any]
    ) -> Optional[Any]:
        """Return the resident orchestrator for ``session_id``, recreating it from disk
        under a per-session lock if it was evicted (a2-complete).

        ALL recreation paths route through here so two callers (e.g. a STEER message and
        a self-wake) racing on the same evicted session can't double-build the
        orchestrator and orphan one. A resident session returns immediately WITHOUT
        taking the lock, so this never blocks queuing into an already-running session.
        The lock is a dedicated ``_recreate_locks`` entry — not the execution lock."""
        orchestrator = self._registry.get(session_id)
        if orchestrator:
            return orchestrator
        if not hasattr(self, "_recreate_locks"):
            self._recreate_locks = {}
        lock = self._recreate_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            orchestrator = self._registry.get(session_id)  # re-check under lock
            if not orchestrator:
                orchestrator = await self._recreate_orchestrator(session_id, session_info)
        # If recreation failed the session never becomes resident, so _evict_session's
        # lock-pop (gated on a live orchestrator) will never fire — drop the lock here so
        # a stream of failed recreations can't leak one Lock per distinct session_id.
        if orchestrator is None and not lock.locked():
            self._recreate_locks.pop(session_id, None)
        return orchestrator

    def unbind_chat(self, session_key: str) -> None:
        """Drop a chat<->session binding (explicit /new). The next message from that
        chat then routes cold (fresh session) instead of STEERing into the old thread.
        Fail-open; no-op if the singular-chat bus is off (a4)."""
        try:
            if not self.container:
                return
            registry = self.container.get_service("session_chat_registry")
            if registry is not None:
                registry.delete(session_key)
        except Exception as e:
            logger.debug(f"unbind_chat failed for {session_key}: {e}")

    async def ensure_session_and_deliver(
        self,
        user_id: str,
        session_id: str,
        text: str,
        *,
        kind: str = "comment",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Resident-or-recreate the BOUND session, then queue a user message into it.

        Reuses the self-wake "resident-or-recreatable" rail: if the orchestrator was
        evicted, ``_recreate_orchestrator`` restores it from disk (``load_from_disk``
        rehydrates the full message history).

        Returns one of (a-MED2):
          - ``"delivered"`` — queued; the caller then runs the session to process it.
          - ``"busy"``      — the session is alive but its queue is full; the caller
                              should tell the user "still working", NOT mint a fresh one.
          - ``"gone"``      — truly gone (no on-disk metadata / wrong tenant / not
                              recreatable); the caller falls back to a fresh session.
        """
        try:
            session_info = self.session_manager.get_session_info(session_id)
            if not session_info:
                return "gone"
            # Tenant guard: never deliver into another user's session.
            owner = session_info.get("user_id")
            if user_id is not None and owner is not None and owner != user_id:
                logger.warning(
                    f"ensure_session_and_deliver: user mismatch for {session_id} "
                    f"(owner={owner}, caller={user_id}) — refusing"
                )
                return "gone"
            orchestrator = await self._resolve_or_recreate(session_id, session_info)
            if not orchestrator:
                return "gone"
            try:
                await orchestrator.submit_user_message(
                    agent_id=None, text=text, kind=kind, metadata=metadata,
                )
            except MessageQueueFullError:
                # The session is resident and processing — its queue is just saturated.
                # Surfacing "busy" keeps the user on the SAME thread instead of dropping
                # them into a fresh, amnesiac session.
                logger.info(
                    f"ensure_session_and_deliver: queue full for {session_id} — busy"
                )
                return "busy"
            return "delivered"
        except Exception as e:
            logger.error(
                f"ensure_session_and_deliver failed for {session_id}: {e}", exc_info=True
            )
            return "gone"

    def _session_has_pending_input(self, session_id: str) -> bool:
        """True if the session has genuine queued input waiting to be processed.

        Checks the orchestrator's pre-agent pending queue and every resident
        agent's HITL queue. An evicted orchestrator (not in the registry) has
        nothing queued in memory by definition — recreation paths queue their
        message first via ensure_session_and_deliver. Fail-open: on any error,
        report input present so legacy behaviour (run) is preserved.
        """
        orchestrator = self._registry.get(session_id)
        if not orchestrator:
            return False
        try:
            if getattr(orchestrator, '_pending_messages', None):
                return True
            for agent in (getattr(orchestrator, 'agents', None) or {}).values():
                hitl = getattr(agent, 'hitl_manager', None)
                if hitl is not None and hitl.get_queue_size() > 0:
                    return True
        except Exception as e:
            logger.debug(f"_session_has_pending_input({session_id}) failed: {e}")
            return True  # when in doubt, run (legacy behaviour)
        return False

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
            return "Task package not available"

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

        provider = request.get('provider', 'openai')
        model = request.get('model', 'gpt-5')
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

    async def get_session_status(
        self,
        user_id: str,
        session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get session status.

        Args:
            user_id: User ID
            session_id: Optional specific session

        Returns:
            Status dictionary
        """
        if not self.task_available:
            return {
                'found': False,
                'message': 'Task package not available'
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
            # Get latest session
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
        """Cancel a session.

        Args:
            user_id: User ID
            force: Force cancellation
            session_id: Session to cancel (or latest)

        Returns:
            True if cancelled
        """
        if not self.task_available:
            return False

        if not session_id:
            sessions = self.session_manager.get_active_sessions(user_id)
            if not sessions:
                return False
            session_id = sessions[-1]

        # Verify ownership
        session_info = self.session_manager.get_session_info(session_id)
        if not session_info or session_info.get('user_id') != user_id:
            return False

        # FIX: Actually stop the running session by calling orchestrator.cancel()
        # This sets the cancellation flag on all agents, causing them to stop at the next step
        orchestrator = self._registry.get(session_id)
        if orchestrator:
            try:
                orchestrator.cancel()
                logger.info(f"Called cancel() on orchestrator for session {session_id}")
            except Exception as e:
                logger.error(f"Error calling orchestrator.cancel(): {e}")

        # F3: persist the terminal status unconditionally (idempotent;
        # CREATED→CANCELLED is a valid transition per session.py). orchestrator
        # .cancel() only flips an in-memory flag — it does NOT touch the
        # session's persisted status — so without this an interactively-cancelled
        # session would leak as 'created' on disk. Flipping it here removes the
        # fragile two-hop persistence (the previous else-branch only updated
        # status when the session was NOT running).
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

    async def process_user_message(
        self,
        user_id: str,
        input_text: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Process user input - supports continuous chat.

        Flow:
        1. Check for existing session (even if completed)
        2. If exists: Queue message and resume that session
        3. If not: Create new session

        Args:
            user_id: User ID
            input_text: User's message
            metadata: Optional metadata

        Returns:
            Response message
        """
        if not self._initialized:
            await self._initialize()

        if not self.task_available:
            return "Task automation not available"

        text = input_text.strip()

        # Handle commands
        if text.startswith('/v2_status'):
            status = await self.get_session_status(user_id)
            if status['found']:
                return f"Session: {status['id']}\nStatus: {status['status']}\nTask: {status['task']}"
            return "No active session"

        if text.startswith('/v2_cancel'):
            if await self.cancel_session(user_id, force=True):
                return "Session cancelled"
            return "No session to cancel"

        if not text:
            return "Please provide a task description"

        try:
            # CONTINUOUS CHAT: Check for existing session first
            # User might be continuing a conversation from seconds/hours/months ago
            existing_session_id = None
            # Set when an existing session could not be resumed, so the new-session
            # path can tell the user instead of silently pretending to continue (B2).
            resume_failed_id = None

            # Try to get user's latest session (even if completed/suspended)
            if user_id in self.user_sessions:
                existing_session_id = self.user_sessions[user_id]
            else:
                # Check SessionManager for any sessions for this user
                all_sessions = self.session_manager._sessions
                user_session_ids = [sid for sid, info in all_sessions.items()
                                   if info.get('user_id') == user_id]
                if user_session_ids:
                    # Get most recent
                    existing_session_id = user_session_ids[-1]

            # If existing session found, queue message and resume
            if existing_session_id:
                session_info = self.session_manager.get_session_info(existing_session_id)
                if session_info:
                    logger.info(f"💬 Continuing session {existing_session_id} (status: {session_info.get('status')})")

                    # W1: a genuine user turn clears the self-wake re-entry budget so
                    # the agent can again earn forged continuations on its own work.
                    try:
                        from agents.task.constants import AutonomyConfig
                        if AutonomyConfig.self_wake_enabled():
                            from agents.task.agent.core.self_wake import get_reentry_budget
                            get_reentry_budget().reset(existing_session_id)
                    except Exception:
                        # Fail-open: reentry budget reset failure is non-critical
                        pass

                    # Get or recreate orchestrator (shared per-session lock, a2)
                    orchestrator = await self._resolve_or_recreate(existing_session_id, session_info)

                    if orchestrator:
                        # Queue message to agent via orchestrator
                        try:
                            await orchestrator.submit_user_message(
                                agent_id=None,  # Routes to first agent
                                text=text,
                                kind="continuation",
                                metadata=metadata or {}
                            )
                            logger.info(f"✅ Queued message to session {existing_session_id}")
                        except Exception as e:
                            logger.error(f"Failed to queue message: {e}")

                        # Resume session execution
                        asyncio.create_task(self.run_session(user_id, existing_session_id))

                        return f"💬 Message sent to existing session\nSession: {existing_session_id}"
                    else:
                        # Could not resume (orchestrator missing AND recreation failed).
                        # Fresh-start-with-notice: rather than silently spawn a duplicate,
                        # retire the un-recreatable session from in-memory tracking so it
                        # stops leaking the per-user limit and isn't re-selected next
                        # message — history stays on disk (delete_files=False, recoverable
                        # by id) — then fall through to create a new session and tell the
                        # user what happened.
                        logger.warning(
                            f"Could not resume session {existing_session_id} "
                            f"(orchestrator recreation failed); starting a new session"
                        )
                        resume_failed_id = existing_session_id
                        self.session_manager.cleanup_session(existing_session_id, delete_files=False)
                        self._session_last_activity.pop(existing_session_id, None)
                        self._session_execution_locks.pop(existing_session_id, None)

            # No existing session or failed to resume - create new session
            logger.info(f"🆕 Creating new session for user {user_id}")
            session = await self.create_session(user_id, text)

            # Run in background
            asyncio.create_task(self.run_session(user_id, session['id']))

            if resume_failed_id:
                return (
                    f"⚠️ Couldn't resume your previous session ({resume_failed_id[:8]}…), "
                    f"so I started a new one.\nSession: {session['id']}"
                )
            return f"🤖 Task started: {text[:100]}...\nSession: {session['id']}"

        except Exception as e:
            logger.error(f"Failed to process message: {e}")
            return f"Error: {str(e)}"

    # ------------------------------------------------------------------
    # S2 (chat consolidation): synchronous chat adapter
    # ------------------------------------------------------------------
    @staticmethod
    def _chat_key(user_id: str, chat_id: Optional[str]) -> str:
        """Durable conversation key. Falls back to user_id when no chat_id is
        given, mirroring the legacy ChatAgent's one-thread-per-user behavior."""
        return f"chat:{user_id}:{chat_id or user_id}"

    def _chat_tool_ids(self) -> List[str]:
        from agents.task.constants import CHAT_TOOL_IDS
        return list(CHAT_TOOL_IDS)

    async def _resolve_chat_persona(self) -> str:
        """Resolve the persona via the surface-shared resolver (T1-07): explicit
        POLYROB_PERSONA (template key or literal) > default character > "".
        Fail-open to "" (off-path byte-identical)."""
        try:
            from agents.personality.persona_resolver import resolve_persona
            return await resolve_persona(container=self.container)
        except Exception as e:
            logger.debug(f"chat persona resolve skipped: {e}")
            return ""

    @staticmethod
    def _looks_like_brain_state(text) -> bool:
        """True when *text* is agent brain-state telemetry, not a chat reply.

        Mirrors cli.ui.dialog.is_brain_state WITHOUT importing the CLI layer
        (agents must not depend on cli). Recognises the JSON shapes
        ({"current_state": {...}} and >=2 bare brain keys, tolerating a leading
        ```json fence) and the Memory:/Next: text echo. Live-caught on GLM, whose
        raw brain-JSON content can be the last AIMessage in history.
        """
        if not text:
            return False
        t = str(text).strip()
        # strip a single enclosing markdown code fence (DeepSeek JSON-fallback path)
        if t.startswith("```") or t.startswith("~~~"):
            m = re.match(r"^\s*(?:```|~~~)[^\n]*\n(.*?)\n?(?:```|~~~)\s*$", t, re.DOTALL)
            if m:
                t = m.group(1).strip()
        _brain_keys = {"current_state", "next_goal", "evaluation_previous_goal",
                       "page_summary", "memory", "reasoning", "macro_goal", "subgoal"}
        if t.startswith("{"):
            try:
                obj, _end = json.JSONDecoder().raw_decode(t)
            except (ValueError, TypeError):
                obj = None
            if isinstance(obj, dict):
                if isinstance(obj.get("current_state"), dict):
                    return True
                if sum(1 for k in obj if k in _brain_keys) >= 2:
                    return True
        # Memory:/Next: echo form (require both so prose with a stray header isn't caught)
        has_mem = re.search(r"(?im)^\s*memory\s*:", t)
        has_next = re.search(r"(?im)^\s*next(?:[_ ]?goal)?\s*:", t)
        return bool(has_mem and has_next)

    def _extract_chat_reply(self, session_id: str) -> str:
        """Return the agent's ACTUAL last assistant reply for a finished turn.

        Priority (corrected after a live GLM brain-JSON leak):
        1. The clean done() output — history.final_result() when is_done() — since
           done() adds its clean message to history BEFORE the atomic add of the
           model's raw content (which may be brain-state JSON), so "last AIMessage"
           alone would capture telemetry.
        2. The conversational/send path: the last NON-brain AIMessage (the real
           send_message text; ActionResult.extracted_content is only a placeholder).
        3. final_result() as a last resort.
        Brain-state telemetry is never returned; the generic run_session string never is.
        """
        try:
            orch = self._registry.get(session_id)
            if not orch or not getattr(orch, 'agents', None):
                return ""
            agent = next(iter(orch.agents.values()), None)
            if agent is None:
                return ""

            # 1) clean done() output
            try:
                hist = agent.state.history
                if hist.is_done():
                    fr = hist.final_result()
                    if fr and not self._looks_like_brain_state(fr):
                        return str(fr).strip()
            except Exception:
                # Fail-open: done() output extraction failure is non-critical
                pass

            # 2) last non-brain AIMessage (conversational/send path)
            mm = getattr(agent, 'message_manager', None)
            if mm is not None:
                try:
                    from modules.llm.messages import AIMessage
                    for managed in reversed(list(mm.history.messages)):
                        msg = getattr(managed, 'message', None)
                        content = getattr(msg, 'content', None)
                        if isinstance(msg, AIMessage) and isinstance(content, str) and content.strip():
                            text = content.strip()
                            prefix = "✅ Task Complete\n\n"
                            if text.startswith(prefix):
                                text = text[len(prefix):].strip()
                            if text and not self._looks_like_brain_state(text):
                                return text
                except Exception as e:
                    logger.debug(f"chat reply scan failed: {e}")

            # 3) last-resort fallback
            try:
                fr = agent.state.history.final_result()
                if fr and not self._looks_like_brain_state(fr):
                    return str(fr).strip()
            except Exception:
                # Fail-open: history scan failure is non-critical, try next fallback
                pass
        except Exception as e:
            logger.debug(f"_extract_chat_reply failed: {e}")
        return ""

    async def chat_once(
        self,
        user_id: str,
        text: str,
        chat_id: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """Run ONE synchronous chat turn on the unified task agent and return the
        assistant's reply text.

        This is the synchronous counterpart to the fire-and-forget
        process_user_message: it awaits run_session and returns the real reply
        (not the "Task started…" ack, not run_session's generic completion
        string). It reuses a durable session keyed by (user_id, chat_id) so a
        follow-up continues the same conversation, and runs tool-light.

        `provider`/`model` (B3) are an optional PER-REQUEST model override —
        e.g. from the OpenAI-compat `/v1/chat/completions` `body.model` field
        via `map_model()`. When set, they steer this turn's model (and, since
        chat sessions are durable, every subsequent turn of this (user_id,
        chat_id) session until overridden again). See `_chat_once_locked` for
        how the override is applied (live swap_model for a reused session;
        baked into the SessionRequest for a brand-new one).
        """
        if not self._initialized:
            await self._initialize()
        if not self.task_available:
            return "Task automation not available"

        text = (text or "").strip()
        if not text:
            return "Please provide a message"

        key = self._chat_key(user_id, chat_id)
        locks = getattr(self, "_chat_locks", None)
        if locks is None:
            locks = {}
            self._chat_locks = locks
        lock = locks.get(key) or locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                result = await self._chat_once_locked(
                    user_id, text, key, provider=provider, model=model,
                )
        finally:
            # Evict the lock only if this turn left no active session mapping
            # for `key` (e.g. session creation raised or failed) -- a
            # successful turn always re-populates self._chat_sessions[key], so
            # this is a no-op on the common path. Safe without extra locking:
            # no `await` sits between the membership check and the pop, so
            # asyncio cannot interleave another task's get-or-create for the
            # same key in between (task switches only happen at await points).
            if key not in self._chat_sessions:
                locks.pop(key, None)
        return result

    async def _maybe_swap_chat_model(
        self, orch, provider: Optional[str], model: str,
    ) -> None:
        """Apply a B3 per-request model override to a REUSED session's live
        agent, idempotently.

        Uses the same accessor `_extract_chat_reply` uses (`orch.agents` is a
        dict; chat sessions run a single agent, so the first value is it). A
        no-op when no live agent exists yet (e.g. mid-creation) or when the
        requested (provider, model) already matches the agent's current
        SSOT (`model_name`/`llm_provider`) — a falsy `provider` never counts
        as "different", it means swap_model should auto-detect. This guard
        is what stops an unchanged `body.model` from rebuilding the LLM on
        every single request. A failed swap is logged and the turn proceeds
        on the agent's current (unchanged) model.
        """
        agent = next(iter(orch.agents.values()), None) if getattr(orch, "agents", None) else None
        if agent is None or not hasattr(agent, "swap_model"):
            return
        same_model = getattr(agent, "model_name", None) == model
        # A never-swapped agent never has `.llm_provider` set to the request's
        # provider label the way swap_model would — it may be None (older builds)
        # or only the Agent-level mirror. Fall back to the MessageManager-backed
        # `provider_name` SSOT so an unchanged model doesn't rebuild the LLM on
        # every request (B3 idempotence). A falsy request provider never counts
        # as "different" (auto-detect).
        current_provider = (
            getattr(agent, "llm_provider", None) or getattr(agent, "provider_name", None)
        )
        same_provider = (not provider) or current_provider == provider
        if same_model and same_provider:
            return
        # A failed swap must warn + proceed on the current model — an exception
        # from swap_model (e.g. a provider build blowing up) must never 500 the
        # /v1 request (the plan's contract: failed swap = warn + proceed).
        try:
            res = await agent.swap_model(provider, model)
        except Exception as e:
            logger.warning(
                f"per-request model swap raised: {e} — "
                f"turn continues on {getattr(agent, 'model_name', '?')}"
            )
            return
        if not res.get("ok"):
            logger.warning(
                f"per-request model swap failed: {res.get('error')} — "
                f"turn continues on {getattr(agent, 'model_name', '?')}"
            )

    async def _chat_once_locked(
        self,
        user_id: str,
        text: str,
        key: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """The body of chat_once, run under the per-chat-key lock (see chat_once)."""
        session_id = self._chat_sessions.get(key)

        # Validate a remembered session still exists; else drop and recreate.
        if session_id and not self.session_manager.get_session_info(session_id):
            self._chat_sessions.pop(key, None)
            session_id = None

        # C1: expand @file/@folder/@diff/@url references (opt-in, fail-soft).
        # Use the session workspace when available; fall back to CWD for new sessions.
        try:
            from agents.task.constants import AutonomyConfig
            if AutonomyConfig.context_references_enabled():
                from agents.task.agent.messages.context_references import (
                    preprocess_context_references,
                )
                from agents.task.path import pm
                import os as _os
                _root = str(pm().get_workspace_dir(session_id, user_id)) if session_id else _os.getcwd()
                text = preprocess_context_references(
                    text, root=_root, confine_to_root=True, allow_filesystem=True
                )
        except Exception:
            pass  # fail-soft: leave text unchanged

        persona = await self._resolve_chat_persona()

        if session_id:
            # Reuse: queue a continuation and resume (await, unlike the
            # background process_user_message path).
            orch = self._registry.get(session_id)
            if not orch:
                info = self.session_manager.get_session_info(session_id)
                orch = await self._resolve_or_recreate(session_id, info) if info else None
            if orch:
                if persona:
                    orch._persona_block = persona
                if model:
                    await self._maybe_swap_chat_model(orch, provider, model)
                try:
                    await orch.submit_user_message(
                        agent_id=None, text=text, kind="continuation",
                    )
                except Exception as e:
                    logger.warning(f"chat_once: failed to queue continuation: {e}")
            else:
                # Un-recreatable — drop the mapping and fall through to create.
                self._chat_sessions.pop(key, None)
                session_id = None

        if not session_id:
            from agents.task.constants import CHAT_MAX_STEPS
            # Provider/model via the one shared resolver (Seam 2): CHAT_/DEFAULT_ env
            # pins win; else the historical openai/gpt-5 default if OpenAI is keyed;
            # else the first keyed provider (fixes only-one-key chat). Model always
            # matches the resolved provider.
            _provider, _model = _resolve_chat_runtime()
            # B3: a per-request model override for a BRAND-NEW session has no
            # live agent yet to swap_model() (that's created later, inside
            # run_session) — bake it into the SessionRequest instead, the
            # same knob CHAT_PROVIDER/CHAT_MODEL already use above.
            if model:
                _model = model
                _provider = provider or _provider
            req = SessionRequest(
                task=text,
                tools=self._chat_tool_ids(),
                max_steps=CHAT_MAX_STEPS,
                use_vision=False,
                provider=_provider,
                model=_model,
            )
            from agents.task.constants import CHAT_SKIP_CREDIT_CHECK
            info = await self.create_session(
                user_id, req, chat_session_key=key,
                skip_credit_check=CHAT_SKIP_CREDIT_CHECK,
            )
            session_id = info['id']
            self._chat_sessions[key] = session_id
            orch = self._registry.get(session_id)
            if orch and persona:
                orch._persona_block = persona

        await self.run_session(user_id, session_id)
        return self._extract_chat_reply(session_id) or ""

    async def deliver_self_wake(
        self,
        session_id: str,
        user_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Forge a fresh internal turn for ``session_id`` (W1 self-wake rail).

        The public producer seam for non-user re-entry: a finished goal run, cron job,
        or future background producer calls this to re-enter a session that has gone
        idle (its loop ended on done()/conversational-exit). Reuses UP-12's proven
        ingress — ``submit_user_message`` → HITL queue → run-loop drain — rather than a
        parallel queue, then kicks the loop via ``run_session`` (the forge-fresh-run
        UP-12 lacks: it only re-enters an already-running loop).

        Safety (all enforced here):
          * gated ``SELF_WAKE_ENABLED`` (default OFF → returns False, no-op);
          * ``ReentryBudget`` depth + idle-backoff cap (prevents ping-pong storms);
          * resident-or-recreatable only (in-process; never claims a remote session —
            an unrecoverable session is dropped + audit-logged, the honest behaviour);
          * forged text framed as UP-06 untrusted DATA + tagged ``kind="self_wake"``.

        Returns True iff a forged turn was dispatched.
        """
        from agents.task.constants import AutonomyConfig
        if not AutonomyConfig.self_wake_enabled():
            return False

        from agents.task.agent.core.self_wake import (
            get_reentry_budget, format_self_wake, SELF_WAKE_KIND,
        )
        def _wake_ev(outcome: str, reason: Optional[str] = None) -> None:
            # Self-wake was entirely invisible (no log/telemetry): fired vs
            # skipped vs dropped collapsed to a silent return. Emit each outcome
            # to the durable event log (fail-open).
            try:
                from agents.task.telemetry.event_log import get_event_log, event_log_enabled
                if event_log_enabled():
                    get_event_log().record(
                        "self_wake", user_id=user_id, session_id=session_id,
                        source="self_wake", outcome=outcome, reason=reason)
            except Exception:
                pass

        budget = get_reentry_budget()
        # Atomically consume the slot up-front (see ReentryBudget.try_consume) so two
        # concurrent producers can't both pass the check and exceed the cap. A slot
        # consumed on a subsequently-failed dispatch errs toward FEWER wakes — the
        # safe direction for a runaway guard.
        if not budget.try_consume(session_id):
            logger.info(f"🛌 self-wake budget exhausted for session {session_id} — dropping")
            _wake_ev("skipped", "budget_exhausted")
            return False

        try:
            session_info = self.session_manager.get_session_info(session_id)
            if not session_info:
                logger.warning(f"self-wake: session {session_id} not found — dropping (audit)")
                _wake_ev("dropped", "session_not_found")
                return False

            orchestrator = await self._resolve_or_recreate(session_id, session_info)
            if not orchestrator:
                logger.warning(
                    f"self-wake: session {session_id} not resident and not recreatable "
                    f"— dropping (cross-worker/expired reality, audit)"
                )
                _wake_ev("dropped", "not_resident")
                return False

            meta = dict(metadata or {})
            meta.setdefault("source", "self_wake")
            await orchestrator.submit_user_message(
                agent_id=None,
                text=format_self_wake(text),
                kind=SELF_WAKE_KIND,
                metadata=meta,
            )
            # NOTE: the budget slot was already consumed atomically by try_consume()
            # above — do not record() again here (that would double-count).
            # AU-F3.1: hold a strong ref (asyncio.create_task alone only weakly
            # references the task — see _SELF_WAKE_TASKS docstring above).
            t = asyncio.create_task(self.run_session(user_id, session_id))
            _SELF_WAKE_TASKS.add(t)
            t.add_done_callback(_SELF_WAKE_TASKS.discard)
            logger.info(f"🛎️ self-wake dispatched to session {session_id} "
                        f"(remaining budget {budget.remaining(session_id)})")
            _wake_ev("fired", None)
            return True
        except Exception as e:
            logger.error(f"self-wake delivery failed for {session_id}: {e}", exc_info=True)
            _wake_ev("error", str(e)[:200])
            return False

    async def deliver_correspondent_data(
        self,
        session_id: str,
        source: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """WS-A: deliver a third-party correspondent reply as DATA into ``session_id``.

        The reply is injected as a CORRESPONDENT-origin control message (untrusted-
        wrapped) into the session that INITIATED contact — never a steering user turn,
        never a new session. Resident-or-recreatable only: an unrecoverable session is
        dropped + audit-logged (a third party can't resurrect a dead session). The
        owner ``user_id`` for the re-run comes from the session's OWN metadata, never
        the correspondent's identity (tenant safety). Gated ``CORRESPONDENT_ACCESS_ENABLED``
        (default OFF → no-op). Returns True iff the data was delivered.
        """
        from agents.task.surface_config import SurfaceConfig
        if not SurfaceConfig.correspondent_access_enabled():
            return False
        try:
            session_info = self.session_manager.get_session_info(session_id)
            if not session_info:
                logger.warning(
                    f"correspondent delivery: session {session_id} not found — dropping (audit)")
                return False
            orchestrator = await self._resolve_or_recreate(session_id, session_info)
            if not orchestrator:
                logger.warning(
                    f"correspondent delivery: session {session_id} not resident/recreatable "
                    f"— dropping (audit)")
                return False
            delivered = orchestrator.inject_correspondent_message(text, source, metadata)
            if not delivered:
                logger.warning(
                    f"correspondent delivery: no resident agent for {session_id} — dropping (audit)")
                return False
            owner_user_id = (session_info.get("user_id")
                             if isinstance(session_info, dict)
                             else getattr(session_info, "user_id", None))
            asyncio.create_task(self.run_session(owner_user_id, session_id))
            logger.info(f"📨 correspondent DATA from {source} delivered to session {session_id}")
            return True
        except Exception as e:
            logger.error(f"correspondent delivery failed for {session_id}: {e}", exc_info=True)
            return False

    async def _recreate_orchestrator(
        self,
        session_id: str,
        session_info: Dict[str, Any]
    ) -> Optional[Any]:
        """Recreate orchestrator from saved session state (for suspended sessions).

        Args:
            session_id: Session identifier
            session_info: Session metadata from SessionManager

        Returns:
            Recreated orchestrator or None if recreation failed
        """
        try:
            from agents.task.agent.orchestrator import SessionOrchestrator

            # Get session request metadata - check multiple sources
            request = session_info.get('request', {})
            config = session_info.get('config', {})

            # Log what we found for debugging
            if not request and not config:
                logger.error(f"No request or config metadata found for session {session_id}")
                logger.debug(f"Session info keys: {list(session_info.keys())}")
                return None

            user_id = session_info.get('user_id')
            if not user_id:
                logger.error(f"No user_id found for session {session_id}")
                return None

            # Webview streaming is optional in core mode (see create_session).
            stream_callback = None
            try:
                from agents.task.utils_webview import make_webview_stream_callback
                stream_callback = make_webview_stream_callback()
            except Exception as e:
                logger.debug(f"Webview streaming unavailable on recreate: {e}")

            # Recreate orchestrator
            orchestrator = SessionOrchestrator(
                session_id=session_id,
                user_id=user_id,
                container=self.container,
                on_stream_chunk=stream_callback,
            )

            # #0 mute-on-resume: re-attach the outbound chat surface BEFORE initialize()
            # so a resumed chat's replies route back out (recreation otherwise leaves
            # _message_router/_chat_session_key unset → the agent answers into the void).
            self._rebind_recreated_chat(orchestrator, session_id, user_id)

            # Initialize with same tools - check multiple sources for tools
            # Priority: request.tools > config.tools > session_info.tools > defaults
            tool_ids = (
                request.get('tools') or
                config.get('tools') or
                session_info.get('tools') or
                ['browser', 'filesystem', 'task']
            )

            # Get tools_config from multiple sources
            tools_config = (
                (request.get('session_config') or {}).get('tools_config') or
                config.get('tools_config') or
                {}
            )

            logger.info(f"Recreating orchestrator with tools: {tool_ids}")

            await orchestrator.initialize(
                tool_ids=tool_ids,
                tools_config=tools_config
            )

            # Recreate agent - use both request and config for fallbacks
            # Merge request and config for _get_llm_for_request
            llm_request = {**config, **request}  # request takes priority
            llm = await self._get_llm_for_request(llm_request)
            agent = await orchestrator.create_agent(
                task=request.get('task') or session_info.get('task', ''),
                llm=llm,
                agent_name="executor",
                use_vision=request.get('use_vision', config.get('use_vision', True)),
                max_actions_per_step=10,
                session_config=request.get('session_config') or config
            )

            # RESTORE MESSAGE HISTORY (FIX #4)
            if hasattr(agent, 'message_manager') and agent.message_manager:
                try:
                    loaded = agent.message_manager.load_from_disk(
                        session_id=session_id,
                        user_id=user_id
                    )
                    if loaded:
                        logger.info(f"📂 Restored message history for session {session_id}")
                    else:
                        logger.info(f"No message history to restore for session {session_id}")
                except Exception as e:
                    logger.error(f"Failed to restore message history: {e}")

            # RESTORE HITL STATE (queued messages from before eviction)
            if hasattr(agent, 'hitl_manager') and agent.hitl_manager:
                try:
                    from agents.task.path import pm
                    import json
                    hitl_path = pm().create_file_path(
                        session_id=session_id,
                        subdir_name="memory",
                        filename="hitl_state.json",
                        user_id=user_id
                    )
                    if hitl_path.exists():
                        with open(hitl_path, 'r') as f:
                            hitl_state = json.load(f)
                        agent.hitl_manager.restore_state(hitl_state)
                        queued_count = len(hitl_state.get('queued_messages', []))
                        logger.info(f"📂 Restored HITL state for session {session_id} ({queued_count} queued messages)")
                        
                        # Remove the file after restoration to avoid re-restoring stale state
                        hitl_path.unlink()
                except Exception as e:
                    logger.warning(f"Could not restore HITL state: {e}")

            # PRE-LOAD hierarchical memory to validate session can be fully restored
            # Note: The actual loading happens in Agent.run(), but we validate here
            if hasattr(agent, 'task_context_manager') and agent.task_context_manager:
                try:
                    # Try to load the H-MEM session to verify it exists
                    memory = agent.task_context_manager.load_session(session_id, user_id)
                    if memory:
                        logger.info(f"📂 Validated hierarchical memory exists for session {session_id}")
                    else:
                        logger.info(f"No existing H-MEM for session {session_id} (will be created on run)")
                except Exception as e:
                    logger.warning(f"Could not pre-load H-MEM for session {session_id}: {e}")

            # Store orchestrator
            self.register_orchestrator(session_id, orchestrator)
            self._session_last_activity[session_id] = time.time()

            logger.info(f"Successfully recreated orchestrator for suspended session {session_id}")
            return orchestrator

        except Exception as e:
            logger.error(f"Failed to recreate orchestrator for {session_id}: {e}", exc_info=True)
            return None

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
        """Periodically clean up old session workspaces."""
        while True:
            try:
                # Run cleanup daily
                await asyncio.sleep(86400)  # 24 hours

                if self.session_manager:
                    cleaned = self.session_manager.cleanup_old_workspaces(max_age_days=7)
                    logger.info(f"Workspace cleanup: removed {cleaned} old workspaces")

            except Exception as e:
                logger.error(f"Error in workspace cleanup: {e}")

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
        logger.info("TaskAgent cleanup complete")

    # Required abstract methods from BaseAgent

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

    # Additional convenience methods that delegate to SessionManager

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

    def get_all_sessions(self) -> Dict[str, Any]:
        """Get all sessions (admin function)."""
        if not self.task_available or not self.session_manager:
            return {}
        # Access internal tracking for admin purposes
        return self.session_manager._sessions.copy()
    
    # Methods required by API
    
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
            asyncio.create_task(orchestrator.cleanup())

        # NOTE: Don't call cleanup_session - keep completed sessions in memory
        # for continuous chat feature. They remain accessible via get_session_by_id()
        # but the orchestrator is cleaned up to free resources.
    
    # API compatibility methods
    
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