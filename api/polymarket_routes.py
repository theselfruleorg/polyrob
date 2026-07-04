"""
API routes for Polymarket integration.

Provides endpoints for configuring wallet credentials, executing tools,
and managing Polymarket access.
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Request, Depends

from api.polymarket_models import (
    ConfigurePolymarketRequest,
    UpdateTradingLimitsRequest,
    PolymarketStatusResponse,
    ExecuteToolRequest,
    ExecuteToolResponse,
    AvailableToolsResponse,
    AuditLogResponse,
    AuditLogEntryResponse,
    TradingStatsResponse,
    ErrorResponse,
)
from tools.polymarket.models import TradingLimits
from api.dependencies import get_user_id, get_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(tags=["polymarket"])


async def get_polymarket_tool():
    """Get PolymarketTool from container."""
    from core.container import DependencyContainer

    container = DependencyContainer.get_instance()
    if not container:
        raise HTTPException(status_code=503, detail="Service unavailable")

    tool = container.get_service("polymarket")
    if not tool:
        raise HTTPException(
            status_code=503,
            detail="Polymarket service not available"
        )

    return tool


async def get_polymarket_db():
    """Get PolymarketDBHandler from container."""
    from core.container import DependencyContainer

    container = DependencyContainer.get_instance()
    if not container:
        raise HTTPException(status_code=503, detail="Service unavailable")

    db = container.get_service("polymarket_db")
    if not db:
        raise HTTPException(
            status_code=503,
            detail="Polymarket database not available"
        )

    return db


# ========================================
# CONFIGURATION ENDPOINTS
# ========================================

@router.post(
    "/configure",
    response_model=PolymarketStatusResponse,
    responses={400: {"model": ErrorResponse}}
)
async def configure_polymarket(
    request: Request,
    data: ConfigurePolymarketRequest,
    user_id: str = Depends(get_user_id)
):
    """
    Configure Polymarket credentials.

    Set up wallet address and private key for trading, or enable demo mode
    for read-only access to market data.

    **Demo Mode**: Set `demo_mode: true` to use Polymarket without a wallet.
    You can search markets, view prices, and analyze data, but cannot trade.

    **Trading Mode**: Set `demo_mode: false` and provide:
    - `wallet_address`: Your Polygon wallet address (0x...)
    - `private_key`: Your wallet's private key (64 hex characters)

    **Security**: Private keys are encrypted at rest using AES-128-CBC.
    """
    db = await get_polymarket_db()

    # Validate: if not demo mode, need wallet credentials
    # Check if this is an update (existing credentials) or new setup
    existing_credentials = await db.get_credentials(user_id)

    if not data.demo_mode:
        # For new setup, require wallet address and private key
        # For updates, allow partial updates (use existing values)
        if not existing_credentials:
            if not data.wallet_address:
                raise HTTPException(
                    status_code=400,
                    detail="Wallet address required when not in demo mode"
                )
            if not data.private_key:
                raise HTTPException(
                    status_code=400,
                    detail="Private key required when not in demo mode"
                )

    # Build trading limits
    trading_limits = None
    if data.trading_limits:
        trading_limits = TradingLimits(
            max_order_size_usd=data.trading_limits.max_order_size_usd,
            max_total_exposure_usd=data.trading_limits.max_total_exposure_usd,
            max_position_per_market_usd=data.trading_limits.max_position_per_market_usd,
            min_liquidity_required=data.trading_limits.min_liquidity_required,
            max_spread_tolerance=data.trading_limits.max_spread_tolerance,
            require_confirmation_above_usd=data.trading_limits.require_confirmation_above_usd,
            enable_autonomous_trading=data.trading_limits.enable_autonomous_trading,
        )
    elif existing_credentials:
        # Preserve existing trading limits if not provided
        trading_limits = existing_credentials.trading_limits

    # Save credentials with proxy wallet support
    await db.save_credentials(
        user_id=user_id,
        wallet_address=data.wallet_address,
        proxy_wallet_address=data.proxy_wallet_address,
        private_key=data.private_key,
        signature_type=data.signature_type,
        demo_mode=data.demo_mode,
        enabled=True,
        trading_limits=trading_limits
    )

    return await get_status(request, user_id)


@router.get("/status", response_model=PolymarketStatusResponse)
async def get_status(
    request: Request,
    user_id: str = Depends(get_user_id)
):
    """
    Get Polymarket configuration status.

    Returns current configuration including wallet address (masked),
    trading limits, and connection statistics.
    """
    db = await get_polymarket_db()

    credentials = await db.get_credentials(user_id)

    if not credentials:
        return PolymarketStatusResponse(
            configured=False,
            enabled=False,
            demo_mode=True,
            has_api_credentials=False,
            signature_type=2,
            chain_id=137,
            trading_limits=TradingLimits().to_dict(),
            allowances_verified=False
        )

    # Mask wallet address for display
    wallet_display = None
    if credentials.wallet_address:
        addr = credentials.wallet_address
        wallet_display = f"{addr[:6]}...{addr[-4:]}"

    # Mask proxy wallet address for display
    proxy_wallet_display = None
    if credentials.proxy_wallet_address:
        addr = credentials.proxy_wallet_address
        proxy_wallet_display = f"{addr[:6]}...{addr[-4:]}"

    # Get connection stats from DB
    query = """
        SELECT connection_count, last_connected_at, last_error
        FROM polymarket_credentials
        WHERE user_id = ?
    """
    stats = await db.db.fetch_one(query, (user_id,))

    return PolymarketStatusResponse(
        configured=True,
        enabled=credentials.enabled,
        demo_mode=credentials.demo_mode,
        wallet_address=wallet_display,
        proxy_wallet_address=proxy_wallet_display,
        has_private_key=bool(credentials.private_key),
        has_api_credentials=credentials.has_api_credentials(),
        signature_type=credentials.signature_type,
        chain_id=credentials.chain_id,
        trading_limits=credentials.trading_limits.to_dict(),
        connection_count=stats["connection_count"] if stats else 0,
        last_connected_at=stats["last_connected_at"] if stats else None,
        last_error=stats["last_error"] if stats else None,
        allowances_verified=credentials.allowances_verified
    )


@router.patch(
    "/trading-limits",
    response_model=PolymarketStatusResponse,
    responses={404: {"model": ErrorResponse}}
)
async def update_trading_limits(
    request: Request,
    data: UpdateTradingLimitsRequest,
    user_id: str = Depends(get_user_id)
):
    """
    Update trading limits without changing credentials.

    Allows adjusting safety limits like max order size, exposure limits,
    and autonomous trading settings.
    """
    db = await get_polymarket_db()

    credentials = await db.get_credentials(user_id)
    if not credentials:
        raise HTTPException(
            status_code=404,
            detail="Polymarket not configured. Use POST /configure first."
        )

    trading_limits = TradingLimits(
        max_order_size_usd=data.trading_limits.max_order_size_usd,
        max_total_exposure_usd=data.trading_limits.max_total_exposure_usd,
        max_position_per_market_usd=data.trading_limits.max_position_per_market_usd,
        min_liquidity_required=data.trading_limits.min_liquidity_required,
        max_spread_tolerance=data.trading_limits.max_spread_tolerance,
        require_confirmation_above_usd=data.trading_limits.require_confirmation_above_usd,
        enable_autonomous_trading=data.trading_limits.enable_autonomous_trading,
    )

    await db.save_credentials(
        user_id=user_id,
        wallet_address=credentials.wallet_address,
        proxy_wallet_address=credentials.proxy_wallet_address,
        signature_type=credentials.signature_type,
        demo_mode=credentials.demo_mode,
        enabled=credentials.enabled,
        trading_limits=trading_limits
    )

    return await get_status(request, user_id)


@router.post("/disable", response_model=PolymarketStatusResponse)
async def disable_polymarket(
    request: Request,
    user_id: str = Depends(get_user_id)
):
    """
    Disable Polymarket access.

    Disables Polymarket without deleting credentials.
    Can be re-enabled later.
    """
    db = await get_polymarket_db()

    credentials = await db.get_credentials(user_id)
    if credentials:
        await db.save_credentials(
            user_id=user_id,
            wallet_address=credentials.wallet_address,
            proxy_wallet_address=credentials.proxy_wallet_address,
            signature_type=credentials.signature_type,
            demo_mode=credentials.demo_mode,
            enabled=False,
            trading_limits=credentials.trading_limits
        )

    return await get_status(request, user_id)


@router.post("/enable", response_model=PolymarketStatusResponse)
async def enable_polymarket(
    request: Request,
    user_id: str = Depends(get_user_id)
):
    """
    Enable Polymarket access.

    Re-enables Polymarket if it was disabled.
    """
    db = await get_polymarket_db()

    credentials = await db.get_credentials(user_id)
    if not credentials:
        raise HTTPException(
            status_code=404,
            detail="Polymarket not configured. Use POST /configure first."
        )

    await db.save_credentials(
        user_id=user_id,
        wallet_address=credentials.wallet_address,
        proxy_wallet_address=credentials.proxy_wallet_address,
        signature_type=credentials.signature_type,
        demo_mode=credentials.demo_mode,
        enabled=True,
        trading_limits=credentials.trading_limits
    )

    return await get_status(request, user_id)


@router.delete("/credentials")
async def delete_credentials(
    request: Request,
    user_id: str = Depends(get_user_id)
):
    """
    Delete Polymarket credentials.

    Permanently removes wallet credentials and configuration.
    This action cannot be undone.
    """
    db = await get_polymarket_db()
    await db.delete_credentials(user_id)

    return {"status": "deleted", "message": "Polymarket credentials removed"}


# ========================================
# TOOL EXECUTION ENDPOINTS
# ========================================

@router.get("/tools", response_model=AvailableToolsResponse)
async def get_available_tools(
    request: Request,
    user_id: str = Depends(get_user_id)
):
    """
    Get available Polymarket tools.

    Returns list of all Polymarket tools with their schemas.
    """
    db = await get_polymarket_db()
    tool = await get_polymarket_tool()

    credentials = await db.get_credentials(user_id)
    if not credentials or not credentials.enabled:
        return AvailableToolsResponse(
            tools=[],
            demo_mode=True,
            enabled=False
        )

    # Get tools from PolymarketTool
    tools = tool.get_available_tools()

    return AvailableToolsResponse(
        tools=tools,
        demo_mode=credentials.demo_mode,
        enabled=credentials.enabled
    )


@router.post("/execute", response_model=ExecuteToolResponse)
async def execute_tool(
    request: Request,
    data: ExecuteToolRequest,
    user_id: str = Depends(get_user_id)
):
    """
    Execute a Polymarket tool.

    Executes the specified tool with provided arguments.
    Trading tools require proper wallet configuration.
    """
    import time
    
    polymarket_tool = await get_polymarket_tool()
    db = await get_polymarket_db()
    
    # Set user context for the tool
    polymarket_tool.set_user_context(user_id)
    
    start_time = time.time()
    
    try:
        # Execute the action on the tool
        result = await polymarket_tool.execute_action(data.tool_name, data.arguments)
        execution_time = (time.time() - start_time) * 1000
        
        # Log to audit
        await db.audit_log(
            user_id=user_id,
            action="tool_call",
            tool_name=data.tool_name,
            details={"arguments": data.arguments, "success": True},
            ip_address=get_client_ip(request)
        )
        
        return ExecuteToolResponse(
            success=True,
            data=result,
            error=None,
            tool_name=data.tool_name,
            execution_time_ms=execution_time
        )
        
    except Exception as e:
        execution_time = (time.time() - start_time) * 1000
        
        # Log error to audit
        await db.audit_log(
            user_id=user_id,
            action="tool_call_error",
            tool_name=data.tool_name,
            details={"arguments": data.arguments, "error": str(e)},
            ip_address=get_client_ip(request)
        )
        
        return ExecuteToolResponse(
            success=False,
            data=None,
            error=str(e),
            tool_name=data.tool_name,
            execution_time_ms=execution_time
        )


# ========================================
# AUDIT & STATS ENDPOINTS
# ========================================

@router.get("/audit", response_model=AuditLogResponse)
async def get_audit_log(
    request: Request,
    limit: int = 50,
    action: Optional[str] = None,
    user_id: str = Depends(get_user_id)
):
    """
    Get Polymarket audit log.

    Returns history of Polymarket operations including tool calls,
    configuration changes, and errors.
    """
    db = await get_polymarket_db()

    entries = await db.get_audit_log(
        user_id,
        limit=min(limit, 100),
        action=action
    )

    return AuditLogResponse(
        entries=[
            AuditLogEntryResponse(
                id=e["id"],
                action=e["action"],
                tool_name=e["tool_name"],
                market_id=e["market_id"],
                details=e["details"],
                ip_address=e["ip_address"],
                timestamp=e["timestamp"]
            )
            for e in entries
        ],
        count=len(entries)
    )


@router.get("/stats", response_model=TradingStatsResponse)
async def get_trading_stats(
    request: Request,
    user_id: str = Depends(get_user_id)
):
    """
    Get trading statistics.

    Returns aggregate statistics about Polymarket usage.
    """
    db = await get_polymarket_db()

    stats = await db.get_trading_stats(user_id)

    return TradingStatsResponse(
        total_calls=stats["total_calls"],
        orders_placed=stats["orders_placed"],
        orders_cancelled=stats["orders_cancelled"],
        first_activity=stats["first_activity"],
        last_activity=stats["last_activity"]
    )
