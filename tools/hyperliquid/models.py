"""
Hyperliquid Tool Data Models

Constants, credentials, and result types for Hyperliquid integration.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from datetime import datetime

# =============================================================================
# Constants
# =============================================================================

# API Endpoints
MAINNET_API_URL = "https://api.hyperliquid.xyz"
TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"
MAINNET_WS_URL = "wss://api.hyperliquid.xyz/ws"
TESTNET_WS_URL = "wss://api.hyperliquid-testnet.xyz/ws"

# Minimum order values
MIN_ORDER_VALUE_USD = 10.0  # Hyperliquid minimum

# Leverage limits
MAX_LEVERAGE = 50
DEFAULT_LEVERAGE = 1

# Order types
ORDER_TYPE_LIMIT = "Limit"
ORDER_TYPE_MARKET = "Market"
ORDER_TYPE_TRIGGER = "Trigger"

# Time in Force options
TIF_GTC = "Gtc"  # Good til cancelled
TIF_IOC = "Ioc"  # Immediate or cancel
TIF_ALO = "Alo"  # Add liquidity only (post-only)

# Position sides
SIDE_LONG = "long"
SIDE_SHORT = "short"


# =============================================================================
# Trading Limits
# =============================================================================

@dataclass
class TradingLimits:
    """Safety constraints for autonomous trading"""

    # Order size limits
    max_order_size_usd: float = 1000.0
    max_total_exposure_usd: float = 10000.0
    max_position_per_market_usd: float = 5000.0

    # Leverage limits
    max_leverage: int = 5  # Conservative default

    # Risk management
    max_daily_loss_usd: float = 500.0
    require_confirmation_above_usd: float = 500.0

    # Trading controls
    enable_autonomous_trading: bool = False
    allowed_coins: List[str] = field(default_factory=list)  # Empty = all allowed
    blocked_coins: List[str] = field(default_factory=list)

    # Spread tolerance
    max_spread_tolerance: float = 0.01  # 1%
    min_liquidity_required: float = 50000.0  # USD in orderbook

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_order_size_usd": self.max_order_size_usd,
            "max_total_exposure_usd": self.max_total_exposure_usd,
            "max_position_per_market_usd": self.max_position_per_market_usd,
            "max_leverage": self.max_leverage,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "require_confirmation_above_usd": self.require_confirmation_above_usd,
            "enable_autonomous_trading": self.enable_autonomous_trading,
            "allowed_coins": self.allowed_coins,
            "blocked_coins": self.blocked_coins,
            "max_spread_tolerance": self.max_spread_tolerance,
            "min_liquidity_required": self.min_liquidity_required,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TradingLimits":
        return cls(
            max_order_size_usd=data.get("max_order_size_usd", 1000.0),
            max_total_exposure_usd=data.get("max_total_exposure_usd", 10000.0),
            max_position_per_market_usd=data.get("max_position_per_market_usd", 5000.0),
            max_leverage=data.get("max_leverage", 5),
            max_daily_loss_usd=data.get("max_daily_loss_usd", 500.0),
            require_confirmation_above_usd=data.get("require_confirmation_above_usd", 500.0),
            enable_autonomous_trading=data.get("enable_autonomous_trading", False),
            allowed_coins=data.get("allowed_coins", []),
            blocked_coins=data.get("blocked_coins", []),
            max_spread_tolerance=data.get("max_spread_tolerance", 0.01),
            min_liquidity_required=data.get("min_liquidity_required", 50000.0),
        )


# =============================================================================
# Credentials
# =============================================================================

@dataclass
class AgentWallet:
    """Hyperliquid agent wallet for delegated trading"""

    address: str  # Agent wallet address
    private_key: str  # Agent wallet private key (encrypted at rest)
    name: Optional[str] = None
    created_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "address": self.address,
            "name": self.name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentWallet":
        return cls(
            address=data["address"],
            private_key=data.get("private_key", ""),  # Decrypted separately
            name=data.get("name"),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
        )


@dataclass
class HyperliquidCredentials:
    """User credentials for Hyperliquid trading"""

    user_id: str
    wallet_address: str  # Main wallet address (for account queries)
    private_key: str  # Main wallet private key (encrypted at rest)

    # Agent wallet (optional - for delegated trading)
    agent_wallet: Optional[AgentWallet] = None

    # Network configuration
    testnet: bool = True  # Default to testnet for safety

    # Trading mode
    demo_mode: bool = True  # Read-only mode
    enabled: bool = True

    # Safety limits
    trading_limits: TradingLimits = field(default_factory=TradingLimits)

    # Connection tracking
    last_connected_at: Optional[datetime] = None
    last_error: Optional[str] = None
    connection_count: int = 0

    @property
    def api_url(self) -> str:
        """Get the appropriate API URL based on network"""
        return TESTNET_API_URL if self.testnet else MAINNET_API_URL

    @property
    def ws_url(self) -> str:
        """Get the appropriate WebSocket URL based on network"""
        return TESTNET_WS_URL if self.testnet else MAINNET_WS_URL

    @property
    def trading_wallet_address(self) -> str:
        """Get the wallet address to use for trading (agent or main)"""
        if self.agent_wallet:
            return self.agent_wallet.address
        return self.wallet_address

    @property
    def trading_private_key(self) -> str:
        """Get the private key to use for trading (agent or main)"""
        if self.agent_wallet:
            return self.agent_wallet.private_key
        return self.private_key

    def is_configured(self) -> bool:
        """Check if credentials are properly configured"""
        return bool(self.wallet_address and self.private_key)

    def can_trade(self) -> bool:
        """Check if trading is allowed"""
        return (
            self.is_configured() and
            self.enabled and
            not self.demo_mode and
            self.trading_limits.enable_autonomous_trading
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "wallet_address": self.wallet_address,
            "agent_wallet": self.agent_wallet.to_dict() if self.agent_wallet else None,
            "testnet": self.testnet,
            "demo_mode": self.demo_mode,
            "enabled": self.enabled,
            "trading_limits": self.trading_limits.to_dict(),
            "last_connected_at": self.last_connected_at.isoformat() if self.last_connected_at else None,
            "last_error": self.last_error,
            "connection_count": self.connection_count,
        }


# =============================================================================
# Execution Result
# =============================================================================

@dataclass
class ExecutionResult:
    """Result of a tool action execution"""

    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    tool_name: Optional[str] = None
    execution_time_ms: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "tool_name": self.tool_name,
            "execution_time_ms": self.execution_time_ms,
        }
