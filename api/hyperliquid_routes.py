"""
Hyperliquid API Routes

FastAPI endpoints for Hyperliquid tool configuration and execution.
"""

from fastapi import APIRouter, Depends, Request, HTTPException
from typing import Optional

from api.hyperliquid_models import (
    ConfigureHyperliquidRequest,
    UpdateTradingLimitsRequest,
    ExecuteToolRequest,
    SetDemoModeRequest,
    SetEnabledRequest,
    HyperliquidStatusResponse,
    ExecuteToolResponse,
    AvailableToolsResponse,
    AuditLogResponse,
    TradingStatsResponse,
    SuccessResponse,
)
from tools.hyperliquid.service import HyperliquidTool
from tools.hyperliquid.models import TradingLimits, AgentWallet
from modules.database.hyperliquid import HyperliquidDBHandler
from core.logging import get_component_logger
from api.dependencies import get_user_id, get_client_ip

router = APIRouter(prefix="/api/hyperliquid", tags=["hyperliquid"])
logger = get_component_logger("HyperliquidAPI")


def get_hyperliquid_tool(request: Request) -> HyperliquidTool:
    """Get HyperliquidTool from container"""
    container = getattr(request.app.state, "container", None)
    if not container:
        raise HTTPException(status_code=503, detail="Service container not available")

    tool = container.get_service("hyperliquid")
    if not tool:
        raise HTTPException(status_code=503, detail="Hyperliquid service not available")
    return tool


def get_hyperliquid_db(request: Request) -> HyperliquidDBHandler:
    """Get HyperliquidDBHandler from container"""
    container = getattr(request.app.state, "container", None)
    if not container:
        raise HTTPException(status_code=503, detail="Service container not available")

    db = container.get_service("hyperliquid_db")
    if not db:
        raise HTTPException(status_code=503, detail="Hyperliquid database not available")
    return db


# =============================================================================
# Configuration Endpoints
# =============================================================================

@router.post("/configure", response_model=HyperliquidStatusResponse)
async def configure_hyperliquid(
    request: Request,
    body: ConfigureHyperliquidRequest,
    user_id: str = Depends(get_user_id),
    client_ip: str = Depends(get_client_ip),
):
    """Configure Hyperliquid credentials for the user"""
    db = get_hyperliquid_db(request)

    # Build agent wallet if provided
    agent_wallet = None
    if body.agent_wallet_address and body.agent_wallet_private_key:
        agent_wallet = AgentWallet(
            address=body.agent_wallet_address,
            private_key=body.agent_wallet_private_key,
            name=body.agent_wallet_name,
        )

    # Build trading limits
    trading_limits = TradingLimits(
        max_order_size_usd=body.max_order_size_usd or 1000.0,
        max_leverage=body.max_leverage or 5,
        enable_autonomous_trading=body.enable_autonomous_trading or False,
    )

    await db.save_credentials(
        user_id=user_id,
        wallet_address=body.wallet_address,
        private_key=body.private_key,
        agent_wallet=agent_wallet,
        testnet=body.testnet,
        demo_mode=body.demo_mode,
        trading_limits=trading_limits,
    )

    # Audit
    await db.audit_log(
        user_id=user_id,
        action="configure",
        details={"testnet": body.testnet, "demo_mode": body.demo_mode},
        ip_address=client_ip,
    )

    # Mask wallet address for response
    masked_wallet = f"{body.wallet_address[:6]}...{body.wallet_address[-4:]}"
    masked_agent = None
    if agent_wallet:
        masked_agent = f"{agent_wallet.address[:6]}...{agent_wallet.address[-4:]}"

    return HyperliquidStatusResponse(
        configured=True,
        wallet_address=masked_wallet,
        agent_wallet_address=masked_agent,
        testnet=body.testnet,
        demo_mode=body.demo_mode,
        enabled=True,
        trading_limits=trading_limits.to_dict(),
    )


@router.get("/status", response_model=HyperliquidStatusResponse)
async def get_status(
    request: Request,
    user_id: str = Depends(get_user_id),
):
    """Get current Hyperliquid configuration status"""
    db = get_hyperliquid_db(request)
    credentials = await db.get_credentials(user_id)

    if not credentials:
        return HyperliquidStatusResponse(
            configured=False,
            wallet_address=None,
            testnet=True,
            demo_mode=True,
            enabled=False,
        )

    # Mask addresses
    masked_wallet = f"{credentials.wallet_address[:6]}...{credentials.wallet_address[-4:]}"
    masked_agent = None
    if credentials.agent_wallet:
        masked_agent = f"{credentials.agent_wallet.address[:6]}...{credentials.agent_wallet.address[-4:]}"

    return HyperliquidStatusResponse(
        configured=True,
        wallet_address=masked_wallet,
        agent_wallet_address=masked_agent,
        testnet=credentials.testnet,
        demo_mode=credentials.demo_mode,
        enabled=credentials.enabled,
        trading_limits=credentials.trading_limits.to_dict(),
        connection_count=credentials.connection_count,
        last_connected_at=credentials.last_connected_at.isoformat() if credentials.last_connected_at else None,
        last_error=credentials.last_error,
    )


@router.put("/trading-limits", response_model=SuccessResponse)
async def update_trading_limits(
    request: Request,
    body: UpdateTradingLimitsRequest,
    user_id: str = Depends(get_user_id),
    client_ip: str = Depends(get_client_ip),
):
    """Update trading limits"""
    db = get_hyperliquid_db(request)

    # Get existing credentials
    credentials = await db.get_credentials(user_id)
    if not credentials:
        raise HTTPException(status_code=404, detail="Credentials not configured")

    # Update limits with provided values
    limits = credentials.trading_limits
    if body.max_order_size_usd is not None:
        limits.max_order_size_usd = body.max_order_size_usd
    if body.max_total_exposure_usd is not None:
        limits.max_total_exposure_usd = body.max_total_exposure_usd
    if body.max_position_per_market_usd is not None:
        limits.max_position_per_market_usd = body.max_position_per_market_usd
    if body.max_leverage is not None:
        limits.max_leverage = body.max_leverage
    if body.max_daily_loss_usd is not None:
        limits.max_daily_loss_usd = body.max_daily_loss_usd
    if body.require_confirmation_above_usd is not None:
        limits.require_confirmation_above_usd = body.require_confirmation_above_usd
    if body.enable_autonomous_trading is not None:
        limits.enable_autonomous_trading = body.enable_autonomous_trading
    if body.allowed_coins is not None:
        limits.allowed_coins = body.allowed_coins
    if body.blocked_coins is not None:
        limits.blocked_coins = body.blocked_coins

    await db.update_trading_limits(user_id, limits)

    # Audit
    await db.audit_log(
        user_id=user_id,
        action="update_trading_limits",
        details=body.model_dump(exclude_unset=True),
        ip_address=client_ip,
    )

    return SuccessResponse(success=True, message="Trading limits updated")


@router.put("/demo-mode", response_model=SuccessResponse)
async def set_demo_mode(
    request: Request,
    body: SetDemoModeRequest,
    user_id: str = Depends(get_user_id),
    client_ip: str = Depends(get_client_ip),
):
    """Set demo mode (read-only or trading)"""
    db = get_hyperliquid_db(request)

    updated = await db.set_demo_mode(user_id, body.demo_mode)
    if not updated:
        raise HTTPException(status_code=404, detail="Credentials not configured")

    # Audit
    await db.audit_log(
        user_id=user_id,
        action="set_demo_mode",
        details={"demo_mode": body.demo_mode},
        ip_address=client_ip,
    )

    mode_str = "read-only" if body.demo_mode else "trading"
    return SuccessResponse(success=True, message=f"Demo mode set to {mode_str}")


@router.put("/enabled", response_model=SuccessResponse)
async def set_enabled(
    request: Request,
    body: SetEnabledRequest,
    user_id: str = Depends(get_user_id),
    client_ip: str = Depends(get_client_ip),
):
    """Enable or disable Hyperliquid integration"""
    db = get_hyperliquid_db(request)

    updated = await db.set_enabled(user_id, body.enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="Credentials not configured")

    # Audit
    await db.audit_log(
        user_id=user_id,
        action="set_enabled",
        details={"enabled": body.enabled},
        ip_address=client_ip,
    )

    status_str = "enabled" if body.enabled else "disabled"
    return SuccessResponse(success=True, message=f"Hyperliquid {status_str}")


@router.delete("/credentials", response_model=SuccessResponse)
async def delete_credentials(
    request: Request,
    user_id: str = Depends(get_user_id),
    client_ip: str = Depends(get_client_ip),
):
    """Permanently delete Hyperliquid credentials"""
    db = get_hyperliquid_db(request)

    deleted = await db.delete_credentials(user_id)

    if deleted:
        await db.audit_log(
            user_id=user_id,
            action="delete_credentials",
            ip_address=client_ip,
        )

    return SuccessResponse(success=deleted, message="Credentials deleted" if deleted else "No credentials found")


# =============================================================================
# Tool Endpoints
# =============================================================================

@router.get("/tools", response_model=AvailableToolsResponse)
async def get_available_tools(
    request: Request,
):
    """List all available Hyperliquid tools"""
    tool = get_hyperliquid_tool(request)
    tools = tool.get_available_tools()
    return AvailableToolsResponse(tools=tools, count=len(tools))


@router.post("/execute", response_model=ExecuteToolResponse)
async def execute_tool(
    request: Request,
    body: ExecuteToolRequest,
    user_id: str = Depends(get_user_id),
    client_ip: str = Depends(get_client_ip),
):
    """Execute a Hyperliquid tool action"""
    tool = get_hyperliquid_tool(request)

    # Set user context
    tool.set_user_context(user_id)

    # Execute action
    result = await tool.execute_action(body.tool_name, body.arguments or {})

    return ExecuteToolResponse(
        success=result.success,
        data=result.data,
        error=result.error,
        tool_name=result.tool_name,
        execution_time_ms=result.execution_time_ms,
    )


# =============================================================================
# Convenience Endpoints (direct action access)
# =============================================================================

@router.get("/markets/perpetual")
async def get_perpetual_markets(
    request: Request,
    user_id: str = Depends(get_user_id),
):
    """Get all perpetual markets"""
    tool = get_hyperliquid_tool(request)
    tool.set_user_context(user_id)
    result = await tool.execute_action("get_perpetual_markets", {})
    return result.data if result.success else {"error": result.error}


@router.get("/markets/spot")
async def get_spot_markets(
    request: Request,
    user_id: str = Depends(get_user_id),
):
    """Get all spot markets"""
    tool = get_hyperliquid_tool(request)
    tool.set_user_context(user_id)
    result = await tool.execute_action("get_spot_markets", {})
    return result.data if result.success else {"error": result.error}


@router.get("/price/{coin}")
async def get_price(
    request: Request,
    coin: str,
    user_id: str = Depends(get_user_id),
):
    """Get current price for a coin"""
    tool = get_hyperliquid_tool(request)
    tool.set_user_context(user_id)
    result = await tool.execute_action("get_current_price", {"coin": coin})
    return result.data if result.success else {"error": result.error}


@router.get("/orderbook/{coin}")
async def get_orderbook(
    request: Request,
    coin: str,
    depth: int = 10,
    user_id: str = Depends(get_user_id),
):
    """Get orderbook for a coin"""
    tool = get_hyperliquid_tool(request)
    tool.set_user_context(user_id)
    result = await tool.execute_action("get_orderbook", {"coin": coin, "depth": depth})
    return result.data if result.success else {"error": result.error}


@router.get("/funding/{coin}")
async def get_funding(
    request: Request,
    coin: str,
    user_id: str = Depends(get_user_id),
):
    """Get funding rate for a coin"""
    tool = get_hyperliquid_tool(request)
    tool.set_user_context(user_id)
    result = await tool.execute_action("get_funding_rate", {"coin": coin})
    return result.data if result.success else {"error": result.error}


@router.get("/account")
async def get_account(
    request: Request,
    user_id: str = Depends(get_user_id),
):
    """Get account state"""
    tool = get_hyperliquid_tool(request)
    tool.set_user_context(user_id)
    result = await tool.execute_action("get_account_state", {})
    return result.data if result.success else {"error": result.error}


@router.get("/balances/spot")
async def get_spot_balances(
    request: Request,
    user_id: str = Depends(get_user_id),
):
    """Get spot balances"""
    tool = get_hyperliquid_tool(request)
    tool.set_user_context(user_id)
    result = await tool.execute_action("get_spot_balances", {})
    return result.data if result.success else {"error": result.error}


@router.get("/orders/open")
async def get_open_orders(
    request: Request,
    user_id: str = Depends(get_user_id),
):
    """Get open orders"""
    tool = get_hyperliquid_tool(request)
    tool.set_user_context(user_id)
    result = await tool.execute_action("get_open_orders", {})
    return result.data if result.success else {"error": result.error}


# =============================================================================
# Audit & Stats Endpoints
# =============================================================================

@router.get("/audit", response_model=AuditLogResponse)
async def get_audit_log(
    request: Request,
    limit: int = 50,
    action: Optional[str] = None,
    user_id: str = Depends(get_user_id),
):
    """Get audit log entries"""
    db = get_hyperliquid_db(request)
    entries = await db.get_audit_log(user_id, limit, action)
    return AuditLogResponse(entries=entries, count=len(entries))


@router.get("/stats", response_model=TradingStatsResponse)
async def get_trading_stats(
    request: Request,
    user_id: str = Depends(get_user_id),
):
    """Get trading statistics"""
    db = get_hyperliquid_db(request)
    stats = await db.get_trading_stats(user_id)
    return TradingStatsResponse(**stats)
