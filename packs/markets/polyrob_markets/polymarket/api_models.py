"""
Pydantic models for Polymarket API endpoints.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator
import re


class TradingLimitsRequest(BaseModel):
    """Trading limits configuration."""
    max_order_size_usd: int = Field(default=1000, ge=1, le=100000)
    max_total_exposure_usd: int = Field(default=5000, ge=1, le=1000000)
    max_position_per_market_usd: int = Field(default=2000, ge=1, le=100000)
    min_liquidity_required: int = Field(default=10000, ge=0)
    max_spread_tolerance: float = Field(default=0.05, ge=0, le=1)
    require_confirmation_above_usd: int = Field(default=500, ge=0)
    enable_autonomous_trading: bool = Field(default=False)


class ConfigurePolymarketRequest(BaseModel):
    """Request to configure Polymarket credentials with proxy wallet support."""
    wallet_address: Optional[str] = Field(
        None,
        description="EOA wallet address (0x...) - derived from private key",
        pattern=r"^0x[a-fA-F0-9]{40}$"
    )
    proxy_wallet_address: Optional[str] = Field(
        None,
        description="Proxy wallet address from Polymarket profile (for website users)",
        pattern=r"^0x[a-fA-F0-9]{40}$"
    )
    private_key: Optional[str] = Field(
        None,
        description="Polygon private key (with or without 0x prefix)",
        min_length=64,
        max_length=66
    )
    signature_type: int = Field(
        default=2,
        ge=0,
        le=2,
        description="Signature type: 0=EOA (hardware wallet), 1=Magic (email wallet), 2=Proxy (Polymarket website users, default)"
    )
    demo_mode: bool = Field(
        default=True,
        description="Use demo mode (read-only, no wallet required)"
    )
    trading_limits: Optional[TradingLimitsRequest] = Field(
        default=None,
        description="Trading safety limits"
    )

    @field_validator("wallet_address")
    @classmethod
    def validate_wallet_address(cls, v: Optional[str]) -> Optional[str]:
        if v and not re.match(r"^0x[a-fA-F0-9]{40}$", v):
            raise ValueError("Invalid wallet address format. Must be 0x followed by 40 hex characters.")
        return v

    @field_validator("proxy_wallet_address")
    @classmethod
    def validate_proxy_wallet_address(cls, v: Optional[str]) -> Optional[str]:
        if v and not re.match(r"^0x[a-fA-F0-9]{40}$", v):
            raise ValueError("Invalid proxy wallet address format. Must be 0x followed by 40 hex characters.")
        return v

    @field_validator("private_key")
    @classmethod
    def validate_private_key(cls, v: Optional[str]) -> Optional[str]:
        if v:
            # Remove 0x prefix for validation
            key = v[2:] if v.startswith("0x") else v
            if len(key) != 64 or not re.match(r"^[a-fA-F0-9]+$", key):
                raise ValueError("Invalid private key format. Must be 64 hex characters.")
        return v


class UpdateTradingLimitsRequest(BaseModel):
    """Request to update trading limits only."""
    trading_limits: TradingLimitsRequest


class PolymarketStatusResponse(BaseModel):
    """Response with Polymarket configuration status."""
    configured: bool
    enabled: bool
    demo_mode: bool
    wallet_address: Optional[str] = None
    proxy_wallet_address: Optional[str] = None
    has_private_key: bool = False
    has_api_credentials: bool = False
    signature_type: int = 2  # 0=EOA, 1=Magic, 2=Proxy
    chain_id: int = 137
    trading_limits: Dict[str, Any]
    connection_count: int = 0
    last_connected_at: Optional[str] = None
    last_error: Optional[str] = None
    allowances_verified: bool = False


class ExecuteToolRequest(BaseModel):
    """Request to execute a Polymarket tool."""
    tool_name: str = Field(..., description="Name of the tool to execute")
    arguments: Dict[str, Any] = Field(
        default_factory=dict,
        description="Tool arguments"
    )


class ExecuteToolResponse(BaseModel):
    """Response from tool execution."""
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    tool_name: str
    execution_time_ms: float


class AvailableToolsResponse(BaseModel):
    """Response with available Polymarket tools."""
    tools: List[Dict[str, Any]]
    demo_mode: bool
    enabled: bool


class AuditLogEntryResponse(BaseModel):
    """Single audit log entry."""
    id: int
    action: str
    tool_name: Optional[str]
    market_id: Optional[str]
    details: Optional[Dict[str, Any]]
    ip_address: Optional[str]
    timestamp: str


class AuditLogResponse(BaseModel):
    """Response with audit log entries."""
    entries: List[AuditLogEntryResponse]
    count: int


class TradingStatsResponse(BaseModel):
    """Response with trading statistics."""
    total_calls: int
    orders_placed: int
    orders_cancelled: int
    first_activity: Optional[str]
    last_activity: Optional[str]


class ErrorResponse(BaseModel):
    """Error response."""
    detail: str
