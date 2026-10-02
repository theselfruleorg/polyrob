"""
Hyperliquid Tool Data Models

Constants, credentials, and result types for Hyperliquid integration.
"""

from dataclasses import dataclass
from typing import Optional, Dict, Any

# =============================================================================
# Constants
# =============================================================================

# API Endpoints (defined in the persistence tier; re-exported here)
from polyrob_markets.hyperliquid.store_models import (  # noqa: E402,F401
    MAINNET_API_URL,
    TESTNET_API_URL,
    MAINNET_WS_URL,
    TESTNET_WS_URL,
)

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
# Trading limits + credentials: moved to polyrob_markets/hyperliquid/store_models.py
# (the DB handler stores them and does not import the service). Re-exported here
# so every existing import path keeps working.
# =============================================================================

from polyrob_markets.hyperliquid.store_models import (  # noqa: E402,F401
    TradingLimits,
    AgentWallet,
    HyperliquidCredentials,
)


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
