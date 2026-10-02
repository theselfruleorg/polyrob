"""
Polymarket data models.

Defines credentials, trading limits, and execution results.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional


# Chain IDs + signature types (defined in the persistence tier; re-exported here)
from polyrob_markets.polymarket.store_models import (  # noqa: F401
    POLYGON_MAINNET,
    POLYGON_AMOY_TESTNET,
    SIGNATURE_TYPE_EOA,
    SIGNATURE_TYPE_MAGIC,
    SIGNATURE_TYPE_PROXY,
)

# Trading Constants
MIN_ORDER_VALUE_USD = 1.00


# Trading limits + credentials: moved to polyrob_markets/polymarket/store_models.py
# (the DB handler stores them and does not import the service). Re-exported here
# so every existing import path keeps working.
from polyrob_markets.polymarket.store_models import (  # noqa: E402,F401
    TradingLimits,
    ApiCredentials,
    PolymarketCredentials,
)


@dataclass
class ExecutionResult:
    """Result from executing a Polymarket action."""
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    tool_name: Optional[str] = None
    execution_time_ms: float = 0.0
