"""Configuration models for MCP service."""

import logging
import os
import re
from typing import Dict, List, Optional, Any, Union
from enum import Enum
from pydantic import BaseModel, Field, validator, root_validator
from core.exceptions import ConfigurationError

logger = logging.getLogger(__name__)


class MCPServerType(str, Enum):
    """MCP server connection types."""
    STDIO = "stdio"
    SSE = "sse"
    HTTP = "http"  # HTTP JSON-RPC (request-response)
    STREAMABLE_HTTP = "streamable_http"  # POST with SSE responses (e.g., mcp.anysite.io)


class MCPEnvironmentVariable(BaseModel):
    """Environment variable configuration for MCP servers."""
    name: str = Field(..., description="Environment variable name")
    value: str = Field(..., description="Environment variable value (supports ${VAR} substitution)")


class MCPServerConfig(BaseModel):
    """Configuration for a single MCP server."""
    
    type: MCPServerType = Field(..., description="Server connection type")
    command: Optional[List[str]] = Field(None, description="Command to start server (for STDIO)")
    args: Optional[List[str]] = Field(default_factory=list, description="Arguments for server command")
    url: Optional[str] = Field(None, description="Server URL (for SSE)")
    headers: Optional[Dict[str, str]] = Field(default_factory=dict, description="HTTP headers (for SSE)")
    env: Optional[List[MCPEnvironmentVariable]] = Field(default_factory=list, description="Environment variables")
    enabled: bool = Field(True, description="Whether this server is enabled")
    timeout: int = Field(30, description="Connection timeout in seconds")
    retry_attempts: int = Field(3, description="Number of retry attempts")
    retry_delay: int = Field(5, description="Delay between retries in seconds")
    auto_reconnect: bool = Field(True, description="Whether to auto-reconnect on failure")
    max_concurrent_requests: int = Field(10, description="Maximum concurrent requests to this server")
    message_endpoint: Optional[str] = Field(None, description="For SSE: explicit POST endpoint for messages (FIX #7)")

    @validator('command')
    def validate_command_for_stdio(cls, v, values):
        """Validate that command is provided for STDIO servers."""
        if values.get('type') == MCPServerType.STDIO and not v:
            raise ValueError("command is required for STDIO servers")
        return v
    
    @validator('url')
    def validate_url_for_sse(cls, v, values):
        """Validate that URL is provided for SSE servers."""
        if values.get('type') == MCPServerType.SSE and not v:
            raise ValueError("url is required for SSE servers")
        return v
    
    @validator('timeout')
    def validate_timeout(cls, v):
        """Validate timeout is positive."""
        if v <= 0:
            raise ValueError("timeout must be positive")
        return v
    
    @validator('retry_attempts')
    def validate_retry_attempts(cls, v):
        """Validate retry attempts is non-negative."""
        if v < 0:
            raise ValueError("retry_attempts must be non-negative")
        return v


class MCPConfig(BaseModel):
    """Main MCP service configuration."""

    enabled: bool = Field(False, description="Whether MCP service is enabled (disabled by default for security)")
    servers: Dict[str, MCPServerConfig] = Field(default_factory=dict, description="Configured MCP servers")
    global_timeout: int = Field(60, description="Global timeout for all operations")
    max_concurrent_connections: int = Field(10, description="Maximum concurrent server connections")
    enable_resource_caching: bool = Field(True, description="Whether to cache resources")
    cache_ttl_seconds: int = Field(300, description="Cache TTL in seconds")
    auto_discover_tools: bool = Field(True, description="Whether to automatically discover and register tools")
    log_mcp_communications: bool = Field(False, description="Whether to log MCP protocol messages (debug)")
    
    @validator('global_timeout')
    def validate_global_timeout(cls, v):
        """Validate global timeout is positive."""
        if v <= 0:
            raise ValueError("global_timeout must be positive")
        return v
    
    @validator('max_concurrent_connections')
    def validate_max_concurrent_connections(cls, v):
        """Validate max concurrent connections is positive."""
        if v <= 0:
            raise ValueError("max_concurrent_connections must be positive")
        return v
    
    @validator('cache_ttl_seconds')
    def validate_cache_ttl(cls, v):
        """Validate cache TTL is non-negative."""
        if v < 0:
            raise ValueError("cache_ttl_seconds must be non-negative")
        return v


def resolve_environment_variables(config_value: str) -> str:
    """Resolve environment variables in configuration values.
    
    Supports ${VAR} and ${VAR:-default} syntax.
    
    Args:
        config_value: Configuration value that may contain environment variables
        
    Returns:
        Resolved configuration value
        
    Raises:
        ConfigurationError: If required environment variable is not found
    """
    if not isinstance(config_value, str):
        return config_value
    
    # Pattern to match ${VAR} or ${VAR:-default}
    pattern = re.compile(r'\$\{([^}]+)\}')
    
    def replace_var(match):
        var_expr = match.group(1)
        
        # Check if there's a default value
        if ':-' in var_expr:
            var_name, default_value = var_expr.split(':-', 1)
            return os.getenv(var_name, default_value)
        else:
            var_name = var_expr
            value = os.getenv(var_name)
            if value is None:
                raise ConfigurationError(f"Required environment variable '{var_name}' not found")
            return value
    
    return pattern.sub(replace_var, config_value)


def resolve_config_environment_variables(config: MCPConfig) -> MCPConfig:
    """Resolve all environment variables in an MCP configuration.
    
    Args:
        config: MCP configuration with potential environment variables
        
    Returns:
        Configuration with resolved environment variables
        
    Raises:
        ConfigurationError: If required environment variable is not found
    """
    resolved_servers = {}

    def _resolve_one(server_config: MCPServerConfig) -> MCPServerConfig:
        # Create a copy of the server config dict
        server_dict = server_config.model_dump()

        # Resolve URL if present
        if server_dict.get('url'):
            server_dict['url'] = resolve_environment_variables(server_dict['url'])

        # Resolve headers if present
        if server_dict.get('headers'):
            resolved_headers = {}
            for key, value in server_dict['headers'].items():
                resolved_headers[key] = resolve_environment_variables(value)
            server_dict['headers'] = resolved_headers

        # Resolve environment variables
        if server_dict.get('env'):
            resolved_env = []
            for env_var in server_dict['env']:
                if isinstance(env_var, dict):
                    resolved_env.append({
                        'name': env_var['name'],
                        'value': resolve_environment_variables(env_var['value'])
                    })
                else:
                    # Handle MCPEnvironmentVariable objects
                    resolved_env.append(MCPEnvironmentVariable(
                        name=env_var.name,
                        value=resolve_environment_variables(env_var.value)
                    ))
            server_dict['env'] = resolved_env

        # Resolve command arguments if present
        if server_dict.get('command'):
            resolved_command = []
            for cmd_part in server_dict['command']:
                resolved_command.append(resolve_environment_variables(cmd_part))
            server_dict['command'] = resolved_command

        if server_dict.get('args'):
            resolved_args = []
            for arg in server_dict['args']:
                resolved_args.append(resolve_environment_variables(arg))
            server_dict['args'] = resolved_args

        # Recreate the server config with resolved values
        return MCPServerConfig(**server_dict)

    for server_name, server_config in config.servers.items():
        # Fault-isolate per server: a missing required ${VAR} for ONE server drops
        # only that server (logged loudly), instead of aborting resolution for the
        # whole config and silently disabling ALL MCP (the prior behavior — the
        # caller swallowed the raise and fell back to an empty default config).
        try:
            resolved_servers[server_name] = _resolve_one(server_config)
        except ConfigurationError as e:
            logger.error(
                "MCP server '%s' disabled: %s. Other MCP servers are unaffected.",
                server_name, e,
            )

    # Create new config with resolved servers
    config_dict = config.model_dump()
    config_dict['servers'] = resolved_servers

    return MCPConfig(**config_dict)


# Default MCP server configurations - completely empty, all servers added dynamically
DEFAULT_MCP_SERVERS = {}


def get_default_mcp_config() -> MCPConfig:
    """Get default MCP configuration with all servers disabled for security."""
    return MCPConfig(
        enabled=False,  # Disabled by default
        servers=DEFAULT_MCP_SERVERS,
        global_timeout=60,
        max_concurrent_connections=5,  # Conservative default
        enable_resource_caching=True,
        cache_ttl_seconds=300,
        auto_discover_tools=True,
        log_mcp_communications=False
    )


def add_mcp_server(config: MCPConfig, name: str, server_config: Union[MCPServerConfig, Dict[str, Any]]) -> MCPConfig:
    """Add a new MCP server to the configuration dynamically.
    
    Args:
        config: Existing MCP configuration
        name: Name for the new server
        server_config: Server configuration (MCPServerConfig or dict)
    
    Returns:
        Updated MCPConfig
    """
    if isinstance(server_config, dict):
        server_config = MCPServerConfig(**server_config)
    
    config.servers[name] = server_config
    return config


def create_stdio_server(command: List[str], env_vars: Optional[Dict[str, str]] = None, **kwargs) -> MCPServerConfig:
    """Helper to create a STDIO MCP server configuration.
    
    Args:
        command: Command to start the server
        env_vars: Environment variables as key-value pairs
        **kwargs: Additional configuration options
    
    Returns:
        MCPServerConfig for STDIO server
    """
    env = []
    if env_vars:
        for name, value in env_vars.items():
            env.append(MCPEnvironmentVariable(name=name, value=value))
    
    return MCPServerConfig(
        type=MCPServerType.STDIO,
        command=command,
        env=env,
        enabled=kwargs.get('enabled', False),
        timeout=kwargs.get('timeout', 30),
        retry_attempts=kwargs.get('retry_attempts', 3),
        retry_delay=kwargs.get('retry_delay', 5),
        auto_reconnect=kwargs.get('auto_reconnect', True),
        max_concurrent_requests=kwargs.get('max_concurrent_requests', 10)
    )


def create_sse_server(url: str, headers: Optional[Dict[str, str]] = None, **kwargs) -> MCPServerConfig:
    """Helper to create an SSE MCP server configuration.

    Args:
        url: Server URL
        headers: HTTP headers
        **kwargs: Additional configuration options

    Returns:
        MCPServerConfig for SSE server
    """
    return MCPServerConfig(
        type=MCPServerType.SSE,
        url=url,
        headers=headers or {},
        enabled=kwargs.get('enabled', False),
        timeout=kwargs.get('timeout', 30),
        retry_attempts=kwargs.get('retry_attempts', 3),
        retry_delay=kwargs.get('retry_delay', 5),
        auto_reconnect=kwargs.get('auto_reconnect', True),
        max_concurrent_requests=kwargs.get('max_concurrent_requests', 10)
    )


def load_local_mcp_servers() -> Dict[str, Any]:
    """Load MCP server configs from ~/.polyrob/mcp.json then ./.polyrob/mcp.json (R7).

    Project (./.polyrob) overrides global (~/.polyrob) on name clash. Each file may
    use a top-level "servers" or "mcpServers" object. Missing/invalid files are
    skipped. File-first: no DB, no dependency on config/mcp_config.json.
    """
    import json
    from pathlib import Path
    from core.paths import polyrob_home
    merged: Dict[str, Any] = {}
    for path in (polyrob_home() / "mcp.json", Path.cwd() / ".polyrob" / "mcp.json"):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        servers = data.get("servers") or data.get("mcpServers") or {}
        if isinstance(servers, dict):
            merged.update(servers)   # later (project) wins
    return merged