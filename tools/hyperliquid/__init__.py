"""
Hyperliquid Tool Package

Provides trading capabilities for Hyperliquid perpetual futures and spot markets.
"""

from tools.hyperliquid.service import HyperliquidTool, HyperliquidDataTool
from tools.hyperliquid.models import (
    HyperliquidCredentials,
    TradingLimits,
    ExecutionResult,
    AgentWallet,
    MAINNET_API_URL,
    TESTNET_API_URL,
    MAINNET_WS_URL,
    TESTNET_WS_URL,
    MIN_ORDER_VALUE_USD,
    MAX_LEVERAGE,
    DEFAULT_LEVERAGE,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    ORDER_TYPE_TRIGGER,
    TIF_GTC,
    TIF_IOC,
    TIF_ALO,
    SIDE_LONG,
    SIDE_SHORT,
)

__all__ = [
    # Main tool
    "HyperliquidTool",
    "HyperliquidDataTool",
    # Credentials
    "HyperliquidCredentials",
    "TradingLimits",
    "ExecutionResult",
    "AgentWallet",
    # API URLs
    "MAINNET_API_URL",
    "TESTNET_API_URL",
    "MAINNET_WS_URL",
    "TESTNET_WS_URL",
    # Constants
    "MIN_ORDER_VALUE_USD",
    "MAX_LEVERAGE",
    "DEFAULT_LEVERAGE",
    "ORDER_TYPE_LIMIT",
    "ORDER_TYPE_MARKET",
    "ORDER_TYPE_TRIGGER",
    "TIF_GTC",
    "TIF_IOC",
    "TIF_ALO",
    "SIDE_LONG",
    "SIDE_SHORT",
]
