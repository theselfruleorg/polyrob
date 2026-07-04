"""
Polymarket Integration Package.

Provides access to Polymarket prediction markets through PolymarketTool,
which uses direct API integration with Gamma + CLOB APIs.

Usage:
    polymarket.search_markets(query="...")
    polymarket.get_trending_markets(limit=10)
    polymarket.get_market_details(market_id="...")
"""

from tools.polymarket.service import PolymarketTool, PolymarketDataTool

from tools.polymarket.models import (
    PolymarketCredentials,
    TradingLimits,
    ExecutionResult,
    ApiCredentials,
    POLYGON_MAINNET,
    POLYGON_AMOY_TESTNET,
    SIGNATURE_TYPE_EOA,
    SIGNATURE_TYPE_MAGIC,
    SIGNATURE_TYPE_PROXY,
    MIN_ORDER_VALUE_USD,
)

__all__ = [
    "PolymarketTool",
    "PolymarketDataTool",
    "PolymarketCredentials",
    "TradingLimits",
    "ExecutionResult",
    "ApiCredentials",
    "POLYGON_MAINNET",
    "POLYGON_AMOY_TESTNET",
    "SIGNATURE_TYPE_EOA",
    "SIGNATURE_TYPE_MAGIC",
    "SIGNATURE_TYPE_PROXY",
    "MIN_ORDER_VALUE_USD",
]
