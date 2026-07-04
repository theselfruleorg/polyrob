"""
Service for managing per-user MCP server configurations.

Provides high-level operations for adding, configuring, and managing
user-specific MCP servers with validation and security checks.
"""

import time
from typing import List, Optional, Dict, Any
from dataclasses import dataclass

from modules.database.user_mcp_servers import (
    UserMCPServersHandler,
    UserMCPServer,
    UserMCPSettings
)
from tools.mcp.security import MCPEncryption, MCPURLValidator, get_encryption, get_url_validator
from tools.mcp.config import MCPServerConfig, MCPServerType

from core.logging import get_component_logger

# Module-level logger for static methods
logger = get_component_logger("UserMCPService")


@dataclass
class AddServerResult:
    """Result of adding a server."""

    success: bool
    server: Optional[UserMCPServer] = None
    error: Optional[str] = None
    ready: bool = False


@dataclass
class TestConnectionResult:
    """Result of testing server connection."""

    success: bool
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    tools_discovered: Optional[int] = None
    tools: Optional[List[str]] = None  # List of tool names discovered


class RateLimiter:
    """Simple in-memory rate limiter (FIX #11)."""
    
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: Dict[str, List[float]] = {}  # user_id -> list of timestamps
    
    def check(self, user_id: str) -> bool:
        """Check if request is allowed. Returns True if allowed."""
        import time
        now = time.time()
        
        if user_id not in self._requests:
            self._requests[user_id] = []
        
        # Remove old requests outside window
        cutoff = now - self.window_seconds
        self._requests[user_id] = [t for t in self._requests[user_id] if t > cutoff]
        
        if len(self._requests[user_id]) >= self.max_requests:
            return False
        
        self._requests[user_id].append(now)
        return True
    
    def remaining(self, user_id: str) -> int:
        """Get remaining requests in window."""
        import time
        now = time.time()
        cutoff = now - self.window_seconds
        
        if user_id not in self._requests:
            return self.max_requests
        
        recent = [t for t in self._requests[user_id] if t > cutoff]
        return max(0, self.max_requests - len(recent))


class UserMCPService:
    """Service for user MCP server management."""

    def __init__(
        self,
        db_handler: UserMCPServersHandler,
        encryption: Optional[MCPEncryption] = None,
        url_validator: Optional[MCPURLValidator] = None,
        allow_http: bool = False,
        rate_limit_requests: int = 20,
        rate_limit_window: int = 60
    ):
        """
        Initialize service.

        Args:
            db_handler: Database handler for user MCP servers
            encryption: Encryption instance (uses default if not provided)
            url_validator: URL validator (uses default if not provided)
            allow_http: Allow HTTP URLs (development only)
            rate_limit_requests: Max requests per window (FIX #11)
            rate_limit_window: Rate limit window in seconds (FIX #11)
        """
        self.db = db_handler
        self.encryption = encryption or get_encryption()
        self.validator = url_validator or get_url_validator(allow_http=allow_http)
        self.logger = get_component_logger("UserMCPService")
        # FIX #11: Rate limiting
        self._rate_limiter = RateLimiter(rate_limit_requests, rate_limit_window)
    
    def _check_rate_limit(self, user_id: str) -> None:
        """Check rate limit and raise if exceeded (FIX #11)."""
        if not self._rate_limiter.check(user_id):
            remaining = self._rate_limiter.remaining(user_id)
            raise ValueError(
                f"Rate limit exceeded. Please wait before making more requests. "
                f"({remaining} requests remaining)"
            )
    
    @staticmethod
    def _sanitize_error(error: str) -> str:
        """Sanitize error message to hide internal details (FIX #14)."""
        import re
        # Remove file paths
        error = re.sub(r'/[^\s:]+\.py', '[internal]', error)
        error = re.sub(r'line \d+', '', error)
        # Remove stack trace indicators
        error = re.sub(r'Traceback.*?:', '', error, flags=re.DOTALL)
        error = re.sub(r'File "[^"]+",', '', error)
        # Remove internal module names
        error = re.sub(r'\b(tools|modules|agents)\.[a-z_.]+', '[internal]', error)
        # Limit length
        if len(error) > 200:
            error = error[:200] + '...'
        return error.strip()

    async def add_server(
        self,
        user_id: str,
        server_name: str,
        server_url: str,
        server_type: str = "sse",
        api_key: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        auth_method: str = "api_key",
        display_name: Optional[str] = None,
        verify_connection: bool = False,
        **options
    ) -> AddServerResult:
        """
        Add a new MCP server for a user.

        Performs validation and security checks before adding.

        Args:
            user_id: Owner's user ID
            server_name: Unique name for the server (lowercase, alphanumeric, hyphens)
            server_url: Server endpoint URL (must be HTTPS)
            server_type: 'sse' or 'http' (NOT 'stdio' - blocked for security)
            api_key: API key for authentication
            headers: Custom headers for requests
            auth_method: 'api_key', 'bearer', or 'none'
            display_name: Friendly display name
            verify_connection: If True, verify MCP protocol before saving (FIX #6)
            **options: Additional options (timeout, retry_attempts, etc.)

        Returns:
            AddServerResult with success status and server or error
        """
        # FIX #11: Check rate limit
        try:
            self._check_rate_limit(user_id)
        except ValueError as e:
            return AddServerResult(success=False, error=str(e))

        # Validate server type (NO STDIO for users)
        type_valid, type_error = self.validator.validate_server_type(server_type)
        if not type_valid:
            return AddServerResult(
                success=False,
                error=type_error
            )

        # Validate URL (HTTPS only, no internal IPs)
        url_valid, url_error = self.validator.validate(server_url)
        if not url_valid:
            return AddServerResult(
                success=False,
                error=f"Invalid URL: {url_error}"
            )

        # Validate server name format
        if not self._validate_server_name(server_name):
            return AddServerResult(
                success=False,
                error="Server name must be lowercase, alphanumeric with hyphens only (3-50 chars)"
            )

        # Check user's server limit
        settings = await self.db.get_user_settings(user_id)
        current_count = await self.db.server_count(user_id)

        if current_count >= settings.max_servers:
            return AddServerResult(
                success=False,
                error=f"Maximum server limit ({settings.max_servers}) reached"
            )

        # Check for duplicate name
        existing = await self.db.get_server(user_id, server_name)
        if existing:
            return AddServerResult(
                success=False,
                error=f"Server '{server_name}' already exists"
            )

        # FIX #6: Optionally verify connection before saving
        tools_discovered = 0
        if verify_connection and (api_key is not None or auth_method == 'none'):
            # Build temporary config for testing
            temp_config = MCPServerConfig(
                type=MCPServerType(server_type),
                url=server_url,
                headers=self._build_auth_headers(headers, api_key, auth_method),
                enabled=True,
                timeout=options.get('timeout', 30)
            )
            
            # Test MCP protocol
            test_result = await self._test_mcp_protocol_with_config(temp_config)
            
            if not test_result.success:
                return AddServerResult(
                    success=False,
                    error=f"Server verification failed: {test_result.error}"
                )
            
            tools_discovered = test_result.tools_discovered or 0
            self.logger.info(f"Server {server_name} verified: {tools_discovered} tools discovered")

        # Add server
        try:
            server = await self.db.add_server(
                user_id=user_id,
                server_name=server_name,
                server_url=server_url,
                server_type=server_type,
                auth_method=auth_method,
                api_key=api_key,
                headers=headers,
                display_name=display_name,
                **options
            )

            # Update tools_discovered if we verified
            if tools_discovered > 0:
                await self.db.update_connection_status(
                    user_id, server_name,
                    connected=True,
                    tools_count=tools_discovered
                )

            return AddServerResult(
                success=True,
                server=server,
                ready=api_key is not None or auth_method == 'none'
            )

        except Exception as e:
            self.logger.error(f"Failed to add server '{server_name}' for user {user_id}: {e}")
            return AddServerResult(
                success=False,
                error=f"Database error: {str(e)}"
            )

    def _build_auth_headers(
        self,
        headers: Optional[Dict[str, str]],
        api_key: Optional[str],
        auth_method: str
    ) -> Optional[Dict[str, str]]:
        """Build headers dict with authentication."""
        final_headers = dict(headers) if headers else {}
        if api_key:
            if auth_method == 'bearer':
                final_headers['Authorization'] = f'Bearer {api_key}'
            elif auth_method == 'api_key':
                final_headers['Authorization'] = f'Bearer {api_key}'
        return final_headers or None

    async def _test_mcp_protocol_with_config(
        self,
        config: MCPServerConfig
    ) -> TestConnectionResult:
        """Test MCP protocol with a config object (for verification before saving)."""
        import time
        import asyncio
        from tools.mcp.protocol import MCPClient, MCPSSETransport, MCPHTTPTransport

        client = None
        start = time.time()
        
        try:
            if config.type == MCPServerType.SSE:
                transport = MCPSSETransport(
                    url=config.url,
                    headers=config.headers,
                    timeout=min(config.timeout, 30),
                    validate_ssrf=True  # SSRF: user-supplied URL
                )
            elif config.type == MCPServerType.HTTP:
                transport = MCPHTTPTransport(
                    url=config.url,
                    headers=config.headers,
                    timeout=min(config.timeout, 30),
                    validate_ssrf=True  # SSRF: user-supplied URL
                )
            else:
                return TestConnectionResult(
                    success=False,
                    error=f"Unsupported server type: {config.type}"
                )

            client = MCPClient(transport)
            
            try:
                await asyncio.wait_for(client.connect(), timeout=30)
            except asyncio.TimeoutError:
                return TestConnectionResult(
                    success=False,
                    error="MCP handshake timed out (30s)"
                )

            latency = (time.time() - start) * 1000
            tools = client.tools
            tool_names = [t.get('name', 'unknown') for t in tools]

            return TestConnectionResult(
                success=True,
                latency_ms=round(latency, 2),
                tools_discovered=len(tools),
                tools=tool_names[:20]
            )

        except Exception as e:
            return TestConnectionResult(
                success=False,
                error=f"MCP protocol error: {str(e)}"
            )
        finally:
            if client:
                try:
                    await client.close()
                except Exception:
                    pass

    async def get_user_servers(
        self,
        user_id: str,
        enabled_only: bool = True
    ) -> List[UserMCPServer]:
        """
        Get all MCP servers for a user.

        Args:
            user_id: Owner's user ID
            enabled_only: Only return enabled servers

        Returns:
            List of UserMCPServer records
        """
        return await self.db.get_user_servers(user_id, enabled_only)

    async def get_server(
        self,
        user_id: str,
        server_name: str
    ) -> Optional[UserMCPServer]:
        """
        Get a specific server.

        Args:
            user_id: Owner's user ID
            server_name: Server name

        Returns:
            UserMCPServer or None
        """
        return await self.db.get_server(user_id, server_name)

    async def get_server_config(
        self,
        user_id: str,
        server_name: str
    ) -> Optional[MCPServerConfig]:
        """
        Get MCPServerConfig for a user's server (for use by MCPServerManager).

        Decrypts credentials and builds config object.

        Args:
            user_id: Owner's user ID
            server_name: Server name

        Returns:
            MCPServerConfig ready for connection, or None
        """
        server = await self.db.get_server(user_id, server_name)
        if not server:
            return None

        # Get decrypted credentials
        api_key, headers = await self.db.get_decrypted_credentials(user_id, server_name)

        # Build headers with auth
        final_headers = headers or {}
        if api_key:
            if server.auth_method == 'bearer':
                final_headers['Authorization'] = f'Bearer {api_key}'
            elif server.auth_method == 'api_key':
                # Default to Bearer, but could be customized per server
                final_headers['Authorization'] = f'Bearer {api_key}'

        return MCPServerConfig(
            type=MCPServerType(server.server_type),
            url=server.server_url,
            headers=final_headers or None,
            enabled=server.enabled,
            timeout=server.timeout,
            retry_attempts=server.retry_attempts,
            retry_delay=server.retry_delay,
            auto_reconnect=server.auto_reconnect,
            max_concurrent_requests=server.max_concurrent_requests,
            message_endpoint=server.message_endpoint  # For SSE servers
        )

    async def update_server(
        self,
        user_id: str,
        server_name: str,
        **updates
    ) -> bool:
        """
        Update server configuration.

        Args:
            user_id: Owner's user ID
            server_name: Server name
            **updates: Fields to update

        Returns:
            True if updated successfully
        """
        # Validate URL if being updated
        if 'server_url' in updates:
            url_valid, url_error = self.validator.validate(updates['server_url'])
            if not url_valid:
                raise ValueError(f"Invalid URL: {url_error}")

        # Validate server_type if being updated
        if 'server_type' in updates:
            type_valid, type_error = self.validator.validate_server_type(updates['server_type'])
            if not type_valid:
                raise ValueError(type_error)

        return await self.db.update_server(user_id, server_name, **updates)

    async def delete_server(self, user_id: str, server_name: str) -> bool:
        """
        Delete a user's MCP server.

        Args:
            user_id: Owner's user ID
            server_name: Server name

        Returns:
            True if deleted successfully
        """
        # FIX #11: Check rate limit
        self._check_rate_limit(user_id)

        # FIX #12: Audit log (uses private method)
        await self.db._audit_log(
            user_id=user_id,
            action="server_deleted",
            server_name=server_name,
            details=None
        )

        return await self.db.delete_server(user_id, server_name)

    async def test_connection(
        self,
        user_id: str,
        server_name: str,
        verify_protocol: bool = True
    ) -> TestConnectionResult:
        """
        Test connectivity to a user's MCP server.

        Performs actual MCP protocol handshake to verify the server speaks MCP.

        Args:
            user_id: Owner's user ID
            server_name: Server name
            verify_protocol: If True, performs full MCP handshake (default). 
                           If False, only checks HTTP connectivity.

        Returns:
            TestConnectionResult with status and discovered tools
        """
        # FIX #11: Check rate limit
        try:
            self._check_rate_limit(user_id)
        except ValueError as e:
            return TestConnectionResult(success=False, error=str(e))

        config = await self.get_server_config(user_id, server_name)
        if not config:
            return TestConnectionResult(
                success=False,
                error="Server not found"
            )

        try:
            start = time.time()

            if verify_protocol:
                # FIX #5: Perform actual MCP protocol handshake
                result = await self._test_mcp_protocol(user_id, server_name, config, start)
            else:
                # Legacy: Just check HTTP connectivity
                result = await self._test_http_connectivity(user_id, server_name, config, start)
            
            # FIX #12: Audit log test results (uses private method)
            await self.db._audit_log(
                user_id=user_id,
                action="server_tested",
                server_name=server_name,
                details={"success": result.success, "tools": result.tools_discovered}
            )
            
            return result

        except Exception as e:
            # FIX #14: Sanitize error message
            error = self._sanitize_error(f"Unexpected error: {str(e)}")
            await self.db.update_connection_status(
                user_id, server_name,
                connected=False,
                error=error
            )
            return TestConnectionResult(
                success=False,
                error=error
            )

    async def _test_http_connectivity(
        self,
        user_id: str,
        server_name: str,
        config: MCPServerConfig,
        start_time: float
    ) -> TestConnectionResult:
        """Test basic HTTP connectivity (legacy method)."""
        import aiohttp
        import time

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    config.url,
                    headers=config.headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    latency = (time.time() - start_time) * 1000

                    if resp.status < 400:
                        await self.db.update_connection_status(
                            user_id, server_name, connected=True
                        )
                        return TestConnectionResult(
                            success=True,
                            latency_ms=round(latency, 2)
                        )
                    else:
                        error = f"Server returned {resp.status}"
                        await self.db.update_connection_status(
                            user_id, server_name, connected=False, error=error
                        )
                        return TestConnectionResult(success=False, error=error)

        except aiohttp.ClientError as e:
            error = f"Connection error: {str(e)}"
            await self.db.update_connection_status(
                user_id, server_name, connected=False, error=error
            )
            return TestConnectionResult(success=False, error=error)

    async def _test_mcp_protocol(
        self,
        user_id: str,
        server_name: str,
        config: MCPServerConfig,
        start_time: float
    ) -> TestConnectionResult:
        """
        Test MCP protocol by performing actual handshake.
        
        Connects, sends initialize request, and attempts to list tools.
        """
        import time
        import asyncio
        from tools.mcp.protocol import MCPClient, MCPSSETransport, MCPHTTPTransport

        client = None
        try:
            # Create appropriate transport based on server type
            if config.type == MCPServerType.SSE:
                transport = MCPSSETransport(
                    url=config.url,
                    headers=config.headers,
                    timeout=min(config.timeout, 30),  # Cap at 30s for test
                    validate_ssrf=True  # SSRF: user-supplied URL
                )
            elif config.type == MCPServerType.HTTP:
                transport = MCPHTTPTransport(
                    url=config.url,
                    headers=config.headers,
                    timeout=min(config.timeout, 30),
                    validate_ssrf=True  # SSRF: user-supplied URL
                )
            else:
                return TestConnectionResult(
                    success=False,
                    error=f"Unsupported server type for testing: {config.type}"
                )

            # Create client and connect with timeout
            client = MCPClient(transport)
            
            try:
                await asyncio.wait_for(client.connect(), timeout=30)
            except asyncio.TimeoutError:
                return TestConnectionResult(
                    success=False,
                    error="MCP handshake timed out (30s)"
                )

            latency = (time.time() - start_time) * 1000

            # Get discovered tools
            tools = client.tools
            tool_names = [t.get('name', 'unknown') for t in tools]
            tools_count = len(tools)

            # Update database with results
            await self.db.update_connection_status(
                user_id, server_name,
                connected=True,
                tools_count=tools_count
            )

            self.logger.info(
                f"MCP protocol test passed for {server_name}: "
                f"{tools_count} tools, {latency:.0f}ms"
            )

            return TestConnectionResult(
                success=True,
                latency_ms=round(latency, 2),
                tools_discovered=tools_count,
                tools=tool_names[:20]  # Limit to first 20 tool names
            )

        except Exception as e:
            error = f"MCP protocol error: {str(e)}"
            await self.db.update_connection_status(
                user_id, server_name,
                connected=False,
                error=error
            )
            return TestConnectionResult(success=False, error=error)

        finally:
            # Clean up client
            if client:
                try:
                    await client.close()
                except Exception:
                    pass

    async def get_user_settings(self, user_id: str) -> UserMCPSettings:
        """
        Get user's MCP settings.

        Args:
            user_id: User ID

        Returns:
            UserMCPSettings record
        """
        return await self.db.get_user_settings(user_id)

    async def update_user_settings(self, user_id: str, **updates) -> bool:
        """
        Update user's MCP settings.

        Args:
            user_id: User ID
            **updates: Settings to update

        Returns:
            True if updated
        """
        return await self.db.update_user_settings(user_id, **updates)

    async def get_available_servers_for_session(
        self,
        user_id: str,
        include_global: bool = True
    ) -> Dict[str, Any]:
        """
        Get all available MCP servers for a session.

        Returns both global (platform) servers and user's custom servers.

        Args:
            user_id: User ID
            include_global: Whether to include global servers

        Returns:
            Dict with 'global' and 'user' server lists
        """
        settings = await self.db.get_user_settings(user_id)

        result = {
            'global': [],
            'user': []
        }

        # Get user's custom servers
        user_servers = await self.db.get_user_servers(user_id, enabled_only=True)
        for server in user_servers:
            result['user'].append({
                'name': server.server_name,
                'tool_id': f"mcp:user:{server.server_name}",
                'display_name': server.display_name or server.server_name,
                'type': server.server_type,
                'status': server.auth_status,
                'tools_count': server.tools_discovered
            })

        # Include global servers if enabled
        if include_global and settings.include_global_servers:
            # Global servers are loaded from mcp_config.json at startup
            # This will be populated by MCPTool when it initializes
            result['include_global'] = True
        else:
            result['include_global'] = False

        return result

    async def health_check_user_servers(
        self,
        user_id: str,
        update_status: bool = True
    ) -> Dict[str, Any]:
        """
        Perform health check on all user's MCP servers (FIX #15).
        
        Args:
            user_id: User ID to check
            update_status: If True, update connection status in database
            
        Returns:
            Dict with health status for each server
        """
        servers = await self.get_user_servers(user_id, enabled_only=True)
        results = {
            "user_id": user_id,
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_servers": len(servers),
            "healthy": 0,
            "unhealthy": 0,
            "servers": {}
        }
        
        for server in servers:
            try:
                # Use fast HTTP check for health (not full MCP protocol)
                test_result = await self.test_connection(
                    user_id, 
                    server.server_name,
                    verify_protocol=False  # Faster check
                )
                
                if test_result.success:
                    results["healthy"] += 1
                    results["servers"][server.server_name] = {
                        "status": "healthy",
                        "latency_ms": test_result.latency_ms
                    }
                else:
                    results["unhealthy"] += 1
                    results["servers"][server.server_name] = {
                        "status": "unhealthy",
                        "error": self._sanitize_error(test_result.error or "Unknown error")
                    }
                    
            except Exception as e:
                results["unhealthy"] += 1
                results["servers"][server.server_name] = {
                    "status": "error",
                    "error": self._sanitize_error(str(e))
                }
        
        return results

    def _validate_server_name(self, name: str) -> bool:
        """
        Validate server name format.

        Must be:
        - 3-50 characters
        - Lowercase
        - Alphanumeric and hyphens only
        - No leading/trailing hyphens

        Args:
            name: Server name to validate

        Returns:
            True if valid
        """
        import re

        if not name or len(name) < 3 or len(name) > 50:
            return False

        if name != name.lower():
            return False

        if not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', name) and len(name) > 2:
            return False

        if len(name) <= 2:
            return re.match(r'^[a-z0-9]+$', name) is not None

        return True


# Singleton instance
_user_mcp_service: Optional[UserMCPService] = None


def get_user_mcp_service() -> Optional[UserMCPService]:
    """Get the singleton UserMCPService instance (may be None)."""
    return _user_mcp_service


def require_user_mcp_service() -> UserMCPService:
    """
    Get the singleton UserMCPService instance.
    
    FIX #10: Raises error if service not initialized, preventing None checks everywhere.
    
    Raises:
        RuntimeError: If service not initialized
        
    Returns:
        Initialized UserMCPService
    """
    if _user_mcp_service is None:
        raise RuntimeError(
            "UserMCPService not initialized. Call init_user_mcp_service() first."
        )
    return _user_mcp_service


def init_user_mcp_service(db_handler: UserMCPServersHandler, **kwargs) -> UserMCPService:
    """
    Initialize the singleton UserMCPService.

    Args:
        db_handler: Database handler
        **kwargs: Additional options (encryption, url_validator, allow_http)

    Returns:
        Initialized UserMCPService
    """
    global _user_mcp_service
    _user_mcp_service = UserMCPService(db_handler, **kwargs)
    return _user_mcp_service
