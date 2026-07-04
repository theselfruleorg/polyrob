"""MCP (Model Context Protocol) service module."""

from .mcp_tool import MCPTool
from .config import MCPConfig, MCPServerConfig, MCPServerType, get_default_mcp_config
from .server_manager import MCPServerManager, ServerStatus, MCPToolMetadata, MCPResource
from .views import (
    MCPExecuteToolAction, MCPReadResourceAction, MCPListToolsAction, MCPListResourcesAction,
    MCPListServersAction, MCPServerStatusAction, MCPConnectServerAction, MCPDisconnectServerAction,
    MCPReloadServerAction, MCPGetCapabilitiesAction, MCPSubscribeResourceAction, 
    MCPUnsubscribeResourceAction, MCPHealthCheckAction,
    MCPToolInfo, MCPResourceInfo, MCPServerInfo, MCPExecutionResult, MCPResourceContent, MCPHealthStatus
)

__all__ = [
    # Main tool class
    'MCPTool',

    # Configuration
    'MCPConfig',
    'MCPServerConfig',
    'MCPServerType',
    'get_default_mcp_config',

    # Server management
    'MCPServerManager',
    'ServerStatus',
    'MCPToolMetadata',  # Metadata class for discovered tools
    'MCPResource',
    
    # Action parameter models
    'MCPExecuteToolAction',
    'MCPReadResourceAction',
    'MCPListToolsAction',
    'MCPListResourcesAction',
    'MCPListServersAction',
    'MCPServerStatusAction',
    'MCPConnectServerAction',
    'MCPDisconnectServerAction',
    'MCPReloadServerAction',
    'MCPGetCapabilitiesAction',
    'MCPSubscribeResourceAction',
    'MCPUnsubscribeResourceAction',
    'MCPHealthCheckAction',
    
    # Response models
    'MCPToolInfo',
    'MCPResourceInfo',
    'MCPServerInfo',
    'MCPExecutionResult',
    'MCPResourceContent',
    'MCPHealthStatus'
]