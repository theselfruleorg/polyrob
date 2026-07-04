"""
API routes for user MCP server management.

Provides endpoints for users to manage their custom MCP servers.
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Request, Depends

from api.mcp_models import (
    AddServerRequest, UpdateServerRequest, ServerResponse, ServerListResponse,
    TestConnectionResponse, UserSettingsRequest, UserSettingsResponse,
    AvailableServersResponse, AuditLogEntry, AuditLogResponse, ErrorResponse
)
from api.dependencies import get_user_id, get_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])


async def get_user_mcp_service():
    """Get UserMCPService from container."""
    from core.container import DependencyContainer

    container = DependencyContainer.get_instance()
    if not container:
        raise HTTPException(status_code=503, detail="Service unavailable")

    service = container.get_service('user_mcp_service')
    if not service:
        raise HTTPException(status_code=503, detail="MCP service not initialized")

    return service


async def get_mcp_tool():
    """Get MCPTool from container."""
    from core.container import DependencyContainer

    container = DependencyContainer.get_instance()
    if not container:
        raise HTTPException(status_code=503, detail="Service unavailable")

    # MCPTool is registered as a service, not a tool
    mcp_tool = container.get_service('mcp')
    if not mcp_tool:
        raise HTTPException(status_code=503, detail="MCP tool not available")

    return mcp_tool


# ========================================
# SERVER MANAGEMENT ENDPOINTS
# ========================================

@router.post("/servers", response_model=ServerResponse, responses={400: {"model": ErrorResponse}})
async def add_server(
    request: Request,
    data: AddServerRequest,
    user_id: str = Depends(get_user_id)
):
    """
    Add a new MCP server.

    Creates a new custom MCP server configuration for the authenticated user.
    The server URL must be HTTPS and cannot point to internal networks.
    STDIO server type is not allowed for security reasons.

    Returns the created server with its tool_id for use in sessions.
    """
    service = await get_user_mcp_service()
    client_ip = get_client_ip(request)

    result = await service.add_server(
        user_id=user_id,
        server_name=data.server_name,
        server_url=data.server_url,
        server_type=data.server_type,
        auth_method=data.auth_method,
        api_key=data.api_key,
        headers=data.headers,
        display_name=data.display_name,
        enabled=data.enabled,
        timeout=data.timeout,
        retry_attempts=data.retry_attempts,
        retry_delay=data.retry_delay,
        auto_reconnect=data.auto_reconnect,
        max_concurrent_requests=data.max_concurrent_requests,
        verify_connection=data.verify_connection,
        message_endpoint=data.message_endpoint
    )

    if not result.success:
        raise HTTPException(status_code=400, detail=result.error)

    server = result.server
    return ServerResponse(
        server_name=server.server_name,
        display_name=server.display_name,
        server_url=server.server_url,
        server_type=server.server_type,
        auth_method=server.auth_method,
        enabled=server.enabled,
        timeout=server.timeout,
        retry_attempts=server.retry_attempts,
        retry_delay=server.retry_delay,
        auto_reconnect=server.auto_reconnect,
        max_concurrent_requests=server.max_concurrent_requests,
        message_endpoint=server.message_endpoint,
        auth_status=server.auth_status,
        connection_count=server.connection_count,
        tools_discovered=server.tools_discovered,
        last_connected_at=str(server.last_connected_at) if server.last_connected_at else None,
        last_error=server.last_error,
        created_at=str(server.created_at),
        updated_at=str(server.updated_at),
        tool_id=f"mcp:user:{server.server_name}"
    )


@router.get("/servers", response_model=ServerListResponse)
async def list_servers(
    request: Request,
    enabled_only: bool = False,
    user_id: str = Depends(get_user_id)
):
    """
    List user's MCP servers.

    Returns all MCP servers configured by the authenticated user.
    """
    service = await get_user_mcp_service()

    servers = await service.get_user_servers(user_id, enabled_only=enabled_only)
    settings = await service.get_user_settings(user_id)

    return ServerListResponse(
        servers=[
            ServerResponse(
                server_name=s.server_name,
                display_name=s.display_name,
                server_url=s.server_url,
                server_type=s.server_type,
                auth_method=s.auth_method,
                enabled=s.enabled,
                timeout=s.timeout,
                retry_attempts=s.retry_attempts,
                retry_delay=s.retry_delay,
                auto_reconnect=s.auto_reconnect,
                max_concurrent_requests=s.max_concurrent_requests,
                message_endpoint=s.message_endpoint,
                auth_status=s.auth_status,
                connection_count=s.connection_count,
                tools_discovered=s.tools_discovered,
                last_connected_at=str(s.last_connected_at) if s.last_connected_at else None,
                last_error=s.last_error,
                created_at=str(s.created_at),
                updated_at=str(s.updated_at),
                tool_id=f"mcp:user:{s.server_name}"
            )
            for s in servers
        ],
        count=len(servers),
        max_servers=settings.max_servers
    )


@router.get("/servers/{server_name}", response_model=ServerResponse, responses={404: {"model": ErrorResponse}})
async def get_server(
    server_name: str,
    request: Request,
    user_id: str = Depends(get_user_id)
):
    """
    Get a specific MCP server.

    Returns details of the specified server.
    """
    service = await get_user_mcp_service()

    server = await service.get_server(user_id, server_name)
    if not server:
        raise HTTPException(status_code=404, detail=f"Server '{server_name}' not found")

    return ServerResponse(
        server_name=server.server_name,
        display_name=server.display_name,
        server_url=server.server_url,
        server_type=server.server_type,
        auth_method=server.auth_method,
        enabled=server.enabled,
        timeout=server.timeout,
        retry_attempts=server.retry_attempts,
        retry_delay=server.retry_delay,
        auto_reconnect=server.auto_reconnect,
        max_concurrent_requests=server.max_concurrent_requests,
        message_endpoint=server.message_endpoint,
        auth_status=server.auth_status,
        connection_count=server.connection_count,
        tools_discovered=server.tools_discovered,
        last_connected_at=str(server.last_connected_at) if server.last_connected_at else None,
        last_error=server.last_error,
        created_at=str(server.created_at),
        updated_at=str(server.updated_at),
        tool_id=f"mcp:user:{server.server_name}"
    )


@router.patch("/servers/{server_name}", response_model=ServerResponse, responses={404: {"model": ErrorResponse}})
async def update_server(
    server_name: str,
    request: Request,
    data: UpdateServerRequest,
    user_id: str = Depends(get_user_id)
):
    """
    Update an MCP server.

    Updates configuration for the specified server.
    """
    service = await get_user_mcp_service()

    # Check server exists
    existing = await service.get_server(user_id, server_name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Server '{server_name}' not found")

    # Build update dict from non-None values
    updates = {k: v for k, v in data.dict().items() if v is not None}

    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")

    try:
        await service.update_server(user_id, server_name, **updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Return updated server
    return await get_server(server_name, request, user_id)


@router.delete("/servers/{server_name}", responses={404: {"model": ErrorResponse}})
async def delete_server(
    server_name: str,
    request: Request,
    user_id: str = Depends(get_user_id)
):
    """
    Delete an MCP server.

    Permanently removes the specified server configuration.
    """
    service = await get_user_mcp_service()

    # Check server exists
    existing = await service.get_server(user_id, server_name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Server '{server_name}' not found")

    await service.delete_server(user_id, server_name)

    return {"status": "deleted", "server_name": server_name}


@router.post("/servers/{server_name}/test", response_model=TestConnectionResponse)
async def test_server_connection(
    server_name: str,
    request: Request,
    user_id: str = Depends(get_user_id)
):
    """
    Test connection to an MCP server.

    Attempts to connect to the server and returns connection status.
    """
    service = await get_user_mcp_service()

    # Check server exists
    existing = await service.get_server(user_id, server_name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Server '{server_name}' not found")

    result = await service.test_connection(user_id, server_name)

    return TestConnectionResponse(
        success=result.success,
        latency_ms=result.latency_ms,
        error=result.error,
        tools_discovered=result.tools_discovered
    )


# ========================================
# USER SETTINGS ENDPOINTS
# ========================================

@router.get("/settings", response_model=UserSettingsResponse)
async def get_settings(
    request: Request,
    user_id: str = Depends(get_user_id)
):
    """
    Get user's MCP settings.

    Returns the user's MCP preferences and limits.
    """
    service = await get_user_mcp_service()

    settings = await service.get_user_settings(user_id)

    return UserSettingsResponse(
        mcp_enabled=settings.mcp_enabled,
        include_global_servers=settings.include_global_servers,
        max_servers=settings.max_servers,
        default_timeout=settings.default_timeout
    )


@router.patch("/settings", response_model=UserSettingsResponse)
async def update_settings(
    request: Request,
    data: UserSettingsRequest,
    user_id: str = Depends(get_user_id)
):
    """
    Update user's MCP settings.

    Updates the user's MCP preferences.
    """
    service = await get_user_mcp_service()

    # Build update dict from non-None values
    updates = {k: v for k, v in data.dict().items() if v is not None}

    if updates:
        await service.update_user_settings(user_id, **updates)

    return await get_settings(request, user_id)


# ========================================
# SERVER DISCOVERY ENDPOINT
# ========================================

@router.get("/available", response_model=AvailableServersResponse)
async def get_available_servers(
    request: Request,
    user_id: str = Depends(get_user_id)
):
    """
    Get all available MCP servers for session creation.

    Returns both global platform servers and user's custom servers,
    formatted for use in the tools array when creating sessions.

    Example response:
    ```json
    {
        "global_servers": [
            {"name": "ghost", "tool_id": "mcp:ghost", "tools_count": 3}
        ],
        "user_servers": [
            {"name": "my-server", "tool_id": "mcp:user:my-server", "tools_count": 10}
        ],
        "include_global": true
    }
    ```

    Use the tool_id values in the session's tools array.
    """
    service = await get_user_mcp_service()
    mcp_tool = await get_mcp_tool()

    # Get user's servers and settings
    result = await service.get_available_servers_for_session(user_id)

    # Get global servers from MCP tool
    global_servers = []
    if result.get('include_global'):
        global_server_names = mcp_tool.get_global_server_names()
        for full_name in global_server_names:
            # Extract server name from "global::servername" format
            name = full_name.replace("global::", "")
            global_servers.append({
                "name": name,
                "tool_id": f"mcp:{name}",
                "display_name": name.title(),
                "type": "global"
            })

    return AvailableServersResponse(
        global_servers=global_servers,
        user_servers=result.get('user', []),
        include_global=result.get('include_global', True)
    )


# ========================================
# AUDIT LOG ENDPOINT
# ========================================

@router.get("/audit", response_model=AuditLogResponse)
async def get_audit_log(
    request: Request,
    limit: int = 50,
    server_name: Optional[str] = None,
    user_id: str = Depends(get_user_id)
):
    """
    Get MCP audit log.

    Returns audit log entries for the user's MCP server operations.
    """
    service = await get_user_mcp_service()

    entries = await service.db.get_audit_log(
        user_id,
        limit=min(limit, 100),
        server_name=server_name
    )

    return AuditLogResponse(
        entries=[
            AuditLogEntry(
                id=e['id'],
                action=e['action'],
                server_name=e['server_name'],
                details=e['details'],
                ip_address=e['ip_address'],
                timestamp=str(e['timestamp'])
            )
            for e in entries
        ],
        count=len(entries)
    )
