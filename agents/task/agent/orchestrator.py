from __future__ import annotations
import asyncio
import logging
import functools
from typing import Awaitable, Callable, Dict, List, Optional, Any
from pathlib import Path
import os
from datetime import datetime
import time
import json

# Streaming callback contract: invoked once per LLM output chunk.
# Kept generic so core has no transport dependency (httpx/websocket/socketio
# live in the server layer that supplies the callback).
StreamChunkCallback = Callable[[str, str, str, int], Awaitable[None]]
"""async (session_id, agent_id, chunk, step) -> None"""

# Import centralized path manager
from agents.task.path import pm

# Import LLM error types for fallback handling
from core.exceptions import (
    LLMError,
    LLMProviderExhaustedError,
    LLMPermanentError
)

# Import session manager
from agents.task.agent.session import SessionManager
from agents.task.agent.service import Agent
from tools.browser.browser import Browser
from tools.browser.context import BrowserContext
from tools.browser.browser_manager import BrowserManager
from tools.controller.service import Controller
# Import ActionResult for loop detection and intervention
from agents.task.agent.views import ActionResult
# ToolManager functionality is now integrated into Controller

# Concern-group mixins (pure code-motion from this module)
from agents.task.session.workspace import WorkspaceMixin
from agents.task.session.feed import FeedMixin
from agents.task.session.multi_agent import MultiAgentMixin
from agents.task.session.browser_pool import BrowserPoolMixin
from agents.task.session.hitl_ingress import HITLIngressMixin
from agents.task.session.cleanup import SessionCleanupMixin
from agents.task.session.execution import SessionExecutionMixin
from agents.task.session.hooks import SessionHooksMixin

# Default tools that are always loaded for every session
DEFAULT_TOOLS = ['filesystem', 'task']

# SA-01: strong refs for fire-and-forget async-delegation wake kicks. asyncio only
# weakly references a bare create_task result, so without this the kick task could be
# GC'd mid-flight before it re-runs the idle session's loop.
_ASYNC_DELEGATION_KICKS: set = set()

class SessionOrchestrator(WorkspaceMixin, FeedMixin, MultiAgentMixin, BrowserPoolMixin, HITLIngressMixin, SessionCleanupMixin, SessionExecutionMixin, SessionHooksMixin):
    """Orchestrates sessions with multiple agents.
    
    The SessionOrchestrator is responsible for coordinating agents, services and
    browser contexts throughout the session lifecycle. It has the following responsibilities:
    
    1. Agent Management: Create, register and coordinate execution of agents
    2. Service Management: Register services and ensure they're available to agents
    3. Browser Management: Initialize and share browser instances across agents
    4. Session Tracking: Track session status and completion
    
    The orchestrator works with the Controller component, which manages the action registry
    and handles action execution. This separation of concerns allows:
    - Orchestrator: Focus on high-level session management, agent coordination, and service lifecycle
    - Controller: Focus on action registration and execution, serving as the "action bridge" for agents
    
    Services registered with the orchestrator are made available to the controller,
    which then registers appropriate actions based on the service capabilities.
    """

    # Class-level sentinels satisfy SessionContext Protocol structural check via getattr().
    # Instance assignments in __init__ shadow these immediately.
    session_id: str = ""  # type: ignore[assignment]
    user_id: Optional[str] = ""  # type: ignore[assignment]

    def __init__(self,
                session_id: str,
                user_id: Optional[str] = None,
                config: Optional[Any] = None,
                browser_manager: Optional[BrowserManager] = None,
                container: Optional[Any] = None,
                on_stream_chunk: Optional[StreamChunkCallback] = None):
        """Initialize session orchestrator.

        Args:
            session_id: The unique identifier for this session
            user_id: Optional user identifier for multi-user installations
            config: Optional session configuration with orchestrator settings
            browser_manager: Optional BrowserManager instance for centralized browser management
            container: Optional DependencyContainer for tool loading
            on_stream_chunk: Optional async callback that receives LLM output
                chunks. Signature: (session_id, agent_id, chunk, step). When
                None (core mode), streaming is disabled. The server layer
                supplies an httpx/socketio implementation to bridge chunks to
                the webview frontend.
        """
        # Store container for tool loading
        self.container = container
        # Optional streaming sink (no transport coupling in core).
        self._on_stream_chunk: Optional[StreamChunkCallback] = on_stream_chunk
        # Get session manager instance
        from agents.task.path import get_safe_singleton
        self.session_manager = get_safe_singleton(SessionManager)()

        # Clean the session ID
        self.session_id = pm().clean_session_id(session_id)

        # Validate session_id is provided
        if not self.session_id:
            raise ValueError("session_id is required for SessionOrchestrator")

        # Store user_id
        self.user_id = user_id

        # Store configuration and set up orchestrator behavior
        self.config = config
        self.orchestrator_config = None

        # Initialize TelemetryManager for orchestrator-level telemetry
        try:
            from agents.task.telemetry.manager import TelemetryManager
            self.telemetry_manager = TelemetryManager(
                session_id=self.session_id,
                agent_id=f"orchestrator_{self.session_id}"
            )
        except Exception as e:
            # Create dummy if initialization fails - include ALL telemetry methods
            import logging
            logger = logging.getLogger(f"task.orchestrator[{session_id}]")
            logger.error(f"Failed to initialize TelemetryManager: {e}", exc_info=True)

            class DummyTelemetryManager:
                """Dummy telemetry manager that no-ops all telemetry calls."""
                def capture_agent_registration(self, *args, **kwargs): pass
                def capture_multi_agent_relationship(self, *args, **kwargs): pass
                def capture_tool_execution(self, *args, **kwargs): pass
                def capture_error(self, *args, **kwargs): pass
                def capture_llm_request(self, *args, **kwargs): pass
                def capture_step(self, *args, **kwargs): pass
                def flush_buffers(self, *args, **kwargs): pass
            self.telemetry_manager = DummyTelemetryManager()

        # Initialize unified usage tracker (replaces separate usage_meter + telemetry calls)
        # This provides single source of truth for token tracking and cost calculation
        try:
            from modules.credits.usage_tracker import LLMUsageTracker

            # Get required services from container
            if container and hasattr(container, 'get_service'):
                db = container.get_service('database_manager')
                balance_manager = container.get_service('balance_manager')

                if db and balance_manager:
                    self.usage_tracker = LLMUsageTracker(
                        db=db,
                        balance_manager=balance_manager,
                        telemetry_manager=self.telemetry_manager
                    )
                    import logging
                    logger = logging.getLogger(f"task.orchestrator[{session_id}]")
                    logger.info("✓ Unified usage tracker initialized")
                else:
                    self.usage_tracker = None
                    import logging
                    logger = logging.getLogger(f"task.orchestrator[{session_id}]")
                    logger.debug("Usage tracker not initialized - missing database or balance_manager")
            else:
                self.usage_tracker = None
        except Exception as e:
            import logging
            logger = logging.getLogger(f"task.orchestrator[{session_id}]")
            logger.error(f"Failed to initialize usage tracker: {e}", exc_info=True)
            self.usage_tracker = None

        if config and hasattr(config, 'orchestrator'):
            self.orchestrator_config = config.orchestrator
        else:
            # Create default orchestrator config based on current mode
            from agents.task.config import OrchestratorConfigModel, TaskMode
            mode = TaskMode.get_mode()
            self.orchestrator_config = OrchestratorConfigModel.from_mode(mode)

        # Create session
        try:
            self.session_id = self.session_manager.create_session(self.session_id, user_id)
            # Create session logger (integrates with core logging automatically)
            from agents.task.logging_config import get_task_logger
            self.logger = get_task_logger("orchestrator", self.session_id)
            self.logger.debug(f"Session {self.session_id} ready")
        except Exception as e:
            # Create logger before raising error
            self.logger = logging.getLogger(f"task.orchestrator[{self.session_id}]")
            self.logger.error(f"Error creating/getting session: {e}")
            raise
        
        # REMOVED: Injected browser tracking - BrowserManager owns all browsers

        # Initialize or get BrowserManager
        self.browser_manager = browser_manager
        if not self.browser_manager:
            # Try to get from dependency container
            try:
                from core.container import DependencyContainer
                container = DependencyContainer.get_instance()
                if container and container.has_service('browser_manager'):
                    self.browser_manager = container.get_service('browser_manager')
                    self.logger.debug("Got BrowserManager from container")
            except Exception as e:
                self.logger.debug(f"Could not get BrowserManager from container: {e}")
                # Don't create a new instance - let it be created on demand if needed

        # TodoManager will be initialized before Controller (not lazy loaded)
        
        # Track initialization status
        self._initialized = False
        
        # Agent tracking dictionaries
        self.agents = {}  # agent_id -> agent_instance
        self.agent_types = {}  # agent_id -> agent type
        self.agent_names = {}  # agent_id -> agent name
        self.agent_creation_times = {}  # agent_id -> creation time
        self.agent_models = {}  # agent_id -> model name
        self.agent_execution_sequence = []  # Track agent execution sequence
        
        # CRITICAL FIX (Nov 26, 2025): Pending messages for pre-agent queuing
        # When messages arrive before agents exist, store here for later delivery
        self._pending_messages = []  # List of (text, kind, metadata) tuples
        # SECURITY FIX: Lock to prevent race condition between submit_user_message and create_agent
        self._pending_messages_lock = asyncio.Lock()
        
        # NOTE: Message routing only - agents own their queues via HITLManager

        # REMOVED: Legacy browser storage - use BrowserManager only
        # Warn if deprecated parameters provided
        # Get workspace directory from PathManager BEFORE creating controller
        try:
            # Always use PathManager for workspace directory
            _pm = (self.container.get_service("path_manager")
                   if self.container and hasattr(self.container, "get_service") else None) or pm()
            self._path_manager = _pm
            self._workspace_dir = str(_pm.get_workspace_dir(self.session_id, self.user_id))
            self.logger.debug(f"Using workspace directory: {self._workspace_dir}")
        except Exception as e:
            self.logger.error(f"Failed to create workspace directory: {e}")
            # Raise exception - cannot proceed without workspace
            raise RuntimeError(f"Critical error: Cannot properly set workspace path for session {self.session_id}. Session will be unusable.")

        # REMOVED: TodoManager initialization - now handled by TaskTool
        # TaskTool manages its own TodoManager instances per session

        # Initialize Controller for tool management
        # Try to get existing controller from container, or create new one
        from tools.controller.service import Controller

        # Get container (use provided one or try to get global instance)
        container = self.container
        if not container:
            try:
                from core.container import DependencyContainer
                container = DependencyContainer.get_instance()
            except Exception as e:
                self.logger.debug(f"No container available: {e}")

        # Try to get existing controller from container
        self.controller = None
        if container and hasattr(container, 'get_service'):
            try:
                self.controller = container.get_service('controller')
                if self.controller:
                    self.logger.debug("Using existing controller from container")
            except Exception as e:
                self.logger.debug(f"Could not get controller from container: {e}")

        # Create new controller if not found
        if not self.controller:
            self.controller = Controller(
                container=container,  # May be None
                orchestrator=self  # Controller gets session_id/user_id/workspace_dir from orchestrator
            )
            container_status = "with container" if container else "without container"
            self.logger.debug(f"Created new Controller {container_status}, workspace: {self._workspace_dir}")

        # Browser context management delegated to BrowserManager
        # REMOVED: browser_context_in_use - BrowserManager tracks all contexts

        # Initialize SubAgentManager for subtask delegation
        from agents.task.agent.sub_agent_manager import SubAgentManager
        self.sub_agent_manager = SubAgentManager(orchestrator=self)
        self.logger.debug("SubAgentManager initialized")

        # UP-12: durable async delegation registry (delegate_task background=true).
        # Reuses the SubAgentManager (its semaphore/timeout) and delivers completions
        # back into this session as a new turn via submit_user_message. Inert until a
        # background delegation is dispatched.
        from agents.task.agent.async_delegation import AsyncDelegationRegistry
        # Durable write-through to autonomy_state.db (restart-durable record of
        # dispatched/terminal delegations; recovery sweep in autonomy_state.py).
        # Fail-open — a missing/broken store keeps the legacy volatile registry.
        _deleg_store = None
        try:
            from agents.task.agent.autonomy_state import get_autonomy_state_store
            _deleg_store = get_autonomy_state_store()
        except Exception:
            _deleg_store = None
        self.async_delegation = AsyncDelegationRegistry(
            self.sub_agent_manager, deliver=self._deliver_async_delegation,
            store=_deleg_store, session_id=self.session_id, user_id=self.user_id,
        )

        # Track initialization status
        self._initialized = False
        # LLM concurrency management with validation
        self.session_llm_limit = max(1, int(os.environ.get('SESSION_LLM_LIMIT', '5')))
        self._session_llm_semaphore = asyncio.Semaphore(self.session_llm_limit)
        self.agent_llm_limits = {}  # agent_id -> limit mapping
        
        # Register with session manager
        try:
            self.session_manager.register_agent(
                session_id=self.session_id,
                agent_name="orchestrator",
                agent_id=f"orchestrator_{self.session_id}",
                agent_type="SessionOrchestrator",
                role="coordinator",
                user_id=self.user_id
            )
            self.logger.debug("Registered orchestrator with session manager")
        except Exception as e:
            self.logger.warning(f"Failed to register orchestrator with session manager: {e}")
        
        # Update session status
        try:
            self.session_manager.update_session_status(self.session_id, "initializing")
            self.logger.debug("Updated session status to initializing")
        except Exception as e:
            self.logger.warning(f"Failed to update session status: {e}")
        
        # Initialize async task tracking with better management
        self._execution_tasks = []

        # Track browser contexts separately from agents for proper cleanup
        # This prevents context leaks if agent is removed before cleanup
        self._browser_contexts = set()  # Set of context IDs (agent_id)

    async def initialize(self,
                        tool_ids=None,
                        tools_config=None,
                        controller=None) -> None:
        """Initialize the orchestrator with tools.

        Args:
            tool_ids: List of tool IDs to load from container
            tools_config: Tool configuration dict
            controller: Pre-created controller instance
        """
        if self._initialized:
            self.logger.debug("Orchestrator already initialized")
            return

        try:
            self.logger.info(f"Initializing orchestrator for session {self.session_id}")

            # Use provided controller if available, otherwise keep existing one
            if controller:
                self.controller = controller
                self.shared_controller = controller
                self.logger.debug("Using provided controller instance")

            # REMOVED: Early TodoManager initialization - now handled by TaskTool

            # Check if tools were already loaded
            existing_tools = self.controller.list_tools() if self.controller else []
            if existing_tools:
                self.logger.info(f"Controller already has {len(existing_tools)} tools loaded: {existing_tools}")

            # Determine which tools to load
            if not tool_ids and not existing_tools:
                # No tools specified and none loaded - use comprehensive default
                # Note: twitter removed from defaults - use the anysite tool for social media
                from agents.task.tool_defaults import server_default_tools
                tool_ids = server_default_tools()
                self.logger.info(f"No tool_ids specified, loading comprehensive defaults: {tool_ids}")
            elif tool_ids:
                # Tools specified - ensure defaults are included
                tools_to_load = list(tool_ids)  # Copy to avoid modifying caller's list
                for default in DEFAULT_TOOLS:
                    if default not in tools_to_load and default not in existing_tools:
                        tools_to_load.append(default)
                tool_ids = tools_to_load

            # Parse MCP server specifications from tool_ids AND tools_config
            # Format: "mcp:servername" or "mcp:user:servername"
            # If "mcp:X" and X is a registered tool class, load that tool directly
            # Otherwise treat it as a regular MCP server
            from tools.descriptors import get_tool_class
            
            mcp_servers_requested = []
            parsed_tool_ids = []

            # Helper to check if mcp:X should load a tool or an MCP server
            def process_mcp_spec(server_id: str):
                """Process mcp:X spec - load tool if registered, otherwise MCP server."""
                if not server_id.startswith("mcp:"):
                    return
                spec = server_id[4:]  # Remove "mcp:" prefix

                # Check if this is a registered tool (not a regular MCP server)
                # NOTE: Most MCP servers (like polymarket) are NOT registered as tools.
                # They're accessed purely via mcp_execute_tool.
                if get_tool_class(spec) is not None:
                    if spec not in parsed_tool_ids:
                        parsed_tool_ids.append(spec)
                        self.logger.info(f"Tool '{spec}' requested via {server_id}")
                else:
                    # Regular MCP server (or pseudo-MCP server like polymarket)
                    if spec not in mcp_servers_requested:
                        mcp_servers_requested.append(spec)
                    if "mcp" not in parsed_tool_ids:
                        parsed_tool_ids.append("mcp")

            # Check tools_config["mcp_servers"] (UI sends mcp_servers separately)
            mcp_servers_from_config = (tools_config or {}).get("mcp_servers", [])
            if mcp_servers_from_config:
                self.logger.info(f"MCP servers from tools_config: {mcp_servers_from_config}")
                for server_id in mcp_servers_from_config:
                    process_mcp_spec(server_id)

            for tool_id in (tool_ids or []):
                if tool_id.startswith("mcp:"):
                    # Use generic processor for mcp: specs
                    process_mcp_spec(tool_id)
                else:
                    parsed_tool_ids.append(tool_id)

            tool_ids = parsed_tool_ids

            if mcp_servers_requested:
                self.logger.info(f"MCP servers requested: {mcp_servers_requested}")

            # Filter out already loaded tools
            if tool_ids and existing_tools:
                tool_ids = [t for t in tool_ids if t not in existing_tools]
                if not tool_ids:
                    self.logger.info("All requested tools are already loaded, skipping tool loading")

            if tool_ids:
                try:
                    # Load tools through the controller
                    loaded_tools = await self.controller.load_tools_from_container(tool_ids)
                    total_actions = len(self.controller.registry.list_action_names())
                    self.logger.info(f"✓ Loaded {len(loaded_tools)} tools: {', '.join(f'{k}({len(self.controller.registry.get_actions_by_tool(k))})' for k in loaded_tools.keys())}")

                    # Special handling for browser tool
                    if "browser" in tool_ids and "browser" not in loaded_tools:
                        # Try to get from browser_manager
                        if self.browser_manager:
                            try:
                                browser = await self._ensure_browser_available()
                                if browser:
                                    self.controller.add_tool("browser", browser)
                                    loaded_tools["browser"] = browser
                                    self.logger.info("Loaded browser tool from browser_manager")
                            except Exception as e:
                                self.logger.error(f"Failed to get browser from browser_manager: {e}")
                        else:
                            self.logger.warning("Browser requested but browser_manager not available")

                    # Initialize MCP if available and configured
                    if "mcp" in loaded_tools:
                        self.logger.debug("MCP tool available")
                        mcp_service = loaded_tools["mcp"]

                        # Determine which servers to use
                        requested_servers = mcp_servers_requested if mcp_servers_requested else None

                        # Check tools_config for server list (multiple formats supported)
                        if not requested_servers and tools_config:
                            # Format 1: tools_config["mcp_servers"] = ["mcp:anysite", "mcp:ghost"]
                            if "mcp_servers" in tools_config:
                                mcp_servers_list = tools_config.get("mcp_servers", [])
                                # Strip "mcp:" prefix and filter out registered tools
                                # (those are loaded as tools, not MCP servers)
                                requested_servers = []
                                for s in mcp_servers_list:
                                    name = s[4:] if s.startswith("mcp:") else s
                                    # Skip if this is a registered tool (handled separately)
                                    if get_tool_class(name) is None:
                                        requested_servers.append(name)
                                if requested_servers:
                                    self.logger.info(f"MCP servers from tools_config[mcp_servers]: {requested_servers}")
                            # Format 2 (legacy): tools_config["mcp"]["servers"] = ["anysite", "ghost"]
                            elif "mcp" in tools_config:
                                mcp_config = tools_config.get("mcp", {})
                                if "servers" in mcp_config:
                                    requested_servers = mcp_config["servers"]

                        # Set up user context for user-specific MCP servers
                        if self.user_id and hasattr(mcp_service, 'set_user_context'):
                            mcp_service.set_user_context(self.user_id)

                    # Set up user context for ALL tools that support it (generic approach)
                    if self.user_id:
                        for tool_name, tool_instance in loaded_tools.items():
                            if hasattr(tool_instance, 'set_user_context'):
                                tool_instance.set_user_context(self.user_id)
                                self.logger.info(f"User context set for tool '{tool_name}'")

                    # Continue MCP setup if MCP was loaded
                    if "mcp" in loaded_tools:
                        mcp_service = loaded_tools["mcp"]
                        # Load user MCP service if available
                        if self.user_id and self.container and hasattr(mcp_service, 'set_user_mcp_service'):
                            try:
                                user_mcp_service = self.container.get_service('user_mcp_service')
                                if user_mcp_service:
                                    mcp_service.set_user_mcp_service(user_mcp_service)
                                    self.logger.info("User MCP service configured")
                            except Exception as e:
                                self.logger.warning(f"Could not get user MCP service: {e}")

                        # Load user servers if any user: servers were requested
                        user_servers_requested = [s for s in (requested_servers or []) if s.startswith("user:")]
                        if user_servers_requested and hasattr(mcp_service, 'load_user_servers'):
                            # C6: pass this session's tenant explicitly (don't rely on the
                            # shared self._current_user_id, which a concurrent session mutates).
                            load_result = await mcp_service.load_user_servers(user_id=self.user_id)
                            # Handle both old (int) and new (UserServersLoadResult) return types
                            if hasattr(load_result, 'loaded_count'):
                                user_count = load_result.loaded_count
                                if load_result.failed_count > 0:
                                    self.logger.warning(
                                        f"Failed to load {load_result.failed_count} user MCP servers: "
                                        f"{[f['name'] for f in load_result.failed_servers]}"
                                    )
                                if load_result.timed_out:
                                    self.logger.warning("User MCP server loading timed out")
                            else:
                                user_count = load_result  # Old int return type
                            self.logger.info(f"Loaded {user_count} user MCP servers")

                        if requested_servers:
                            self.logger.info(
                                f"Configuring MCP with servers: {requested_servers}"
                            )

                            # FIX #4: Convert user server request format to internal format
                            # API format: "user:servername" (after stripping mcp: prefix)
                            # Internal format: "user_{user_id}::servername"
                            # This ensures set_requested_servers() filter matches actual server names
                            normalized_servers = []
                            for server in requested_servers:
                                if server.startswith("user:") and self.user_id:
                                    # Convert "user:servername" → "user_{user_id}::servername"
                                    server_name = server[5:]  # Remove "user:" prefix
                                    internal_name = f"user_{self.user_id}::{server_name}"
                                    normalized_servers.append(internal_name)
                                    self.logger.debug(f"Normalized user server: {server} → {internal_name}")
                                else:
                                    normalized_servers.append(server)
                            
                            requested_servers = normalized_servers

                            # Verify requested servers are available (optional check)
                            if hasattr(mcp_service, 'server_manager') and mcp_service.server_manager:
                                available = mcp_service.server_manager.get_all_tools()
                                # Check against internal format (user servers now normalized)
                                # Note: polymarket uses gateway, not server_manager, so it won't appear here
                                missing = [s for s in requested_servers if s not in available and s != 'polymarket']
                                if missing:
                                    self.logger.warning(
                                        f"Requested MCP servers not in server_manager: {missing}"
                                    )
                                else:
                                    self.logger.info(
                                        f"All requested MCP servers are available: {list(available.keys())}"
                                    )

                            # FIX (Jan 2026): ALWAYS apply server filtering, even without server_manager
                            # This is critical for polymarket which uses gateway, not server_manager
                            # Previously this was inside the server_manager check, causing polymarket
                            # to never appear in the prompt when it was the only MCP server selected
                            if hasattr(mcp_service, 'set_requested_servers'):
                                mcp_service.set_requested_servers(requested_servers)
                                self.logger.info(
                                    f"Applied MCP server filter: {requested_servers}"
                                )

                    # MCP tools use discovery pattern - no waiting needed
                    # Agent discovers tools via mcp_list_tools and executes via mcp_execute_tool
                    if 'mcp' in loaded_tools:
                        self.logger.info("✅ MCP tool loaded - tools accessible via mcp_execute_tool")
                        
                        # Log final tool count
                        total_actions = len(self.controller.registry.list_action_names())
                        self.logger.info(f"📊 Total registered actions: {total_actions}")

                except Exception as e:
                    self.logger.error(f"Error loading tools: {e}")
                    # Load minimum required tools
                    try:
                        from tools.filesystem import FileSystem
                        fs_tool = FileSystem('filesystem', self.config, self.container)
                        await fs_tool.initialize()
                        self.controller.add_tool('filesystem', fs_tool)
                        self.logger.info("Loaded filesystem tool as fallback")
                    except Exception as fallback_error:
                        self.logger.error(f"Failed to load even fallback tools: {fallback_error}")

            # Mark as initialized
            self._initialized = True
            self.logger.info(f"✅ Orchestrator initialized successfully for session {self.session_id}")

        except Exception as e:
            self.logger.error(f"Failed to initialize orchestrator: {e}")
            raise

    def cancel(self) -> None:
        """Cancel all agents in this session.

        Sets cancellation flag on all agents, causing them to stop
        execution at the next step boundary.
        """
        self.logger.info(f"❌ Cancelling session {self.session_id}")

        # Cancel all agents
        cancelled_count = 0
        for agent_id, agent in self.agents.items():
            try:
                if hasattr(agent, 'cancel'):
                    agent.cancel()
                    cancelled_count += 1
                    self.logger.debug(f"Cancelled agent {agent_id}")
            except Exception as e:
                self.logger.error(f"Error cancelling agent {agent_id}: {e}")

        if cancelled_count > 0:
            self.logger.info(f"Cancelled {cancelled_count} agent(s) in session {self.session_id}")

        # Update session status
        if hasattr(self, 'session_manager') and self.session_manager:
            try:
                self.session_manager.update_session_status(self.session_id, 'cancelled')
                self.logger.debug(f"Updated session status to cancelled")
            except Exception as e:
                self.logger.error(f"Error updating session status: {e}")

    # REMOVED: get_todo_manager() - Deprecated method removed
    # Agent now accesses TaskTool directly via controller.get_tool('task')

    # The run_session method has been removed as it references attributes that don't exist
    # in the SessionOrchestrator class (active_sessions, task_available, container, etc.)
    # This functionality should be implemented in the AutoV2Agent class instead.

    async def create_agent_from_profile(
        self,
        profile_id: str,
        task: str,
        llm: Any = None,
        overrides: Optional[Dict[str, Any]] = None,
        share_controller: bool = True,
        **kwargs
    ) -> Agent:
        """Create an agent from a profile configuration.
        
        Args:
            profile_id: Profile identifier to load
            task: Task description
            llm: Optional LLM override
            overrides: Optional profile overrides
            share_controller: Whether to use the shared controller (default: True)
            **kwargs: Additional agent parameters
            
        Returns:
            Configured Agent instance
        """
        self.logger.info(f"Creating agent from profile '{profile_id}' (share_controller={share_controller})")
        
        # Pass profile_id and share_controller to create_agent
        return await self.create_agent(
            task=task,
            llm=llm,
            profile_id=profile_id,
            profile_overrides=overrides,
            share_controller=share_controller,
            **kwargs
        )

    # --- SessionContext Protocol adapter methods ---

    def get_agents(self) -> dict:
        return self.agents

    def get_tool_call_tracker(self):
        if self.agents:
            agent = next(iter(self.agents.values()))
            return getattr(agent, 'tool_call_tracker', None)
        return None

    def get_sub_agent_manager(self):
        return getattr(self, 'sub_agent_manager', None)

    async def _deliver_async_delegation(self, rec, block: str) -> None:
        """Deliver a background-delegation completion back into THIS session as a new
        turn (UP-12). Routes through submit_user_message (kind='delegation_result') so
        it re-enters via the HITL queue -> run-loop drain -> inject_user_guidance,
        preserving message-role alternation. submit_user_message itself parks the
        message if no agent is active yet and can raise MessageQueueFullError if the
        queue is full — AsyncDelegationRegistry wraps this call, so a failure here is
        logged, not fatal.

        P1-5: the RESULT is always delivered (submit_user_message parks it if no loop
        is active, or hands it to an active loop). Only the WAKE KICK — forging a fresh
        run to drain an idle session — is bounded by the shared ReentryBudget, because
        that is the ping-pong risk (a self-triggering delegation chain re-forging runs
        indefinitely). The budget is applied UNCONDITIONALLY (the kick fires regardless
        of SELF_WAKE_ENABLED — previously it was only budgeted when self-wake was on, so
        the server default left it unbounded) and FAIL-CLOSED (a budget-guard error
        skips the kick rather than opening the storm rail). A genuine owner turn resets
        the budget."""
        # A3: single-source the forged-turn kind so producer, marker-recompute, and
        # the security gate all reference one constant (see self_wake.FORGED_TURN_KINDS).
        from agents.task.agent.core.self_wake import DELEGATION_RESULT_KIND
        await self.submit_user_message(
            agent_id=getattr(rec, "parent_agent_id", None),
            text=block,
            kind=DELEGATION_RESULT_KIND,
            metadata={
                "source": "async_delegation",
                "delegation_id": rec.delegation_id,
                "status": rec.status,
            },
        )
        # SA-01: if the parent loop already ended (the agent called done() /
        # conversational-exit before the background child finished), submit_user_message
        # only PARKED the result in the HITL queue — nothing would ever drain it, so the
        # deliverable the user was promised as "a new turn" silently vanished. Kick a
        # fresh run to drain it. Idempotent: run_session refuses concurrent execution
        # ("session is already executing"), so this is a cheap no-op when a loop is still
        # active. The kick is set by TaskAgent.register_orchestrator; absent (bare
        # orchestrator / tests) => byte-identical to the parked-only UP-12 behavior.
        kick = getattr(self, "_wake_kick", None)
        if kick is None:
            return
        # P1-5: budget the kick (unconditional + fail-CLOSED). An exhausted budget parks
        # the result without forging another run; a budget-guard error also skips the
        # kick (never open the storm rail on an error).
        try:
            from agents.task.agent.core.self_wake import get_reentry_budget
            if not get_reentry_budget().try_consume(self.session_id):
                self.logger.info(
                    f"🛌 re-entry budget exhausted — parking async-delegation result "
                    f"without a wake kick for session {self.session_id}"
                )
                return
        except Exception:
            self.logger.debug(
                "re-entry budget guard errored — skipping wake kick (fail-closed)",
                exc_info=True,
            )
            return
        try:
            import asyncio as _asyncio
            t = _asyncio.create_task(kick())
            _ASYNC_DELEGATION_KICKS.add(t)
            t.add_done_callback(_ASYNC_DELEGATION_KICKS.discard)
        except Exception:
            self.logger.debug("async-delegation wake kick failed (fail-open)", exc_info=True)

    def get_telemetry_manager(self):
        return getattr(self, 'telemetry_manager', None)
