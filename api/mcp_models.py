"""
Pydantic models for MCP server management API.
"""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, validator
import re


class AddServerRequest(BaseModel):
    """Request to add a new MCP server."""

    server_name: str = Field(
        ...,
        min_length=3,
        max_length=50,
        description="Unique server name (lowercase, alphanumeric, hyphens only)"
    )
    server_url: str = Field(
        ...,
        description="Server endpoint URL (must be HTTPS)"
    )
    server_type: str = Field(
        default="sse",
        description="Server type: 'sse' or 'http' (STDIO not allowed)"
    )
    auth_method: str = Field(
        default="api_key",
        description="Authentication method: 'api_key', 'bearer', or 'none'"
    )
    api_key: Optional[str] = Field(
        default=None,
        description="API key for authentication (will be encrypted)"
    )
    headers: Optional[Dict[str, str]] = Field(
        default=None,
        description="Custom headers (will be encrypted)"
    )
    display_name: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Friendly display name"
    )
    enabled: bool = Field(
        default=True,
        description="Whether server is enabled"
    )
    timeout: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Connection timeout in seconds"
    )
    retry_attempts: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Number of retry attempts"
    )
    retry_delay: int = Field(
        default=5,
        ge=1,
        le=60,
        description="Delay between retry attempts in seconds"
    )
    auto_reconnect: bool = Field(
        default=True,
        description="Whether to automatically reconnect on failure"
    )
    max_concurrent_requests: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Maximum concurrent requests to this server"
    )
    verify_connection: bool = Field(
        default=False,
        description="If true, verify MCP protocol before saving (tests connection and discovers tools)"
    )
    message_endpoint: Optional[str] = Field(
        default=None,
        description="For SSE servers: explicit POST endpoint for sending messages (if different from main URL)"
    )

    @validator('server_name')
    def validate_server_name(cls, v):
        """Validate server name format."""
        if not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', v) and len(v) > 2:
            raise ValueError('Server name must be lowercase alphanumeric with hyphens')
        if len(v) <= 2 and not re.match(r'^[a-z0-9]+$', v):
            raise ValueError('Server name must be lowercase alphanumeric')
        return v

    @validator('server_type')
    def validate_server_type(cls, v):
        """Validate server type."""
        if v not in ('sse', 'http', 'streamable_http'):
            raise ValueError('Server type must be "sse", "http", or "streamable_http" (STDIO not allowed)')
        return v

    @validator('auth_method')
    def validate_auth_method(cls, v):
        """Validate auth method."""
        if v not in ('api_key', 'bearer', 'none'):
            raise ValueError('Auth method must be "api_key", "bearer", or "none"')
        return v


class UpdateServerRequest(BaseModel):
    """Request to update an MCP server."""

    server_url: Optional[str] = Field(
        default=None,
        description="Server endpoint URL (must be HTTPS)"
    )
    auth_method: Optional[str] = Field(
        default=None,
        description="Authentication method"
    )
    api_key: Optional[str] = Field(
        default=None,
        description="API key for authentication"
    )
    headers: Optional[Dict[str, str]] = Field(
        default=None,
        description="Custom headers"
    )
    display_name: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Friendly display name"
    )
    enabled: Optional[bool] = Field(
        default=None,
        description="Whether server is enabled"
    )
    timeout: Optional[int] = Field(
        default=None,
        ge=5,
        le=300,
        description="Connection timeout"
    )
    retry_attempts: Optional[int] = Field(
        default=None,
        ge=0,
        le=10,
        description="Retry attempts"
    )
    retry_delay: Optional[int] = Field(
        default=None,
        ge=1,
        le=60,
        description="Delay between retry attempts in seconds"
    )
    auto_reconnect: Optional[bool] = Field(
        default=None,
        description="Whether to automatically reconnect on failure"
    )
    max_concurrent_requests: Optional[int] = Field(
        default=None,
        ge=1,
        le=50,
        description="Maximum concurrent requests to this server"
    )
    message_endpoint: Optional[str] = Field(
        default=None,
        description="For SSE servers: explicit POST endpoint for sending messages"
    )


class ServerResponse(BaseModel):
    """Response with server details."""

    server_name: str
    display_name: Optional[str]
    server_url: str
    server_type: str
    auth_method: str
    enabled: bool
    timeout: int
    retry_attempts: int
    retry_delay: int
    auto_reconnect: bool
    max_concurrent_requests: int
    message_endpoint: Optional[str]
    auth_status: str
    connection_count: int
    tools_discovered: int
    last_connected_at: Optional[str]
    last_error: Optional[str]
    created_at: str
    updated_at: str

    # Tool ID for use in session creation
    tool_id: str


class ServerListResponse(BaseModel):
    """Response with list of servers."""

    servers: List[ServerResponse]
    count: int
    max_servers: int


class TestConnectionResponse(BaseModel):
    """Response from connection test."""

    success: bool
    latency_ms: Optional[float]
    error: Optional[str]
    tools_discovered: Optional[int]


class UserSettingsRequest(BaseModel):
    """Request to update user MCP settings."""

    mcp_enabled: Optional[bool] = Field(
        default=None,
        description="Enable/disable MCP for user"
    )
    include_global_servers: Optional[bool] = Field(
        default=None,
        description="Include global platform servers"
    )
    max_servers: Optional[int] = Field(
        default=None,
        ge=1,
        le=50,
        description="Maximum number of custom servers"
    )
    default_timeout: Optional[int] = Field(
        default=None,
        ge=5,
        le=300,
        description="Default timeout for new servers"
    )


class UserSettingsResponse(BaseModel):
    """Response with user MCP settings."""

    mcp_enabled: bool
    include_global_servers: bool
    max_servers: int
    default_timeout: int


class AvailableServersResponse(BaseModel):
    """Response with all available MCP servers for session creation."""

    global_servers: List[Dict[str, Any]]
    user_servers: List[Dict[str, Any]]
    include_global: bool


class AuditLogEntry(BaseModel):
    """Single audit log entry."""

    id: int
    action: str
    server_name: Optional[str]
    details: Optional[Dict[str, Any]]
    ip_address: Optional[str]
    timestamp: str


class AuditLogResponse(BaseModel):
    """Response with audit log entries."""

    entries: List[AuditLogEntry]
    count: int


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str
    detail: Optional[str] = None
