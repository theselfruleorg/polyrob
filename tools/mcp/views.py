"""Pydantic models for MCP service actions."""

from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field, field_validator, ConfigDict


class MCPExecuteToolAction(BaseModel):
    """Parameters for executing an MCP tool."""
    model_config = ConfigDict(extra='forbid')

    server_name: str = Field(..., description="Name of the MCP server (e.g., 'anysite', 'ghost')")
    tool_name: str = Field(..., description="Name of the tool to execute on the server")
    # FIX (Jan 2026): Make arguments REQUIRED so LLMs cannot omit it
    # Previously optional with default={}, but LLMs were completely omitting the field,
    # causing all MCP tools to receive empty parameters.
    # Making it required forces LLMs to include arguments={...} even if empty.
    arguments: Dict[str, Any] = Field(
        ...,  # Required - LLM MUST include this field
        description=(
            "REQUIRED: The parameters to pass to the target tool. "
            "This dict is passed directly to the tool - PUT YOUR SEARCH PARAMETERS HERE! "
            "Example: {'query': 'AI startups', 'count': 10} for search tools. "
            "Example: {'keywords': 'CEO founder', 'count': 5} for LinkedIn search. "
            "Use mcp_list_tools to see each tool's required parameters. "
            "For tools that need no parameters, pass empty dict {}."
        )
    )

    @field_validator('server_name')
    @classmethod
    def validate_server_name(cls, v):
        """Validate server name is not empty."""
        if not v or not v.strip():
            raise ValueError("server_name cannot be empty")
        return v.strip()

    @field_validator('tool_name')
    @classmethod
    def validate_tool_name(cls, v):
        """Validate tool name is not empty."""
        if not v or not v.strip():
            raise ValueError("tool_name cannot be empty")
        return v.strip()

    @field_validator('arguments')
    @classmethod
    def validate_arguments(cls, v):
        """Warn about empty arguments - most tools require them."""
        if v is None:
            v = {}
        if not v:
            # Log warning but allow empty for tools that don't need params
            import logging
            logging.getLogger(__name__).warning(
                f"⚠️ Empty arguments provided for MCP tool. "
                f"Most MCP tools require parameters - check mcp_list_tools for schema."
            )
        return v


class MCPReadResourceAction(BaseModel):
    """Parameters for reading an MCP resource."""
    model_config = ConfigDict(extra='forbid')

    server_name: str = Field(..., description="Name of the MCP server")
    resource_uri: str = Field(..., description="URI of the resource to read")

    @field_validator('server_name')
    @classmethod
    def validate_server_name(cls, v):
        """Validate server name is not empty."""
        if not v or not v.strip():
            raise ValueError("server_name cannot be empty")
        return v.strip()

    @field_validator('resource_uri')
    @classmethod
    def validate_resource_uri(cls, v):
        """Validate resource URI is not empty."""
        if not v or not v.strip():
            raise ValueError("resource_uri cannot be empty")
        return v.strip()


class MCPListToolsAction(BaseModel):
    """Parameters for listing MCP tools."""
    model_config = ConfigDict(extra='ignore')  # Allow extra fields but ignore them

    server_name: Optional[str] = Field(None, description="Specific server name (optional, lists all if not provided)")

    @field_validator('server_name')
    @classmethod
    def validate_server_name(cls, v):
        """Validate server name is not empty if provided."""
        if v is not None and (not v or not v.strip()):
            raise ValueError("server_name cannot be empty if provided")
        return v.strip() if v else None


class MCPListResourcesAction(BaseModel):
    """Parameters for listing MCP resources."""
    model_config = ConfigDict(extra='ignore')  # Allow extra fields but ignore them

    server_name: Optional[str] = Field(None, description="Specific server name (optional, lists all if not provided)")

    @field_validator('server_name')
    @classmethod
    def validate_server_name(cls, v):
        """Validate server name is not empty if provided."""
        if v is not None and (not v or not v.strip()):
            raise ValueError("server_name cannot be empty if provided")
        return v.strip() if v else None


class MCPListServersAction(BaseModel):
    """Parameters for listing MCP servers."""
    model_config = ConfigDict(extra='ignore')  # Allow extra fields but ignore them
    
    include_disabled: bool = Field(False, description="Whether to include disabled servers")
    include_details: bool = Field(True, description="Whether to include detailed server information")


class MCPServerStatusAction(BaseModel):
    """Parameters for getting MCP server status."""
    model_config = ConfigDict(extra='forbid')

    server_name: str = Field(..., description="Name of the MCP server")

    @field_validator('server_name')
    @classmethod
    def validate_server_name(cls, v):
        """Validate server name is not empty."""
        if not v or not v.strip():
            raise ValueError("server_name cannot be empty")
        return v.strip()


class MCPConnectServerAction(BaseModel):
    """Parameters for connecting to an MCP server."""
    model_config = ConfigDict(extra='forbid')

    server_name: str = Field(..., description="Name of the MCP server to connect")

    @field_validator('server_name')
    @classmethod
    def validate_server_name(cls, v):
        """Validate server name is not empty."""
        if not v or not v.strip():
            raise ValueError("server_name cannot be empty")
        return v.strip()


class MCPDisconnectServerAction(BaseModel):
    """Parameters for disconnecting from an MCP server."""
    model_config = ConfigDict(extra='forbid')

    server_name: str = Field(..., description="Name of the MCP server to disconnect")

    @field_validator('server_name')
    @classmethod
    def validate_server_name(cls, v):
        """Validate server name is not empty."""
        if not v or not v.strip():
            raise ValueError("server_name cannot be empty")
        return v.strip()


class MCPReloadServerAction(BaseModel):
    """Parameters for reloading an MCP server connection."""
    model_config = ConfigDict(extra='forbid')

    server_name: str = Field(..., description="Name of the MCP server to reload")

    @field_validator('server_name')
    @classmethod
    def validate_server_name(cls, v):
        """Validate server name is not empty."""
        if not v or not v.strip():
            raise ValueError("server_name cannot be empty")
        return v.strip()


class MCPGetCapabilitiesAction(BaseModel):
    """Parameters for getting MCP server capabilities."""
    model_config = ConfigDict(extra='forbid')

    server_name: str = Field(..., description="Name of the MCP server")

    @field_validator('server_name')
    @classmethod
    def validate_server_name(cls, v):
        """Validate server name is not empty."""
        if not v or not v.strip():
            raise ValueError("server_name cannot be empty")
        return v.strip()


class MCPSubscribeResourceAction(BaseModel):
    """Parameters for subscribing to MCP resource updates."""
    model_config = ConfigDict(extra='forbid')

    server_name: str = Field(..., description="Name of the MCP server")
    resource_uri: str = Field(..., description="URI of the resource to subscribe to")

    @field_validator('server_name')
    @classmethod
    def validate_server_name(cls, v):
        """Validate server name is not empty."""
        if not v or not v.strip():
            raise ValueError("server_name cannot be empty")
        return v.strip()

    @field_validator('resource_uri')
    @classmethod
    def validate_resource_uri(cls, v):
        """Validate resource URI is not empty."""
        if not v or not v.strip():
            raise ValueError("resource_uri cannot be empty")
        return v.strip()


class MCPUnsubscribeResourceAction(BaseModel):
    """Parameters for unsubscribing from MCP resource updates."""
    model_config = ConfigDict(extra='forbid')

    server_name: str = Field(..., description="Name of the MCP server")
    resource_uri: str = Field(..., description="URI of the resource to unsubscribe from")

    @field_validator('server_name')
    @classmethod
    def validate_server_name(cls, v):
        """Validate server name is not empty."""
        if not v or not v.strip():
            raise ValueError("server_name cannot be empty")
        return v.strip()

    @field_validator('resource_uri')
    @classmethod
    def validate_resource_uri(cls, v):
        """Validate resource URI is not empty."""
        if not v or not v.strip():
            raise ValueError("resource_uri cannot be empty")
        return v.strip()


class MCPHealthCheckAction(BaseModel):
    """Parameters for performing MCP server health check."""
    model_config = ConfigDict(extra='ignore')

    server_name: Optional[str] = Field(None, description="Specific server name (optional, checks all if not provided)")

    @field_validator('server_name')
    @classmethod
    def validate_server_name(cls, v):
        """Validate server name is not empty if provided."""
        if v is not None and (not v or not v.strip()):
            raise ValueError("server_name cannot be empty if provided")
        return v.strip() if v else None


# Response models for better type safety and documentation
class MCPToolInfo(BaseModel):
    """Information about an MCP tool."""
    
    name: str
    description: str
    server_name: str
    input_schema: Dict[str, Any]
    
    def __str__(self) -> str:
        """Format tool info for LLM visibility with schema details."""
        # Extract required parameters from schema
        props = self.input_schema.get('properties', {})
        required = self.input_schema.get('required', [])
        
        # Build parameter summary
        params = []
        for param_name, param_schema in props.items():
            param_type = param_schema.get('type', 'any')
            is_required = param_name in required
            req_marker = '(required)' if is_required else '(optional)'
            param_desc = param_schema.get('description', '')
            params.append(f"  - {param_name}: {param_type} {req_marker} - {param_desc}")
        
        params_str = '\n'.join(params) if params else '  (no parameters)'
        
        return f"""
Tool: {self.name}
Server: {self.server_name}
Description: {self.description}
Parameters:
{params_str}
"""


class MCPResourceInfo(BaseModel):
    """Information about an MCP resource."""
    
    uri: str
    name: str
    server_name: str
    description: Optional[str] = None
    mime_type: Optional[str] = None


class MCPServerInfo(BaseModel):
    """Information about an MCP server."""
    
    name: str
    status: str
    type: str
    enabled: bool
    connected_at: Optional[float] = None
    last_error: Optional[str] = None
    retry_count: int = 0
    tools_count: int = 0
    resources_count: int = 0
    capabilities: Dict[str, Any] = Field(default_factory=dict)


class MCPExecutionResult(BaseModel):
    """Result of MCP tool execution."""
    
    success: bool
    result: Any = None
    error: Optional[str] = None
    execution_time: Optional[float] = None


class MCPResourceContent(BaseModel):
    """Content of an MCP resource."""
    
    uri: str
    content: Any
    mime_type: Optional[str] = None
    size: Optional[int] = None
    last_modified: Optional[float] = None


class MCPHealthStatus(BaseModel):
    """Health status of MCP servers."""
    
    healthy_servers: List[str] = Field(default_factory=list)
    unhealthy_servers: List[str] = Field(default_factory=list)
    total_servers: int = 0
    overall_health: str = "unknown"  # "healthy", "degraded", "unhealthy"