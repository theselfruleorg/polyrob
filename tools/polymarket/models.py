"""
Polymarket data models.

Defines credentials, trading limits, and execution results.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Chain IDs
POLYGON_MAINNET = 137
POLYGON_AMOY_TESTNET = 80002

# Signature Types for CLOB client authentication
SIGNATURE_TYPE_EOA = 0      # Standard EOA (MetaMask, hardware wallets)
SIGNATURE_TYPE_MAGIC = 1    # Email/Magic wallet (delegated signing)
SIGNATURE_TYPE_PROXY = 2    # Browser wallet proxy (Polymarket website users)

# Trading Constants
MIN_ORDER_VALUE_USD = 1.00

# Polymarket Contract Addresses (Polygon)
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"


@dataclass
class TradingLimits:
    """
    Trading safety limits for Polymarket operations.

    All limits are enforced at the tool level before
    making API calls.
    """
    max_order_size_usd: int = 1000
    max_total_exposure_usd: int = 5000
    max_position_per_market_usd: int = 2000
    min_liquidity_required: int = 10000
    max_spread_tolerance: float = 0.05
    require_confirmation_above_usd: int = 500
    enable_autonomous_trading: bool = False
    allowed_categories: List[str] = field(default_factory=lambda: ["*"])
    blocked_markets: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "max_order_size_usd": self.max_order_size_usd,
            "max_total_exposure_usd": self.max_total_exposure_usd,
            "max_position_per_market_usd": self.max_position_per_market_usd,
            "min_liquidity_required": self.min_liquidity_required,
            "max_spread_tolerance": self.max_spread_tolerance,
            "require_confirmation_above_usd": self.require_confirmation_above_usd,
            "enable_autonomous_trading": self.enable_autonomous_trading,
            "allowed_categories": self.allowed_categories,
            "blocked_markets": self.blocked_markets,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TradingLimits":
        """Create from dictionary."""
        if not data:
            return cls()
        return cls(
            max_order_size_usd=data.get("max_order_size_usd", 1000),
            max_total_exposure_usd=data.get("max_total_exposure_usd", 5000),
            max_position_per_market_usd=data.get("max_position_per_market_usd", 2000),
            min_liquidity_required=data.get("min_liquidity_required", 10000),
            max_spread_tolerance=data.get("max_spread_tolerance", 0.05),
            require_confirmation_above_usd=data.get("require_confirmation_above_usd", 500),
            enable_autonomous_trading=data.get("enable_autonomous_trading", False),
            allowed_categories=data.get("allowed_categories", ["*"]),
            blocked_markets=data.get("blocked_markets", []),
        )


@dataclass
class ApiCredentials:
    """
    L2 API credentials for authenticated CLOB operations.

    These are created via the CLOB API using the private key,
    and stored encrypted for reuse.
    """
    api_key: str
    api_secret: str  # Same as passphrase in some contexts
    api_passphrase: str
    created_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary (secrets included for encrypted storage)."""
        return {
            "api_key": self.api_key,
            "api_secret": self.api_secret,
            "api_passphrase": self.api_passphrase,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["ApiCredentials"]:
        """Create from dictionary."""
        if not data or not data.get("api_key"):
            return None
        return cls(
            api_key=data.get("api_key", ""),
            api_secret=data.get("api_secret", ""),
            api_passphrase=data.get("api_passphrase", ""),
            created_at=data.get("created_at"),
        )


@dataclass
class PolymarketCredentials:
    """
    User's Polymarket credentials with proxy wallet support.

    The private_key is stored encrypted in the database and only
    decrypted in memory when needed for API authentication.

    L2 API credentials (api_credentials) are created via CLOB API
    and stored for reuse to avoid repeated key creation.

    Polymarket uses a proxy wallet system:
    - wallet_address: EOA address (derived from private key)
    - proxy_wallet_address: Smart contract wallet (shown on Polymarket profile)

    Most Polymarket website users need signature_type=2 (PROXY).
    """
    user_id: str
    wallet_address: Optional[str] = None  # EOA address (derived from private key)
    proxy_wallet_address: Optional[str] = None  # Smart contract wallet (from Polymarket profile)
    private_key: Optional[str] = field(default=None, repr=False)  # Never log this
    signature_type: int = SIGNATURE_TYPE_PROXY  # Default to proxy (2) - most common
    demo_mode: bool = True
    enabled: bool = True
    chain_id: int = POLYGON_MAINNET
    trading_limits: TradingLimits = field(default_factory=TradingLimits)
    api_credentials: Optional[ApiCredentials] = field(default=None, repr=False)
    allowances_verified: bool = False  # Track if allowances are configured

    @property
    def funder_address(self) -> Optional[str]:
        """
        Get the address that holds funds (proxy wallet if set, else EOA).

        This is the address used for balance checks and as the 'funder'
        parameter in CLOB client for proxy wallet users.
        """
        return self.proxy_wallet_address or self.wallet_address

    def is_configured(self) -> bool:
        """Check if credentials are properly configured."""
        if self.demo_mode:
            return True
        return bool(self.wallet_address and self.private_key)

    def has_api_credentials(self) -> bool:
        """Check if L2 API credentials are available."""
        return self.api_credentials is not None

    def can_trade(self) -> bool:
        """Check if user can perform trading operations."""
        return (
            not self.demo_mode
            and self.enabled
            and self.is_configured()
            and self.trading_limits.enable_autonomous_trading
        )


@dataclass
class ExecutionResult:
    """Result from executing a Polymarket action."""
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    tool_name: Optional[str] = None
    execution_time_ms: float = 0.0
