"""MCP server connection manager."""

import asyncio
import json
import logging
import os
import time
from typing import Dict, List, Optional, Any, Set, Tuple
from enum import Enum
from dataclasses import dataclass, field

from core.exceptions import ServiceError, ConfigurationError, MCPError, MCPConnectionError, MCPProtocolError, MCPToolExecutionError
from core.logging import get_component_logger
from .config import MCPServerConfig, MCPServerType, resolve_environment_variables
from .subscriptions import ResourceSubscriptionRegistry
from .protocol import MCPClient, MCPStdioTransport, MCPSSETransport, MCPHTTPTransport, MCPStreamableHTTPTransport
from utils.circuit_breaker import CircuitBreaker, get_circuit_breaker_registry, CircuitBreakerError


class ServerStatus(str, Enum):
    """MCP server connection status."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    RECONNECTING = "reconnecting"


@dataclass
class MCPToolMetadata:
    """Metadata for an MCP tool (internal use)."""
    name: str  # Canonical name shown to LLM (always {server}_{tool} format)
    description: str
    input_schema: Dict[str, Any]
    server_name: str
    server_tool_name: str = ""  # Actual name the server expects (may differ from canonical name)


@dataclass
class MCPResource:
    """Represents an MCP resource."""
    uri: str
    name: str
    description: Optional[str]
    mime_type: Optional[str]
    server_name: str


@dataclass
class ServerConnection:
    """Represents a connection to an MCP server."""
    name: str
    config: MCPServerConfig
    status: ServerStatus = ServerStatus.DISCONNECTED
    client: Optional[MCPClient] = None
    circuit_breaker: Optional[CircuitBreaker] = None
    last_error: Optional[str] = None
    connected_at: Optional[float] = None
    retry_count: int = 0
    tools: List[MCPToolMetadata] = field(default_factory=list)
    resources: List[MCPResource] = field(default_factory=list)
    capabilities: Dict[str, Any] = field(default_factory=dict)
    # NOTE: Message processing is handled internally by MCPClient (client._message_handler_task)


class MCPServerManager:
    """Manages connections to multiple MCP servers."""
    
    def __init__(
        self,
        global_timeout: int = 60,
        max_concurrent: int = 10,
        max_per_user: int = 5,  # FIX #13: Per-user connection limit
    ):
        """Initialize server manager.

        Args:
            global_timeout: Global timeout for operations
            max_concurrent: Maximum concurrent connections (global)
            max_per_user: Maximum connections per user (FIX #13)
        """
        self.logger = get_component_logger(self.__class__.__name__)
        self.global_timeout = global_timeout
        self.max_concurrent = max_concurrent
        self.max_per_user = max_per_user  # FIX #13
        self.connections: Dict[str, ServerConnection] = {}
        self.circuit_breaker_registry = get_circuit_breaker_registry()
        self._lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()
        self._health_check_task: Optional[asyncio.Task] = None
        # Item 7F: (server, uri) -> resource-update callbacks.
        self._subscriptions = ResourceSubscriptionRegistry(self.logger)
    
    def count_user_connections(self, user_id: str) -> int:
        """Count active connections for a specific user (FIX #13)."""
        prefix = f"user_{user_id}::"
        return sum(
            1 for name, conn in self.connections.items()
            if name.startswith(prefix) and conn.status == ServerStatus.CONNECTED
        )
    
    def can_add_user_connection(self, user_id: str) -> bool:
        """Check if user can add more connections (FIX #13)."""
        return self.count_user_connections(user_id) < self.max_per_user
        
    async def start(self) -> None:
        """Start the server manager."""
        self.logger.info("Starting MCP server manager")
        self._shutdown_event.clear()
        
        # Start health check task
        self._health_check_task = asyncio.create_task(self._health_check_loop())
        
    async def stop(self) -> None:
        """Stop the server manager and cleanup all connections."""
        self.logger.info("Stopping MCP server manager")
        self._shutdown_event.set()
        
        # Stop health check task
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
        
        # Disconnect all servers
        await self.disconnect_all_servers()
        
    async def add_server(self, name: str, config: MCPServerConfig) -> bool:
        """Add and connect to a new MCP server.
        
        Args:
            name: Server name
            config: Server configuration
            
        Returns:
            True if server was added and connected successfully
        """
        async with self._lock:
            if name in self.connections:
                self.logger.warning(f"Server '{name}' already exists")
                return False
            
            # Check concurrent connection limit
            active_connections = sum(1 for conn in self.connections.values() 
                                   if conn.status == ServerStatus.CONNECTED)
            if active_connections >= self.max_concurrent:
                self.logger.error(f"Maximum concurrent connections ({self.max_concurrent}) reached")
                return False
            
            # FIX #13: Check per-user connection limit for user servers
            if name.startswith("user_") and "::" in name:
                user_id = name.split("::")[0].replace("user_", "")
                if not self.can_add_user_connection(user_id):
                    self.logger.error(
                        f"User {user_id} has reached maximum connections ({self.max_per_user})"
                    )
                    return False
            
            # Create circuit breaker for this server
            circuit_breaker = self.circuit_breaker_registry.get_or_create(
                f"mcp_server_{name}",
                failure_threshold=config.retry_attempts if hasattr(config, 'retry_attempts') else 5,
                recovery_timeout=config.retry_delay if hasattr(config, 'retry_delay') else 60.0
            )
            
            connection = ServerConnection(name=name, config=config, circuit_breaker=circuit_breaker)
            self.connections[name] = connection
            
            if config.enabled:
                return await self._connect_server(connection)
            
            self.logger.info(f"Server '{name}' added but disabled")
            return True
    
    async def remove_server(self, name: str) -> bool:
        """Remove and disconnect from an MCP server.
        
        Args:
            name: Server name
            
        Returns:
            True if server was removed successfully
        """
        async with self._lock:
            if name not in self.connections:
                self.logger.warning(f"Server '{name}' not found")
                return False
            
            connection = self.connections[name]
            await self._disconnect_server(connection)
            del self.connections[name]
            # H10: genuine removal (not a transient disconnect) — drop the server's
            # subscription callbacks so they don't leak for the manager's lifetime.
            self._subscriptions.clear(name)

            self.logger.info(f"Server '{name}' removed")
            return True
    
    async def connect_server(self, name: str) -> bool:
        """Connect to a specific server.
        
        Args:
            name: Server name
            
        Returns:
            True if connection was successful
        """
        async with self._lock:
            if name not in self.connections:
                self.logger.error(f"Server '{name}' not found")
                return False
            
            connection = self.connections[name]
            if connection.status == ServerStatus.CONNECTED:
                self.logger.info(f"Server '{name}' already connected")
                return True
            
            return await self._connect_server(connection)
    
    async def disconnect_server(self, name: str) -> bool:
        """Disconnect from a specific server.
        
        Args:
            name: Server name
            
        Returns:
            True if disconnection was successful
        """
        async with self._lock:
            if name not in self.connections:
                self.logger.error(f"Server '{name}' not found")
                return False
            
            connection = self.connections[name]
            await self._disconnect_server(connection)
            return True
    
    async def disconnect_all_servers(self) -> None:
        """Disconnect from all servers."""
        async with self._lock:
            tasks = []
            for connection in self.connections.values():
                if connection.status == ServerStatus.CONNECTED:
                    tasks.append(self._disconnect_server(connection))
            
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
    
    async def get_server_status(self, name: str) -> Optional[ServerStatus]:
        """Get connection status for a server.
        
        Args:
            name: Server name
            
        Returns:
            Server status or None if not found
        """
        connection = self.connections.get(name)
        return connection.status if connection else None
    
    async def get_server_info(self, name: str) -> Optional[Dict[str, Any]]:
        """Get detailed information about a server.
        
        Args:
            name: Server name
            
        Returns:
            Server information or None if not found
        """
        connection = self.connections.get(name)
        if not connection:
            return None
        
        return {
            'name': connection.name,
            'status': connection.status.value,
            'type': connection.config.type.value,
            'enabled': connection.config.enabled,
            'connected_at': connection.connected_at,
            'last_error': connection.last_error,
            'retry_count': connection.retry_count,
            'tools_count': len(connection.tools),
            'resources_count': len(connection.resources),
            'capabilities': connection.capabilities
        }
    
    async def list_servers(self) -> List[Dict[str, Any]]:
        """List all servers with their status.
        
        Returns:
            List of server information
        """
        servers = []
        for name in self.connections:
            server_info = await self.get_server_info(name)
            if server_info:
                servers.append(server_info)
        return servers
    
    def get_all_tools(self) -> Dict[str, List[MCPToolMetadata]]:
        """Get all available tools from all connected servers.

        Returns:
            Dictionary mapping server names to their tools
        """
        tools = {}
        for name, connection in self.connections.items():
            if connection.status == ServerStatus.CONNECTED:
                tools[name] = connection.tools.copy()
        return tools
    
    def get_all_resources(self) -> Dict[str, List[MCPResource]]:
        """Get all available resources from all connected servers.
        
        Returns:
            Dictionary mapping server names to their resources
        """
        resources = {}
        for name, connection in self.connections.items():
            if connection.status == ServerStatus.CONNECTED:
                resources[name] = connection.resources.copy()
        return resources
    
    async def execute_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any], timeout: Optional[float] = None) -> Any:
        """Execute a tool on a specific server.
        
        Args:
            server_name: Name of the server
            tool_name: Name of the tool to execute (can be prefixed or unprefixed)
            arguments: Tool arguments
            timeout: Optional timeout in seconds (defaults to transport timeout: 180s)
            
        Returns:
            Tool execution result
            
        Raises:
            ServiceError: If server not found, not connected, or tool execution fails
        """
        connection = self.connections.get(server_name)
        if not connection:
            raise MCPConnectionError(f"Server '{server_name}' not found")
        
        if connection.status != ServerStatus.CONNECTED or not connection.client:
            raise MCPConnectionError(f"Server '{server_name}' not connected")
        
        # SIMPLIFIED: Single lookup strategy using canonical names
        # All tools are normalized to {server}_{tool} format at discovery
        # This allows both formats: 'anysite_search' or 'search'
        canonical_name = tool_name if tool_name.startswith(f"{server_name}_") else f"{server_name}_{tool_name}"

        tool = None
        for t in connection.tools:
            if t.name == canonical_name:
                tool = t
                break

        if not tool:
            # Clear error with available tools
            available_tools = [t.name for t in connection.tools]

            error_msg = (
                f"❌ Tool '{tool_name}' (canonical: '{canonical_name}') not found on server '{server_name}'.\n\n"
                f"Available tools ({len(available_tools)}):\n"
            )

            for tool_item in available_tools[:10]:  # Show first 10
                error_msg += f"  - {tool_item}\n"

            if len(available_tools) > 10:
                error_msg += f"  ... and {len(available_tools) - 10} more"

            self.logger.error(error_msg)
            raise MCPToolExecutionError(error_msg)
        
        # Execute with circuit breaker protection
        try:
            # Log execution details
            pending_count = len(connection.client._pending_requests) if connection.client else 0
            loop_healthy = connection.client.is_message_loop_healthy if connection.client else False
            self.logger.info(
                f"📋 execute_tool: server={server_name}, "
                f"requested='{tool_name}', canonical='{canonical_name}', "
                f"server_expects='{tool.server_tool_name}', "
                f"timeout={timeout}, pending_reqs={pending_count}, loop_healthy={loop_healthy}"
            )

            # Use server_tool_name (the EXACT name the server expects)
            async def _execute():
                return await connection.client.execute_tool(tool.server_tool_name, arguments, timeout=timeout)

            if connection.circuit_breaker:
                result = await connection.circuit_breaker.call(_execute)
            else:
                result = await _execute()

            self.logger.info(f"✅ execute_tool completed: server={server_name}, tool={canonical_name} → {tool.server_tool_name}")
            return result

        except CircuitBreakerError:
            raise MCPConnectionError(f"Server '{server_name}' is currently unavailable (circuit breaker open)")
        except Exception as e:
            self.logger.error(f"❌ Tool execution failed on '{server_name}.{tool_name}': {e}")
            raise MCPToolExecutionError(f"Tool execution failed: {e}")
    
    async def read_resource(self, server_name: str, resource_uri: str) -> Any:
        """Read a resource from a specific server.
        
        Args:
            server_name: Name of the server
            resource_uri: URI of the resource to read
            
        Returns:
            Resource content
            
        Raises:
            ServiceError: If server not found, not connected, or resource read fails
        """
        connection = self.connections.get(server_name)
        if not connection:
            raise MCPConnectionError(f"Server '{server_name}' not found")
        
        if connection.status != ServerStatus.CONNECTED or not connection.client:
            raise MCPConnectionError(f"Server '{server_name}' not connected")
        
        # Execute with circuit breaker protection
        try:
            async def _read():
                return await connection.client.read_resource(resource_uri)
            
            if connection.circuit_breaker:
                result = await connection.circuit_breaker.call(_read)
            else:
                result = await _read()
            
            self.logger.info(f"Successfully read resource '{resource_uri}' from server '{server_name}'")
            return result
                
        except CircuitBreakerError:
            raise MCPConnectionError(f"Server '{server_name}' is currently unavailable (circuit breaker open)")
        except Exception as e:
            self.logger.error(f"Resource read failed: {e}")
            raise MCPProtocolError(f"Resource read failed: {e}")

    # ===== Resource subscriptions (Item 7F) =====

    def _make_client_handler(self, server_name: str):
        """Build the per-server notification handler the client invokes on update."""
        async def _handler(uri: str):
            await self.handle_resource_updated(server_name, uri)
        return _handler

    def _default_resource_callback(self, server: str, uri: str) -> None:
        """Default subscription callback when no cache-owning callback was supplied.

        The resource cache lives on MCPTool, not here, so this manager-level default
        can only emit telemetry — it does NOT claim to invalidate anything. Actual
        eviction is wired by MCPTool.subscribe_resource (UP-01 Item 4)."""
        self.logger.info(f"mcp.resource.updated server={server} uri={uri} (no cache bound here)")

    async def subscribe_resource(self, server_name: str, resource_uri: str, callback=None) -> Dict[str, Any]:
        """Subscribe to ``resource_uri`` on ``server_name`` (sends resources/subscribe).

        Routes the server's ``notifications/resources/updated`` to ``callback`` (default:
        invalidate cache + telemetry). Returns a success dict.
        """
        connection = self.connections.get(server_name)
        if not connection:
            raise MCPConnectionError(f"Server '{server_name}' not found")
        if connection.status != ServerStatus.CONNECTED or not connection.client:
            raise MCPConnectionError(f"Server '{server_name}' not connected")

        # Ensure the client routes resource-updated notifications back to us.
        connection.client._resource_update_handler = self._make_client_handler(server_name)
        await connection.client.subscribe_resource(resource_uri)
        self._subscriptions.subscribe(server_name, resource_uri, callback or self._default_resource_callback)
        self.logger.info(f"Subscribed to resource '{resource_uri}' on server '{server_name}'")
        return {"success": True, "server_name": server_name, "resource_uri": resource_uri}

    async def unsubscribe_resource(self, server_name: str, resource_uri: str) -> Dict[str, Any]:
        """Unsubscribe from ``resource_uri`` (sends resources/unsubscribe) + drop callbacks."""
        connection = self.connections.get(server_name)
        if connection and connection.status == ServerStatus.CONNECTED and connection.client:
            try:
                await connection.client.unsubscribe_resource(resource_uri)
            except Exception as e:
                self.logger.warning(f"resources/unsubscribe failed for '{resource_uri}': {e}")
        self._subscriptions.unsubscribe(server_name, resource_uri)
        self.logger.info(f"Unsubscribed from resource '{resource_uri}' on server '{server_name}'")
        return {"success": True, "server_name": server_name, "resource_uri": resource_uri}

    async def handle_resource_updated(self, server_name: str, resource_uri: str) -> int:
        """Dispatch a server-side resource update to registered callbacks. Returns count."""
        return await self._subscriptions.dispatch(server_name, resource_uri)

    async def _restore_subscriptions(self, connection) -> None:
        """Re-wire the resource-update handler and re-send resources/subscribe for every
        uri still tracked for this server (H10 — after a (re)connect). Fail-open per uri.
        """
        uris = self._subscriptions.uris_for(connection.name)
        if not uris or not connection.client:
            return
        connection.client._resource_update_handler = self._make_client_handler(connection.name)
        for uri in uris:
            try:
                await connection.client.subscribe_resource(uri)
                self.logger.info(f"Re-subscribed resource '{uri}' on server '{connection.name}'")
            except Exception as e:
                self.logger.warning(
                    f"resource re-subscribe failed server='{connection.name}' uri='{uri}': {e}"
                )

    async def _connect_server(self, connection: ServerConnection) -> bool:
        """Connect to a specific server (internal).
        
        Args:
            connection: Server connection object
            
        Returns:
            True if connection was successful
        """
        try:
            connection.status = ServerStatus.CONNECTING
            self.logger.info(
                f"🔌 Connecting to MCP server '{connection.name}' "
                f"(type={connection.config.type.value}, timeout={connection.config.timeout}s)"
            )
            
            # SECURITY (SSRF/DNS rebinding): user-registered servers must be
            # validated+pinned at connect time. Trusted operator-configured
            # global servers (anysite/dev) keep legacy behavior.
            is_user_server = connection.name.startswith("user_") and "::" in connection.name

            # Create transport based on server type
            if connection.config.type == MCPServerType.STDIO:
                self.logger.debug(f"Creating STDIO transport for '{connection.name}'")
                transport = MCPStdioTransport(
                    command=connection.config.command + (connection.config.args or []),
                    env={env_var.name: env_var.value for env_var in connection.config.env} if connection.config.env else None,
                    timeout=connection.config.timeout
                )
            elif connection.config.type == MCPServerType.SSE:
                self.logger.debug(f"Creating SSE transport for '{connection.name}' at {connection.config.url}")
                transport = MCPSSETransport(
                    url=connection.config.url,
                    headers=connection.config.headers,
                    timeout=connection.config.timeout,
                    message_endpoint=getattr(connection.config, 'message_endpoint', None),  # FIX #7
                    validate_ssrf=is_user_server
                )
            elif connection.config.type == MCPServerType.HTTP:
                self.logger.debug(f"Creating HTTP JSON-RPC transport for '{connection.name}' at {connection.config.url}")
                transport = MCPHTTPTransport(
                    url=connection.config.url,
                    headers=connection.config.headers,
                    timeout=connection.config.timeout,
                    validate_ssrf=is_user_server
                )
            elif connection.config.type == MCPServerType.STREAMABLE_HTTP:
                self.logger.debug(f"Creating Streamable HTTP transport for '{connection.name}' at {connection.config.url}")
                transport = MCPStreamableHTTPTransport(
                    url=connection.config.url,
                    headers=connection.config.headers,
                    timeout=connection.config.timeout,
                    validate_ssrf=is_user_server
                )
            else:
                raise MCPConnectionError(f"Unsupported server type: {connection.config.type}")

            # Create MCP client and connect
            connection.client = MCPClient(transport)
            self.logger.debug(f"Initiating connection for '{connection.name}'...")
            await connection.client.connect()

            # NOTE: Message processing loop is started automatically by MCPClient.connect()
            # No need to start it again here (was causing duplicate loops)

            # Update connection status
            connection.status = ServerStatus.CONNECTED
            connection.connected_at = time.time()
            connection.retry_count = 0
            connection.last_error = None
            
            # Update tools and resources from client
            await self._update_server_capabilities(connection)
            
            # Wait a bit more if no tools discovered yet but server supports them
            if not connection.tools and "tools" in connection.capabilities:
                self.logger.info(f"⏳ Waiting for tools discovery for server '{connection.name}'...")
                await asyncio.sleep(2)  # Give extra time for async tool discovery
                await self._update_server_capabilities(connection)
            
            self.logger.info(
                f"✅ Successfully connected to server '{connection.name}' "
                f"({len(connection.tools)} tools, {len(connection.resources)} resources)"
            )

            # H10: re-establish any resource subscriptions for this server. Subscriptions
            # survive a transient disconnect (reconnect goes through _disconnect_server,
            # which no longer drops them); a brand-new MCPClient has no handler wired and
            # the server has no record of prior subscriptions, so without this updates
            # silently stop after the first reconnect. No-op on a fresh connect.
            await self._restore_subscriptions(connection)

            return True
                
        except MCPConnectionError as e:
            # Connection-specific errors (already logged by transport)
            self.logger.error(
                f"❌ Connection failed for server '{connection.name}': {e}"
            )
            connection.status = ServerStatus.ERROR
            connection.last_error = str(e)
            
            # Clean up partial connection
            if connection.client:
                try:
                    await connection.client.close()
                except Exception:
                    pass
                connection.client = None

            return False
            
        except Exception as e:
            # Unexpected errors
            self.logger.error(
                f"❌ Unexpected error connecting to server '{connection.name}': {e}",
                exc_info=True
            )
            connection.status = ServerStatus.ERROR
            connection.last_error = str(e)
            
            # Clean up partial connection
            if connection.client:
                try:
                    await connection.client.close()
                except Exception:
                    pass
                connection.client = None

            return False

    async def _disconnect_server(self, connection: ServerConnection) -> None:
        """Disconnect from a specific server (internal).
        
        Args:
            connection: Server connection object
        """
        try:
            self.logger.info(f"Disconnecting from server '{connection.name}'")

            # Close MCP client (will handle cleanup of internal message handler)
            if connection.client:
                try:
                    await connection.client.close()
                except Exception as e:
                    self.logger.error(f"Error closing MCP client for server '{connection.name}': {e}")
                finally:
                    connection.client = None
            
            # Reset connection state
            connection.status = ServerStatus.DISCONNECTED
            connection.connected_at = None
            connection.tools.clear()
            connection.resources.clear()
            connection.capabilities.clear()
            
            self.logger.info(f"Disconnected from server '{connection.name}'")
            
        except Exception as e:
            self.logger.error(f"Error disconnecting from server '{connection.name}': {e}")
    
    # Legacy methods removed - now using proper MCP protocol implementation
    
    async def _update_server_capabilities(self, connection: ServerConnection) -> None:
        """Update server capabilities from MCP client.
        
        Args:
            connection: Server connection object
        """
        try:
            if not connection.client:
                return
            
            # Get capabilities from client
            connection.capabilities = connection.client.capabilities
            
            # Convert client tools to our format with name normalization
            connection.tools = []
            for tool in connection.client.tools:
                original_name = tool["name"]

                # CANONICAL NAME: Always use {server}_{toolname} format
                # This ensures consistent naming regardless of what the server returns
                # Add server prefix if not already present (avoid double-prefixing)
                if original_name.startswith(f"{connection.name}_"):
                    canonical_name = original_name
                else:
                    canonical_name = f"{connection.name}_{original_name}"

                # CRITICAL: server_tool_name must be the EXACT name the server returned
                # The server expects this exact name when we call tools/call
                server_tool_name = original_name

                connection.tools.append(MCPToolMetadata(
                    name=canonical_name,  # What LLM sees
                    description=tool.get("description", ""),
                    input_schema=tool.get("inputSchema", {}),
                    server_name=connection.name,
                    server_tool_name=server_tool_name  # What server expects
                ))

                self.logger.debug(
                    f"  📋 Registered: {canonical_name} "
                    f"(server expects: {server_tool_name})"
                )
            
            # Convert client resources to our format
            connection.resources = [
                MCPResource(
                    uri=resource["uri"],
                    name=resource.get("name", resource["uri"]),
                    description=resource.get("description"),
                    mime_type=resource.get("mimeType"),
                    server_name=connection.name
                )
                for resource in connection.client.resources
            ]
            
            self.logger.info(
                f"Updated capabilities for server '{connection.name}': "
                f"{len(connection.tools)} tools, {len(connection.resources)} resources"
            )
            
            # FIX: Log the actual tool names discovered for debugging
            if connection.tools:
                tool_names = [t.name for t in connection.tools]
                self.logger.info(f"  📋 Tools discovered: {tool_names}")
            
        except Exception as e:
            self.logger.error(f"Failed to update capabilities for server '{connection.name}': {e}")
    
    def get_circuit_breaker_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all circuit breakers."""
        return self.circuit_breaker_registry.get_all_status()
    
    async def reset_circuit_breaker(self, server_name: str) -> bool:
        """Reset circuit breaker for a specific server."""
        return await self.circuit_breaker_registry.reset_breaker(f"mcp_server_{server_name}")
    
    async def reset_all_circuit_breakers(self) -> None:
        """Reset all circuit breakers."""
        await self.circuit_breaker_registry.reset_all()
    
    async def _health_check_loop(self) -> None:
        """Health check loop for all servers (internal)."""
        while not self._shutdown_event.is_set():
            try:
                # Check each connected server
                for connection in list(self.connections.values()):
                    if connection.status == ServerStatus.CONNECTED:
                        await self._health_check_server(connection)
                
                # Wait before next health check
                await asyncio.sleep(30)  # Check every 30 seconds
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in health check loop: {e}")
                await asyncio.sleep(5)  # Wait a bit before retrying
    
    async def _health_check_server(self, connection: ServerConnection) -> None:
        """Perform health check on a single server (internal).
        
        Args:
            connection: Server connection object
        """
        try:
            # Check if client is connected and transport is healthy
            if connection.client and not connection.client.transport.is_closed:
                # Connection seems healthy
                return
            
            # Connection is unhealthy
            if connection.status == ServerStatus.CONNECTED:
                self.logger.warning(f"Server '{connection.name}' connection is unhealthy")
                connection.status = ServerStatus.ERROR
                connection.last_error = "Connection lost during health check"
                
                # Attempt reconnection if enabled
                if connection.config.auto_reconnect:
                    await self._attempt_reconnection(connection)
                        
        except Exception as e:
            self.logger.error(f"Health check failed for server '{connection.name}': {e}")
    
    async def _attempt_reconnection(self, connection: ServerConnection) -> None:
        """Attempt to reconnect to a failed server (internal).
        
        Args:
            connection: Server connection object
        """
        if connection.retry_count >= connection.config.retry_attempts:
            self.logger.error(f"Max retry attempts reached for server '{connection.name}'")
            return
        
        connection.retry_count += 1
        connection.status = ServerStatus.RECONNECTING
        
        self.logger.info(f"Attempting reconnection to server '{connection.name}' (attempt {connection.retry_count})")
        
        # Wait before reconnecting
        await asyncio.sleep(connection.config.retry_delay)
        
        # Cleanup existing connection
        await self._disconnect_server(connection)
        
        # Reset circuit breaker to allow reconnection
        if connection.circuit_breaker:
            await connection.circuit_breaker.reset()
        
        # Attempt to reconnect
        if await self._connect_server(connection):
            self.logger.info(f"Successfully reconnected to server '{connection.name}'")
        else:
            self.logger.error(f"Failed to reconnect to server '{connection.name}'")