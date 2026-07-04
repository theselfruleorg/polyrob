"""MCP (Model Context Protocol) Service for POLYROB."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Callable, Tuple
from core.config import BotConfig
from core.exceptions import ToolError, ConfigurationError, ComponentInitializationError
from tools.base_tool import BaseTool
from modules.memory.cache_manager import CacheManager

from .config import MCPConfig, get_default_mcp_config, resolve_config_environment_variables
from .param_coercion import coerce_arguments as _coerce_arguments
from .server_manager import MCPServerManager, ServerStatus
from .views import (
    MCPExecuteToolAction, MCPReadResourceAction, MCPListToolsAction, MCPListResourcesAction,
    MCPListServersAction, MCPServerStatusAction, MCPConnectServerAction, MCPDisconnectServerAction,
    MCPReloadServerAction, MCPGetCapabilitiesAction, MCPSubscribeResourceAction, 
    MCPUnsubscribeResourceAction, MCPHealthCheckAction,
    MCPToolInfo, MCPResourceInfo, MCPServerInfo, MCPExecutionResult, MCPResourceContent, MCPHealthStatus
)


@dataclass
class UserServersLoadResult:
    """Result of loading user MCP servers (FIX #8)."""
    loaded_count: int = 0
    failed_count: int = 0
    failed_servers: List[Dict[str, str]] = field(default_factory=list)  # [{name, error}]
    timed_out: bool = False


class MCPTool(BaseTool):
    """Service for managing MCP (Model Context Protocol) servers and operations."""
    
    def __init__(self, name: str, config: BotConfig, container: Optional[Any] = None):
        """Initialize MCP service."""
        super().__init__(name=name, config=config, container=container)
        
        # MCP configuration
        self.mcp_config = getattr(config, 'mcp', get_default_mcp_config())
        if not isinstance(self.mcp_config, MCPConfig):
            # Convert dict to MCPConfig if needed
            if isinstance(self.mcp_config, dict):
                self.mcp_config = MCPConfig(**self.mcp_config)
            else:
                self.mcp_config = get_default_mcp_config()
        
        # Resolve environment variables in configuration
        try:
            self.mcp_config = resolve_config_environment_variables(self.mcp_config)
        except Exception as e:
            self.logger.error(f"Failed to resolve MCP configuration environment variables: {e}")
            self.mcp_config = get_default_mcp_config()
        
        # Server manager
        self.server_manager: Optional[MCPServerManager] = None
        
        # Cache for resources if enabled
        self.cache_manager: Optional[CacheManager] = None

        # Server filtering - allows loading only specific MCP servers
        self.requested_servers: Optional[List[str]] = None

        # User context for per-user MCP servers.
        # NOTE (C6): this instance is a process-wide singleton, so _current_user_id is
        # shared mutable state. load/unload take an explicit user_id and snapshot it;
        # _loaded_users tracks which tenants are loaded per-user (a single boolean would
        # be reset by another tenant's set_user_context).
        self._current_user_id: Optional[str] = None
        self._loaded_users: set = set()

        # Per-(user, server) execution rate limiter (WS-B3) — guards the execute
        # path (expensive crawl/scrape tools), not just add_server.
        import os as _os
        from tools.mcp.rate_limit import MCPExecRateLimiter
        self._exec_rate_limiter = MCPExecRateLimiter(
            max_calls=int(_os.getenv("MCP_EXEC_RATE_PER_WINDOW", "20")),
            window_seconds=int(_os.getenv("MCP_EXEC_RATE_WINDOW_SEC", "60")),
        )

        # User MCP service (set via set_user_mcp_service)
        self._user_mcp_service: Optional[Any] = None

        # Enable if MCP is configured and enabled
        self._enabled = self.mcp_config.enabled and bool(self.mcp_config.servers)

        # MCP uses discovery pattern - no callbacks needed
        # Tools are discovered via list_tools and executed via execute_tool

        if not self._enabled:
            self.logger.info("MCP service is disabled or has no configured servers")
        else:
            self.logger.info(f"MCP service initialized with {len(self.mcp_config.servers)} servers")

    @property
    def required_services(self) -> Dict[str, str]:
        """Get required services."""
        return {
            'rate_limit_manager': 'Rate limit management for MCP operations'
        }
    
    @property
    def optional_services(self) -> Dict[str, str]:
        """Get optional services."""
        return {
            'cache_manager': 'Cache for MCP resources and metadata'
        }

    @property
    def required_config(self) -> Dict[str, str]:
        """Get required configuration keys."""
        # MCP service doesn't require specific config keys since it's disabled by default
        # Configuration is handled through the mcp section in BotConfig
        return {}

    async def _initialize(self) -> None:
        """Initialize MCP service."""
        # First call parent's _initialize to register decorated actions
        await super()._initialize()
        
        if not self._enabled:
            self.logger.info("MCP service is disabled, skipping initialization")
            return
        
        try:
            # Get optional cache manager
            self.cache_manager = self.get_service('cache_manager')
            if self.cache_manager:
                self.logger.info("Cache manager available for MCP resource caching")
            
            # Initialize server manager
            self.server_manager = MCPServerManager(
                global_timeout=self.mcp_config.global_timeout,
                max_concurrent=self.mcp_config.max_concurrent_connections,
            )
            
            # Start server manager
            await self.server_manager.start()
            
            # Add configured servers
            self.logger.info(f"Adding {len(self.mcp_config.servers)} configured MCP servers...")
            for server_name, server_config in self.mcp_config.servers.items():
                try:
                    self.logger.info(
                        f"Adding server '{server_name}' "
                        f"(type={server_config.type.value}, enabled={server_config.enabled})"
                    )
                    success = await self.server_manager.add_server(server_name, server_config)
                    if success:
                        self.logger.info(f"✅ Successfully added MCP server '{server_name}'")
                    else:
                        self.logger.warning(f"⚠️ Failed to add MCP server '{server_name}'")
                except Exception as e:
                    self.logger.error(f"❌ Error adding MCP server '{server_name}': {e}", exc_info=True)
            
            # Log initialization summary with detailed status
            server_list = await self.server_manager.list_servers()
            connected_servers = [s for s in server_list if s['status'] == ServerStatus.CONNECTED.value]
            failed_servers = [s for s in server_list if s['status'] == ServerStatus.ERROR.value]
            
            self.logger.info(
                f"📊 MCP service initialized: "
                f"{len(connected_servers)}/{len(server_list)} servers connected"
            )
            
            if connected_servers:
                for server in connected_servers:
                    self.logger.info(
                        f"  ✅ {server['name']}: {server['tools_count']} tools, "
                        f"{server['resources_count']} resources"
                    )
            
            if failed_servers:
                for server in failed_servers:
                    self.logger.warning(
                        f"  ❌ {server['name']}: {server['last_error']}"
                    )

            # Log total tools available via discovery pattern
            all_tools = self.server_manager.get_all_tools()
            total_tools = sum(len(tools) for tools in all_tools.values())
            self.logger.info(
                f"📋 {total_tools} MCP tools available via mcp_execute_tool action"
            )

        except Exception as e:
            self.logger.error(f"Failed to initialize MCP service: {e}")
            raise ComponentInitializationError(f"Failed to initialize MCP service: {e}")

    async def _cleanup(self) -> None:
        """Cleanup MCP service resources."""
        try:
            if self.server_manager:
                await self.server_manager.stop()
                self.server_manager = None
                
            self.logger.info("MCP service cleaned up successfully")
            
        except Exception as e:
            self.logger.error(f"Error during MCP service cleanup: {e}")
            raise ToolError(f"Failed to cleanup MCP service: {e}")

    def set_requested_servers(self, servers: Optional[List[str]]) -> None:
        """Set which MCP servers to use for tool discovery.

        This allows filtering which MCP server tools are exposed to the LLM.
        If not set or None, all configured servers are used (default behavior).

        Supports namespaced server format:
        - "global::servername" for global servers
        - "user_{user_id}::servername" for user servers

        Args:
            servers: List of server names to use, or None for all servers
        """
        self.requested_servers = servers
        if servers:
            self.logger.info(f"MCP tool discovery limited to servers: {servers}")

        else:
            self.logger.debug("MCP tool discovery will use all available servers")

    def set_user_mcp_service(self, service: Any) -> None:
        """Set the UserMCPService for loading user-specific servers.

        Args:
            service: UserMCPService instance
        """
        self._user_mcp_service = service
        self.logger.debug("UserMCPService configured for MCPTool")

    def set_user_context(self, user_id: Optional[str]) -> None:
        """Set the current user context for user-specific servers.

        This should be called when a session starts to enable loading
        of user-specific MCP servers.

        Args:
            user_id: User ID, or None to clear user context
        """
        if user_id != self._current_user_id:
            self._current_user_id = user_id
            if user_id:
                self.logger.info(f"MCP user context set: {user_id}")
            else:
                self.logger.debug("MCP user context cleared")

    async def load_user_servers(
        self,
        user_id: Optional[str] = None,
        overall_timeout: float = 60.0  # FIX #9: Overall timeout for loading all servers
    ) -> UserServersLoadResult:
        """Load user-specific MCP servers.

        Args:
            user_id: The tenant to load servers for. C6: pass this EXPLICITLY (from the
                session's user_id). It is snapshotted here and used consistently across
                awaits, so a concurrent session mutating the shared self._current_user_id
                can't make this loop namespace/fetch servers for the wrong tenant. Falls
                back to self._current_user_id only for legacy callers.
            overall_timeout: Maximum time to spend loading all servers (FIX #9)

        Returns:
            UserServersLoadResult with loaded/failed counts and error details (FIX #8)
        """
        result = UserServersLoadResult()

        # C6: snapshot the tenant ONCE — never re-read the shared field across awaits.
        uid = user_id or self._current_user_id

        if not uid:
            self.logger.debug("No user context set, skipping user server loading")
            return result

        if uid in self._loaded_users:
            self.logger.debug("User servers already loaded")
            return result

        if not self._user_mcp_service:
            self.logger.warning("UserMCPService not configured, cannot load user servers")
            return result

        if not self.server_manager:
            self.logger.warning("Server manager not initialized, cannot load user servers")
            return result

        try:
            # FIX #9: Wrap entire loading process in timeout
            async with asyncio.timeout(overall_timeout):
                # Get user's enabled servers
                user_servers = await self._user_mcp_service.get_user_servers(
                    uid,
                    enabled_only=True
                )

                for server in user_servers:
                    # Create namespaced server name
                    namespaced_name = f"user_{uid}::{server.server_name}"

                    # Get server config with decrypted credentials
                    config = await self._user_mcp_service.get_server_config(
                        uid,
                        server.server_name
                    )

                    if config:
                        try:
                            success = await self.server_manager.add_server(namespaced_name, config)
                            if success:
                                self.logger.info(f"Added user MCP server: {namespaced_name}")
                                result.loaded_count += 1
                            else:
                                # FIX #8: Track failed servers
                                error_msg = "Server manager rejected server (unknown reason)"
                                result.failed_count += 1
                                result.failed_servers.append({
                                    "name": server.server_name,
                                    "error": error_msg
                                })
                                self.logger.warning(f"Failed to add user MCP server: {namespaced_name}")
                        except Exception as e:
                            # FIX #8: Track failed servers with error details
                            error_msg = str(e)
                            result.failed_count += 1
                            result.failed_servers.append({
                                "name": server.server_name,
                                "error": error_msg
                            })
                            self.logger.error(f"Error adding user MCP server {namespaced_name}: {e}")

                self._loaded_users.add(uid)
                self.logger.info(
                    f"Loaded {result.loaded_count} user MCP servers for user {uid}"
                    f" ({result.failed_count} failed)"
                )
                return result

        except asyncio.TimeoutError:
            # FIX #9: Handle overall timeout
            result.timed_out = True
            self.logger.error(
                f"Timeout loading user servers after {overall_timeout}s - "
                f"loaded {result.loaded_count}, failed {result.failed_count}"
            )
            self._loaded_users.add(uid)  # Mark as loaded to prevent retry loops
            return result

        except Exception as e:
            self.logger.error(f"Failed to load user servers: {e}")
            return result

    async def unload_user_servers(self, user_id: Optional[str] = None) -> int:
        """Unload user-specific MCP servers.

        Args:
            user_id: The tenant to unload. C6: pass this EXPLICITLY (the session's
                user_id) so cleanup never disconnects another tenant's servers via the
                shared self._current_user_id. Falls back to self._current_user_id for
                legacy callers.

        Returns:
            Number of servers unloaded
        """
        uid = user_id or self._current_user_id
        if not uid or not self.server_manager:
            return 0

        prefix = f"user_{uid}::"
        unloaded_count = 0

        # Find and remove all servers with user prefix
        servers_to_remove = [
            name for name in self.server_manager.connections.keys()
            if name.startswith(prefix)
        ]

        for server_name in servers_to_remove:
            try:
                await self.server_manager.disconnect_server(server_name)
                unloaded_count += 1
                self.logger.debug(f"Unloaded user MCP server: {server_name}")
            except Exception as e:
                self.logger.error(f"Error unloading user MCP server {server_name}: {e}")

        self._loaded_users.discard(uid)
        if unloaded_count:
            self.logger.info(f"Unloaded {unloaded_count} user MCP servers")

        return unloaded_count

    def get_global_server_names(self) -> List[str]:
        """Get list of global (platform) MCP server names.

        Returns:
            List of global server names with 'global::' prefix
        """
        if not self.mcp_config or not self.mcp_config.servers:
            return []

        return [f"global::{name}" for name in self.mcp_config.servers.keys()]

    # MCP Tool Operations
    
    @BaseTool.action(
        'Execute a tool on an MCP server',
        param_model=MCPExecuteToolAction
    )
    async def execute_tool(self, params: MCPExecuteToolAction, execution_context=None) -> MCPExecutionResult:
        """Execute a tool on an MCP server.

        Args:
            params: Tool execution parameters
            execution_context: ActionExecutionContext with workspace_dir for saving large results
        """
        await self.ensure_initialized()

        if not self._enabled:
            return MCPExecutionResult(
                success=False,
                error="MCP service is disabled"
            )

        return await self._execute_validated(
            params.server_name,
            params.tool_name,
            params.arguments,
            execution_context=execution_context,
        )

    async def _execute_validated(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        execution_context=None,
    ) -> MCPExecutionResult:
        """Shared validate→coerce→execute→truncate core used by both entry points.

        Applies, in order:
          1. Per-(user, server) exec rate limit (WS-B3)
          2. Token-bucket rate limiter (``rate_limit``)
          3. requested_servers allowlist check
          4. Empty-arguments detection with schema-guided error
          5. ``_validate_and_convert_parameters`` schema coercion + validation-failure tracking
          6. ``server_manager.execute_tool`` invocation
          7. Validation-failure counter clear on success
          8. ``process_tool_result`` truncation / workspace offloading

        Args:
            server_name: MCP server identifier.
            tool_name: Tool name on that server.
            arguments: Raw arguments dict (may be mutated to coerced values).
            execution_context: ActionExecutionContext for workspace_dir / user_id resolution.

        Returns:
            ``MCPExecutionResult`` — callers that need a raw result unwrap ``.result``.
        """
        start_time = time.time()

        try:
            # Per-(user, server) execution rate limit (WS-B3). Coach the LLM to
            # back off rather than raising. Shared with the flattened direct-action
            # path via _enforce_exec_rate_limit (UP-01).
            _limited = self._enforce_exec_rate_limit(server_name, execution_context)
            if _limited:
                return MCPExecutionResult(
                    success=False,
                    error=_limited,
                    execution_time=time.time() - start_time,
                )

            # Apply rate limiting
            await self.rate_limit(f"mcp_execute_tool_{server_name}")

            # FIX (Dec 31, 2025): Block execution on non-allowed servers
            # This ensures only UI-selected MCP servers can be used
            if self.requested_servers and server_name not in self.requested_servers:
                self.logger.warning(
                    f"🚫 Blocked execution on server '{server_name}' - "
                    f"not in allowed servers: {self.requested_servers}"
                )
                return MCPExecutionResult(
                    success=False,
                    error=f"Server '{server_name}' is not enabled for this session. "
                          f"Enabled servers: {', '.join(self.requested_servers)}",
                    execution_time=time.time() - start_time
                )

            # GROK FIX: Detect empty arguments and provide VERY AGGRESSIVE error
            # Some LLMs (especially Grok/OpenRouter) understand parameters conceptually
            # but fail to place them in the nested arguments dict
            if not arguments or arguments == {}:
                self.logger.warning(f"⚠️ Empty arguments detected for '{tool_name}'")

                # Get schema to build helpful error
                connection = self.server_manager.connections.get(server_name)
                if connection and connection.status == ServerStatus.CONNECTED:
                    tool_schema = None
                    for tool in connection.tools:
                        if tool.name == tool_name:
                            tool_schema = tool.input_schema
                            break

                    if tool_schema:
                        required = tool_schema.get("required", [])
                        properties = tool_schema.get("properties", {})

                        if required:
                            # Build example with actual required params
                            example_args = {}
                            for req in required:
                                prop = properties.get(req, {})
                                prop_type = prop.get("type", "string")
                                # Note: prop_desc available via prop.get("description", "") if needed
                                if prop_type == "string":
                                    if "query" in req.lower() or "keyword" in req.lower():
                                        example_args[req] = "AI startups 2025"
                                    elif "url" in req.lower():
                                        example_args[req] = "https://example.com"
                                    elif "user" in req.lower():
                                        example_args[req] = "john-doe-123"
                                    else:
                                        example_args[req] = f"your_{req}_here"
                                elif prop_type in ("integer", "number"):
                                    example_args[req] = 10
                                elif prop_type == "boolean":
                                    example_args[req] = True
                                elif prop_type == "array":
                                    example_args[req] = ["item1", "item2"]
                                else:
                                    example_args[req] = f"<{req}>"

                            import json
                            error_msg = (
                                f"\n{'='*70}\n"
                                f"🚨🚨🚨 CRITICAL ERROR: EMPTY ARGUMENTS 🚨🚨🚨\n"
                                f"{'='*70}\n\n"
                                f"Tool: {tool_name}\n"
                                f"You sent: arguments = {{}}\n"
                                f"Required parameters: {required}\n\n"
                                f"{'='*70}\n"
                                f"⚠️ THE PARAMETERS GO INSIDE THE ARGUMENTS DICT! ⚠️\n"
                                f"{'='*70}\n\n"
                                f"❌ WRONG (what you did):\n"
                                f"   mcp_execute_tool(\n"
                                f"       server_name=\"{server_name}\",\n"
                                f"       tool_name=\"{tool_name}\",\n"
                                f"       arguments={{}}  ← EMPTY! TOOL GETS NOTHING!\n"
                                f"   )\n\n"
                                f"✅ CORRECT (copy this exactly):\n"
                                f"   mcp_execute_tool(\n"
                                f"       server_name=\"{server_name}\",\n"
                                f"       tool_name=\"{tool_name}\",\n"
                                f"       arguments={json.dumps(example_args)}\n"
                                f"   )\n\n"
                                f"{'='*70}\n"
                                f"REMEMBER: arguments={{\"query\": \"your search\"}} NOT arguments={{}}\n"
                                f"{'='*70}\n"
                            )

                            self.logger.error(error_msg)
                            return MCPExecutionResult(
                                success=False,
                                error=error_msg,
                                execution_time=time.time() - start_time
                            )

            # Validate and convert parameters before execution
            connection = self.server_manager.connections.get(server_name)
            if connection and connection.status == ServerStatus.CONNECTED:
                self.logger.info(f"🔍 Pre-validation params for '{tool_name}': {arguments}")

                tool_schema = None
                for tool in connection.tools:
                    if tool.name == tool_name:
                        tool_schema = tool.input_schema
                        break

                if tool_schema:
                    self.logger.info(f"📋 Schema found for '{tool_name}' - running validation & conversion")

                    # Validate and convert parameters
                    validated_args, validation_errors = self._validate_and_convert_parameters(
                        tool_schema,
                        arguments,
                        tool_name
                    )

                    if validation_errors:
                        import json

                        # Issue #1 Fix: Track validation failures for this tool
                        # After repeated failures, inject full schema to help LLM
                        failure_count = 0
                        should_inject_full_schema = False

                        if self.container:
                            try:
                                # Try to get tool_call_tracker from orchestrator
                                orchestrator = self.container.get_service('orchestrator') if hasattr(self.container, 'get_service') else None
                                if orchestrator and hasattr(orchestrator, 'agents'):
                                    for agent in orchestrator.agents.values():
                                        if hasattr(agent, 'tool_call_tracker'):
                                            failure_count = agent.tool_call_tracker.track_mcp_validation_failure(
                                                server_name, tool_name
                                            )
                                            should_inject_full_schema = agent.tool_call_tracker.should_inject_mcp_schema(
                                                server_name, tool_name
                                            )
                                            break
                            except Exception as e:
                                self.logger.debug(f"Could not track validation failure: {e}")

                        # Build example with required params
                        required_params = tool_schema.get("required", [])
                        properties = tool_schema.get("properties", {})
                        example_args = {}
                        for req in required_params:
                            prop = properties.get(req, {})
                            prop_type = prop.get("type", "string")
                            # Note: description available via prop.get("description", "") if needed
                            if prop_type == "string":
                                example_args[req] = f"YOUR_{req.upper()}_HERE"
                            elif prop_type == "integer" or prop_type == "number":
                                example_args[req] = 10
                            elif prop_type == "boolean":
                                example_args[req] = True
                            elif prop_type == "array":
                                example_args[req] = ["item1"]
                            else:
                                example_args[req] = f"<{req}>"

                        # Build VERY explicit error message
                        error_lines = [
                            "=" * 60,
                            f"❌ CRITICAL ERROR: EMPTY OR INVALID ARGUMENTS (failure #{failure_count})",
                            "=" * 60,
                            "",
                            f"Tool '{tool_name}' REQUIRES these parameters: {required_params}",
                            "",
                            f"🚫 YOU SENT: arguments = {json.dumps(arguments)}",
                            "",
                            "✅ YOU MUST SEND:",
                            f'   arguments = {json.dumps(example_args)}',
                            "",
                            "📋 COPY THIS EXACT FORMAT:",
                            "```json",
                            json.dumps({
                                "server_name": server_name,
                                "tool_name": tool_name,
                                "arguments": example_args
                            }, indent=2),
                            "```",
                            "",
                            "⚠️ The 'arguments' field MUST contain the tool's parameters!",
                            "   DO NOT leave arguments empty: {}",
                        ]

                        # Issue #1 Fix: After 2+ failures, inject full schema
                        if should_inject_full_schema:
                            error_lines.extend([
                                "",
                                "🔴 REPEATED FAILURE - FULL SCHEMA INJECTED:",
                                "```json",
                                json.dumps(tool_schema, indent=2),
                                "```",
                            ])

                        error_lines.append("=" * 60)
                        error_msg = "\n".join(error_lines)

                        self.logger.error(error_msg)
                        return MCPExecutionResult(
                            success=False,
                            error=error_msg,
                            execution_time=time.time() - start_time
                        )

                    # Use validated parameters
                    arguments = validated_args
                    self.logger.info(f"✅ Parameters validated for {tool_name}")
                    self.logger.info(f"🔍 Post-validation params: {arguments}")
                else:
                    self.logger.warning(f"⚠️  No schema found for '{tool_name}' - skipping validation/conversion")

            # Execute the tool with validated parameters via server_manager
            result = await self.server_manager.execute_tool(
                server_name,
                tool_name,
                arguments
            )

            execution_time = time.time() - start_time

            # Issue #1 Fix: Clear validation failures on success
            if self.container:
                try:
                    orchestrator = self.container.get_service('orchestrator') if hasattr(self.container, 'get_service') else None
                    if orchestrator and hasattr(orchestrator, 'agents'):
                        for agent in orchestrator.agents.values():
                            if hasattr(agent, 'tool_call_tracker'):
                                agent.tool_call_tracker.clear_mcp_validation_failures(
                                    server_name, tool_name
                                )
                                break
                except Exception as e:
                    self.logger.debug(f"Could not clear validation failures: {e}")

            # FIX (Jan 2026): Truncate large MCP responses to prevent context overflow
            # MCP responses were adding 60K+ tokens per step without any limits
            from utils.result_size import process_tool_result

            # Get workspace path for saving large results
            # PRIORITY: Use execution_context (works for sub-agents), fallback to container
            workspace_dir = None
            if execution_context and hasattr(execution_context, 'workspace_dir'):
                workspace_dir = execution_context.workspace_dir

            # Fallback to container if execution_context not available
            if not workspace_dir and self.container:
                try:
                    orchestrator = self.container.get_service('orchestrator') if hasattr(self.container, 'get_service') else None
                    if orchestrator and hasattr(orchestrator, 'workspace_dir'):
                        workspace_dir = orchestrator.workspace_dir
                except Exception:
                    pass

            # Process result with automatic truncation/offloading
            processed_result, was_truncated, saved_path = process_tool_result(
                result,
                workspace_dir=workspace_dir,
                prefix=f"mcp_{server_name}_{tool_name}"
            )

            if was_truncated:
                self.logger.info(
                    f"📦 Large MCP result truncated for '{tool_name}' "
                    f"(saved to {saved_path})"
                )

            self.logger.info(f"Tool '{tool_name}' executed successfully on server '{server_name}' in {execution_time:.2f}s")

            return MCPExecutionResult(
                success=True,
                result=processed_result,
                execution_time=execution_time
            )

        except Exception as e:
            execution_time = time.time() - start_time
            self.logger.error(f"Tool execution failed: {e}")

            return MCPExecutionResult(
                success=False,
                error=str(e),
                execution_time=execution_time
            )

    @BaseTool.action(
        'Read a resource from an MCP server',
        param_model=MCPReadResourceAction
    )
    async def read_resource(self, params: MCPReadResourceAction) -> MCPResourceContent:
        """Read a resource from an MCP server."""
        await self.ensure_initialized()
        
        if not self._enabled:
            raise ToolError("MCP service is disabled")
        
        try:
            # Apply rate limiting
            await self.rate_limit(f"mcp_read_resource_{params.server_name}")

            # FIX (Dec 31, 2025): Block reading from non-allowed servers
            if self.requested_servers and params.server_name not in self.requested_servers:
                raise ToolError(
                    f"Server '{params.server_name}' is not enabled for this session. "
                    f"Enabled servers: {', '.join(self.requested_servers)}"
                )

            # Check cache first if enabled
            cache_key = f"mcp_resource_{params.server_name}_{params.resource_uri}"
            if self.cache_manager and self.mcp_config.enable_resource_caching:
                cached_content = await self.cache_manager.get(cache_key)
                if cached_content:
                    self.logger.debug(f"Resource '{params.resource_uri}' served from cache")
                    return MCPResourceContent(**cached_content)
            
            # Read the resource
            content = await self.server_manager.read_resource(
                params.server_name,
                params.resource_uri
            )
            
            # Create response
            resource_content = MCPResourceContent(
                uri=params.resource_uri,
                content=content,
                last_modified=time.time()
            )
            
            # Cache the result if enabled
            if self.cache_manager and self.mcp_config.enable_resource_caching:
                await self.cache_manager.set(
                    cache_key,
                    resource_content.model_dump(),
                    ttl=self.mcp_config.cache_ttl_seconds
                )
            
            self.logger.info(f"Resource '{params.resource_uri}' read successfully from server '{params.server_name}'")
            
            return resource_content
            
        except Exception as e:
            self.logger.error(f"Resource read failed: {e}")
            raise ToolError(f"Failed to read resource: {e}")

    # MCP Discovery Operations
    
    @BaseTool.action(
        'List available tools from MCP servers',
        param_model=MCPListToolsAction
    )
    async def list_tools(self, params: MCPListToolsAction) -> List[MCPToolInfo]:
        """List available tools from MCP servers."""
        await self.ensure_initialized()

        if not self._enabled:
            return []

        try:
            # Apply rate limiting
            await self.rate_limit("mcp_list_tools")

            all_tools = self.server_manager.get_all_tools()
            tools = []

            # FIX (Dec 31, 2025): Apply requested_servers filter if set
            # This ensures only UI-selected MCP servers are visible to the agent
            if self.requested_servers:
                all_tools = {
                    name: tools_list for name, tools_list in all_tools.items()
                    if name in self.requested_servers
                }
                self.logger.debug(f"Filtered tools by requested_servers: {list(all_tools.keys())}")

            for server_name, server_tools in all_tools.items():
                # Filter by server if specified (case-insensitive)
                if params.server_name and server_name.lower() != params.server_name.lower():
                    continue

                for tool in server_tools:
                    tools.append(MCPToolInfo(
                        name=tool.name,
                        description=tool.description,
                        server_name=tool.server_name,
                        input_schema=tool.input_schema
                    ))

            self.logger.info(f"Listed {len(tools)} tools from MCP servers")
            
            # Format tools for agent visibility with full parameter schemas
            if tools:
                self.logger.debug("Tool schemas being returned to agent:")
                for tool in tools[:3]:  # Log first 3 for debugging
                    self.logger.debug(f"  {tool.name}: {list(tool.input_schema.get('properties', {}).keys())}")
            
            return tools
            
        except Exception as e:
            self.logger.error(f"Failed to list tools: {e}")
            raise ToolError(f"Failed to list tools: {e}")

    @BaseTool.action(
        'List available resources from MCP servers',
        param_model=MCPListResourcesAction
    )
    async def list_resources(self, params: MCPListResourcesAction) -> List[MCPResourceInfo]:
        """List available resources from MCP servers."""
        await self.ensure_initialized()

        if not self._enabled:
            return []

        try:
            # Apply rate limiting
            await self.rate_limit("mcp_list_resources")

            all_resources = self.server_manager.get_all_resources()
            resources = []

            # FIX (Dec 31, 2025): Apply requested_servers filter if set
            if self.requested_servers:
                all_resources = {
                    name: res_list for name, res_list in all_resources.items()
                    if name in self.requested_servers
                }

            for server_name, server_resources in all_resources.items():
                # Filter by server if specified (case-insensitive)
                if params.server_name and server_name.lower() != params.server_name.lower():
                    continue

                for resource in server_resources:
                    resources.append(MCPResourceInfo(
                        uri=resource.uri,
                        name=resource.name,
                        server_name=resource.server_name,
                        description=resource.description,
                        mime_type=resource.mime_type
                    ))

            self.logger.info(f"Listed {len(resources)} resources from MCP servers")
            return resources

        except Exception as e:
            self.logger.error(f"Failed to list resources: {e}")
            raise ToolError(f"Failed to list resources: {e}")

    # MCP Server Management Operations
    
    @BaseTool.action(
        'List MCP servers and their status',
        param_model=MCPListServersAction
    )
    async def list_servers(self, params: MCPListServersAction) -> List[MCPServerInfo]:
        """List MCP servers and their status."""
        await self.ensure_initialized()
        
        if not self._enabled:
            return []
        
        try:
            servers_info = await self.server_manager.list_servers()
            servers = []

            for server_info in servers_info:
                # FIX (Dec 31, 2025): Apply requested_servers filter if set
                server_name = server_info.get('name', '')
                if self.requested_servers and server_name not in self.requested_servers:
                    continue

                # Filter disabled servers if requested
                if not params.include_disabled and not server_info.get('enabled', False):
                    continue
                
                # Create server info object
                if params.include_details:
                    servers.append(MCPServerInfo(**server_info))
                else:
                    # Basic info only
                    servers.append(MCPServerInfo(
                        name=server_info['name'],
                        status=server_info['status'],
                        type=server_info['type'],
                        enabled=server_info['enabled']
                    ))

            return servers
            
        except Exception as e:
            self.logger.error(f"Failed to list servers: {e}")
            raise ToolError(f"Failed to list servers: {e}")

    @BaseTool.action(
        'Get status of a specific MCP server',
        param_model=MCPServerStatusAction
    )
    async def get_server_status(self, params: MCPServerStatusAction) -> MCPServerInfo:
        """Get status of a specific MCP server."""
        await self.ensure_initialized()

        if not self._enabled:
            raise ToolError("MCP service is disabled")

        try:
            server_info = await self.server_manager.get_server_info(params.server_name)
            if not server_info:
                raise ToolError(f"Server '{params.server_name}' not found")

            return MCPServerInfo(**server_info)

        except ToolError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to get server status: {e}")
            raise ToolError(f"Failed to get server status: {e}")

    @BaseTool.action(
        'Connect to an MCP server',
        param_model=MCPConnectServerAction
    )
    async def connect_server(self, params: MCPConnectServerAction) -> Dict[str, Any]:
        """Connect to an MCP server."""
        await self.ensure_initialized()

        if not self._enabled:
            return {"success": False, "error": "MCP service is disabled"}

        try:
            success = await self.server_manager.connect_server(params.server_name)

            if success:
                self.logger.info(f"Successfully connected to server '{params.server_name}'")
                return {"success": True, "message": f"Connected to server '{params.server_name}'"}
            else:
                return {"success": False, "error": f"Failed to connect to server '{params.server_name}'"}

        except Exception as e:
            self.logger.error(f"Error connecting to server: {e}")
            return {"success": False, "error": str(e)}

    @BaseTool.action(
        'Disconnect from an MCP server',
        param_model=MCPDisconnectServerAction
    )
    async def disconnect_server(self, params: MCPDisconnectServerAction) -> Dict[str, Any]:
        """Disconnect from an MCP server."""
        await self.ensure_initialized()

        if not self._enabled:
            return {"success": False, "error": "MCP service is disabled"}

        try:
            success = await self.server_manager.disconnect_server(params.server_name)

            if success:
                self.logger.info(f"Successfully disconnected from server '{params.server_name}'")
                return {"success": True, "message": f"Disconnected from server '{params.server_name}'"}
            else:
                return {"success": False, "error": f"Failed to disconnect from server '{params.server_name}'"}

        except Exception as e:
            self.logger.error(f"Error disconnecting from server: {e}")
            return {"success": False, "error": str(e)}

    @BaseTool.action(
        'Reload an MCP server connection',
        param_model=MCPReloadServerAction
    )
    async def reload_server(self, params: MCPReloadServerAction) -> Dict[str, Any]:
        """Reload an MCP server connection."""
        await self.ensure_initialized()

        if not self._enabled:
            return {"success": False, "error": "MCP service is disabled"}

        try:
            # Disconnect and reconnect
            await self.server_manager.disconnect_server(params.server_name)
            await asyncio.sleep(1)  # Brief pause
            success = await self.server_manager.connect_server(params.server_name)

            if success:
                self.logger.info(f"Successfully reloaded server '{params.server_name}'")
                return {"success": True, "message": f"Reloaded server '{params.server_name}'"}
            else:
                return {"success": False, "error": f"Failed to reload server '{params.server_name}'"}

        except Exception as e:
            self.logger.error(f"Error reloading server: {e}")
            return {"success": False, "error": str(e)}

    @BaseTool.action(
        'Get capabilities of an MCP server',
        param_model=MCPGetCapabilitiesAction
    )
    async def get_capabilities(self, params: MCPGetCapabilitiesAction) -> Dict[str, Any]:
        """Get capabilities of an MCP server."""
        await self.ensure_initialized()

        if not self._enabled:
            raise ToolError("MCP service is disabled")

        try:
            server_info = await self.server_manager.get_server_info(params.server_name)
            if not server_info:
                raise ToolError(f"Server '{params.server_name}' not found")

            return {
                "server_name": params.server_name,
                "capabilities": server_info.get('capabilities', {}),
                "tools_count": server_info.get('tools_count', 0),
                "resources_count": server_info.get('resources_count', 0)
            }

        except ToolError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to get capabilities: {e}")
            raise ToolError(f"Failed to get capabilities: {e}")

    @BaseTool.action(
        'Perform health check on MCP servers',
        param_model=MCPHealthCheckAction
    )
    async def health_check(self, params: MCPHealthCheckAction) -> MCPHealthStatus:
        """Perform health check on MCP servers."""
        await self.ensure_initialized()
        
        if not self._enabled:
            return MCPHealthStatus(overall_health="disabled")
        
        try:
            servers_info = await self.server_manager.list_servers()
            
            healthy_servers = []
            unhealthy_servers = []
            
            for server_info in servers_info:
                # Filter by server if specified
                if params.server_name and server_info['name'] != params.server_name:
                    continue
                
                if server_info['status'] == ServerStatus.CONNECTED.value:
                    healthy_servers.append(server_info['name'])
                else:
                    unhealthy_servers.append(server_info['name'])
            
            total_servers = len(healthy_servers) + len(unhealthy_servers)
            
            # Determine overall health
            if total_servers == 0:
                overall_health = "no_servers"
            elif len(unhealthy_servers) == 0:
                overall_health = "healthy"
            elif len(healthy_servers) == 0:
                overall_health = "unhealthy"
            else:
                overall_health = "degraded"
            
            return MCPHealthStatus(
                healthy_servers=healthy_servers,
                unhealthy_servers=unhealthy_servers,
                total_servers=total_servers,
                overall_health=overall_health
            )
            
        except Exception as e:
            self.logger.error(f"Health check failed: {e}")
            raise ToolError(f"Health check failed: {e}")

    # Resource subscriptions (Item 7F) — resources/subscribe + notifications/resources/updated

    async def _invalidate_resource_cache(self, server: str, uri: str) -> None:
        """Resource-update callback: actually evict the cached resource (UP-01 Item 4).

        The cache is owned here on MCPTool (key ``mcp_resource_{server}_{uri}``), so the
        eviction must run from here — MCPServerManager's default callback can only log.
        """
        if self.cache_manager and getattr(self.mcp_config, "enable_resource_caching", False):
            try:
                await self.cache_manager.delete(f"mcp_resource_{server}_{uri}")
            except Exception as e:
                self.logger.debug(f"resource cache evict skipped server={server} uri={uri}: {e}")
        self.logger.info(f"mcp.resource.updated server={server} uri={uri} (cache evicted)")

    @BaseTool.action(
        'Subscribe to MCP resource updates',
        param_model=MCPSubscribeResourceAction
    )
    async def subscribe_resource(self, params: MCPSubscribeResourceAction) -> Dict[str, Any]:
        """Subscribe to updates for an MCP resource (callback evicts this tool's cache)."""
        await self.ensure_initialized()
        # C5: same tenant guard every other MCP action enforces — a session may only
        # touch servers in its own allowlist (mutating a subscription reaches the
        # server's live connection).
        if not self._enabled:
            raise ToolError("MCP service is disabled")
        if self.requested_servers and params.server_name not in self.requested_servers:
            raise ToolError(
                f"Server '{params.server_name}' is not enabled for this session. "
                f"Enabled servers: {', '.join(self.requested_servers)}"
            )
        try:
            return await self.server_manager.subscribe_resource(
                params.server_name, params.resource_uri,
                callback=self._invalidate_resource_cache,
            )
        except Exception as e:
            self.logger.error(f"Resource subscribe failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "server_name": params.server_name,
                "resource_uri": params.resource_uri,
            }

    @BaseTool.action(
        'Unsubscribe from MCP resource updates',
        param_model=MCPUnsubscribeResourceAction
    )
    async def unsubscribe_resource(self, params: MCPUnsubscribeResourceAction) -> Dict[str, Any]:
        """Unsubscribe from updates for an MCP resource."""
        await self.ensure_initialized()
        # C5: same tenant guard as subscribe_resource — a session must not be able to
        # tear down a subscription on a server outside its allowlist.
        if not self._enabled:
            raise ToolError("MCP service is disabled")
        if self.requested_servers and params.server_name not in self.requested_servers:
            raise ToolError(
                f"Server '{params.server_name}' is not enabled for this session. "
                f"Enabled servers: {', '.join(self.requested_servers)}"
            )
        try:
            return await self.server_manager.unsubscribe_resource(
                params.server_name, params.resource_uri
            )
        except Exception as e:
            self.logger.error(f"Resource unsubscribe failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "server_name": params.server_name,
                "resource_uri": params.resource_uri,
            }

    def has_server(self, server_name: str) -> bool:
        """Check if a specific MCP server is connected.

        Args:
            server_name: Name of the server to check

        Returns:
            True if server is connected, False otherwise
        """
        self.logger.debug(f"🔍 has_server('{server_name}') called")
        
        if not self.server_manager:
            self.logger.debug(f"  ❌ No server_manager")
            return False

        servers = self.server_manager.get_all_tools()
        self.logger.debug(f"  📋 Available servers: {list(servers.keys())}")
        
        result = server_name in servers
        self.logger.debug(f"  Result: {result}")
        
        return result

    def _enforce_exec_rate_limit(self, server_name: str, execution_context=None) -> Optional[str]:
        """Per-(user, server) MCP exec rate limit (WS-B3), shared by both entry points.

        Returns an LLM-facing error string when the caller is rate-limited, else None.
        Identity resolves from the execution context, falling back to the loaded user
        context / "global" (the flattened {server}_{tool} path carries no execution
        context, so it relies on self._current_user_id, which set_user_context populates).
        """
        uid = getattr(execution_context, "user_id", None) or self._current_user_id or "global"
        if not self._exec_rate_limiter.check((uid, server_name)):
            retry = self._exec_rate_limiter.retry_after((uid, server_name))
            self.logger.warning(
                f"⏳ MCP exec rate limit hit for user={uid} server={server_name} "
                f"(retry in ~{retry:.0f}s)"
            )
            return (
                f"Rate limit reached for MCP server '{server_name}'. "
                f"Retry in ~{retry:.0f}s before calling more tools on this server."
            )
        return None

    async def execute_mcp_tool(
        self,
        action_name: str,
        params: Dict[str, Any],
        execution_context=None,
    ) -> Any:
        """Execute an MCP tool by action name.

        This is called by the controller when routing MCP tool calls.
        Routes through ``_execute_validated`` so it gets the same
        allowlist / schema-validation / truncation treatment as ``execute_tool``.

        Args:
            action_name: Namespaced action name (e.g., "anysite_search", "ghost_publish")
            params: Tool parameters

        Returns:
            The processed result string (JSON-serialized, size-capped via
            process_tool_result) — unwrapped from MCPExecutionResult.
            (T0.4: the live controller path now receives the same validation +
            truncation the action path always had.)

        Raises:
            ToolError: If tool execution fails or validation rejects the call
        """
        # T0.4: behavior change — result is now always a processed JSON string; needs a live smoke test
        # Parse action name more robustly
        # Format: {server}_{toolname}
        # Need to identify which part is server vs tool

        # Check if server manager is available
        if not self.server_manager:
            raise ToolError("Server manager not initialized")

        # Get all available servers
        all_tools = self.server_manager.get_all_tools()
        server_name = None
        tool_name = None

        # Try to match against known servers
        for srv_name in all_tools.keys():
            if action_name.startswith(f"{srv_name}_"):
                server_name = srv_name
                tool_name = action_name[len(srv_name) + 1:]  # Everything after "{server}_"
                break

        if not server_name or not tool_name:
            raise ToolError(
                f"Cannot parse MCP action name: {action_name}. "
                f"Expected format: {{server}}_{{toolname}}. "
                f"Available servers: {list(all_tools.keys())}"
            )

        self.logger.info(
            f"🔧 Executing MCP tool: {tool_name} on server {server_name}"
        )
        self.logger.debug(f"   Parameters (raw): {params}")

        # GROK 4.1 FIX (Dec 2025): Deep parse parameters to handle nested JSON strings
        # Some LLMs return nested objects as JSON strings instead of parsed objects
        params = self._deep_parse_params(params)
        self.logger.debug(f"   Parameters (parsed): {params}")

        # Route through the shared validated-execute core (T0.4).
        # _execute_validated handles: rate limit, allowlist, empty-args detection,
        # schema validation/coercion, execution, truncation.
        outcome = await self._execute_validated(
            server_name,
            tool_name,
            params,
            execution_context=execution_context,
        )

        if not outcome.success:
            self.logger.error(f"❌ MCP tool execution failed: {outcome.error}")
            raise ToolError(outcome.error or f"Failed to execute MCP tool {action_name}")

        self.logger.info(f"✅ MCP tool {action_name} executed successfully")
        return outcome.result

    # Parameter Validation Methods

    def _deep_parse_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively parse JSON strings within nested parameter structures.
        
        GROK 4.1 / MCP FIX (Dec 2025):
        Some LLMs (notably Grok 4.1 via OpenRouter) return nested object/array 
        parameters as JSON strings instead of parsed objects. For example:
          {"authors": "[\"a\", \"b\"]", "filters": "{\"key\": \"val\"}"}
        instead of:
          {"authors": ["a", "b"], "filters": {"key": "val"}}
        
        This function recursively finds and parses those strings before
        passing to the MCP server.
        
        Args:
            params: Parameters dict that may contain stringified nested JSON
        
        Returns:
            Parameters dict with all nested JSON strings parsed
        """
        import json
        
        def _parse_recursive(obj: Any, depth: int = 0, max_depth: int = 10) -> Any:
            """Inner recursive parser with depth limit."""
            if depth > max_depth:
                self.logger.warning(f"[DEEP_PARSE] Max depth {max_depth} reached")
                return obj
            
            if isinstance(obj, str):
                # Check if this string looks like JSON (starts with { or [)
                stripped = obj.strip()
                if stripped and (stripped.startswith('{') or stripped.startswith('[')):
                    try:
                        parsed = json.loads(stripped)
                        self.logger.debug(f"[DEEP_PARSE] Parsed nested JSON: {stripped[:80]}...")
                        # Recursively process (may contain more JSON strings)
                        return _parse_recursive(parsed, depth + 1, max_depth)
                    except (json.JSONDecodeError, ValueError):
                        return obj
                return obj
            
            elif isinstance(obj, dict):
                return {k: _parse_recursive(v, depth + 1, max_depth) for k, v in obj.items()}
            
            elif isinstance(obj, list):
                return [_parse_recursive(item, depth + 1, max_depth) for item in obj]
            
            return obj
        
        return _parse_recursive(params)

    def _validate_and_convert_parameters(
        self,
        schema: Dict[str, Any],
        parameters: Dict[str, Any],
        tool_name: str
    ) -> Tuple[Dict[str, Any], List[str]]:
        """Validate and auto-convert parameters against schema.

        Thin delegation wrapper — pure logic lives in
        ``tools.mcp.param_coercion.coerce_arguments``.

        Args:
            schema: JSON Schema for tool parameters
            parameters: Parameters to validate
            tool_name: Name of tool (for error messages)

        Returns:
            Tuple of (converted_params, validation_errors)
        """
        return _coerce_arguments(schema, parameters, tool_name, logger=self.logger)