"""
Hyperliquid API Models

Pydantic models for request/response validation.
"""

import re
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, field_validator


# =============================================================================
# Validation Patterns
# =============================================================================

WALLET_ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")
PRIVATE_KEY_PATTERN = re.compile(r"^(0x)?[a-fA-F0-9]{64}$")


# =============================================================================
# Request Models
# =============================================================================

class ConfigureHyperliquidRequest(BaseModel):
    """Request to configure Hyperliquid credentials"""

    wallet_address: str = Field(..., description="Ethereum wallet address (0x...)")
    private_key: str = Field(..., description="Wallet private key")

    # Optional agent wallet for delegated trading
    agent_wallet_address: Optional[str] = Field(None, description="Agent wallet address")
    agent_wallet_private_key: Optional[str] = Field(None, description="Agent wallet private key")
    agent_wallet_name: Optional[str] = Field(None, description="Agent wallet label")

    # Network and mode
    testnet: bool = Field(True, description="Use testnet (True) or mainnet (False)")
    demo_mode: bool = Field(True, description="Read-only mode (True) or trading enabled (False)")

    # Trading limits
    max_order_size_usd: Optional[float] = Field(1000.0, ge=10, le=100000)
    max_leverage: Optional[int] = Field(5, ge=1, le=50)
    enable_autonomous_trading: Optional[bool] = Field(False)

    @field_validator("wallet_address", "agent_wallet_address", mode="before")
    @classmethod
    def validate_address(cls, v):
        if v is None:
            return v
        if not WALLET_ADDRESS_PATTERN.match(v):
            raise ValueError("Invalid wallet address format (expected 0x followed by 40 hex chars)")
        return v.lower()

    @field_validator("private_key", "agent_wallet_private_key", mode="before")
    @classmethod
    def validate_private_key(cls, v):
        if v is None:
            return v
        # Remove 0x prefix if present for validation
        key = v[2:] if v.startswith("0x") else v
        if len(key) != 64 or not all(c in "0123456789abcdefABCDEF" for c in key):
            raise ValueError("Invalid private key format (expected 64 hex chars)")
        return v


class UpdateTradingLimitsRequest(BaseModel):
    """Request to update trading limits"""

    max_order_size_usd: Optional[float] = Field(None, ge=10, le=100000)
    max_total_exposure_usd: Optional[float] = Field(None, ge=100, le=1000000)
    max_position_per_market_usd: Optional[float] = Field(None, ge=100, le=500000)
    max_leverage: Optional[int] = Field(None, ge=1, le=50)
    max_daily_loss_usd: Optional[float] = Field(None, ge=10, le=50000)
    require_confirmation_above_usd: Optional[float] = Field(None, ge=0)
    enable_autonomous_trading: Optional[bool] = Field(None)
    allowed_coins: Optional[List[str]] = Field(None)
    blocked_coins: Optional[List[str]] = Field(None)


class ExecuteToolRequest(BaseModel):
    """Request to execute a tool action"""

    tool_name: str = Field(..., description="Name of the tool to execute")
    arguments: Optional[Dict[str, Any]] = Field(default_factory=dict)


class SetDemoModeRequest(BaseModel):
    """Request to set demo mode"""
    demo_mode: bool = Field(..., description="True for read-only, False for trading")


class SetEnabledRequest(BaseModel):
    """Request to enable/disable the tool"""
    enabled: bool = Field(..., description="True to enable, False to disable")


# =============================================================================
# Response Models
# =============================================================================

class HyperliquidStatusResponse(BaseModel):
    """Response for configuration status"""

    configured: bool
    wallet_address: Optional[str] = None
    agent_wallet_address: Optional[str] = None
    testnet: bool = True
    demo_mode: bool = True
    enabled: bool = False
    trading_limits: Optional[Dict[str, Any]] = None
    connection_count: Optional[int] = None
    last_connected_at: Optional[str] = None
    last_error: Optional[str] = None


class ExecuteToolResponse(BaseModel):
    """Response for tool execution"""

    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    tool_name: Optional[str] = None
    execution_time_ms: Optional[float] = None


class AvailableToolsResponse(BaseModel):
    """Response listing available tools"""

    tools: List[Dict[str, Any]]
    count: int


class AuditLogEntry(BaseModel):
    """Single audit log entry"""

    id: int
    action: str
    tool_name: Optional[str] = None
    market_id: Optional[str] = None
    details: Dict[str, Any] = {}
    ip_address: Optional[str] = None
    timestamp: str


class AuditLogResponse(BaseModel):
    """Response for audit log query"""

    entries: List[Dict[str, Any]]
    count: int


class TradingStatsResponse(BaseModel):
    """Response for trading statistics"""

    orders_placed: int = 0
    orders_cancelled: int = 0
    bulk_cancels: int = 0
    leverage_updates: int = 0
    total_actions: int = 0
    first_action: Optional[str] = None
    last_action: Optional[str] = None


class ErrorResponse(BaseModel):
    """Error response"""

    error: str
    detail: Optional[str] = None


class SuccessResponse(BaseModel):
    """Generic success response"""

    success: bool = True
    message: Optional[str] = None
